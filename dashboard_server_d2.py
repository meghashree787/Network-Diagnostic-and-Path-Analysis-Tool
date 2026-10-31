#!/usr/bin/env python3
"""
D2 web dashboard backend (adapted from the D1 dashboard_server.py).

Differences from D1:
  * topology, hosts and the ACTIVE path come from the D2 controller
    (/diag/state, /diag/path) instead of ryu.app.rest_topology + BFS
  * paths are ranked by link delay (cost_ms) and show predicted RTT
  * /api/policy sets the routing policy (avoid switches / avoid links)
  * /api/auto switches the loss-aware adaptive rerouting monitor on and off
  * /api/state returns controller state, policy, events and the monitor log
  * /api/diagnose keeps the D1 response shape, so the old index.html still works;
    the new page static/d2.html shows the D2 features

Run (root, on the machine running Mininet, controller already started):
    sudo python3 dashboard_server_d2.py
Then open http://127.0.0.1:5000/d2.html   (or / for the D1-style page)

Mininet: start the topology with topo_d2.py, then in its CLI:
    mininet> pingall
    mininet> h2 python3 udp_server.py &
"""
import argparse
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
PROBE_SCRIPT = os.path.join(HERE, "udp_probe.py")

# Mininet interface names of the diamond built by topo_d2.py (ports are numbered
# in the order the links are added: h1-s1, h2-s4, s1-s2, s2-s4, s1-s3, s3-s4).
LINK_IFACES = {
    ("s1", "s2"): ["s1-eth2", "s2-eth1"],
    ("s2", "s4"): ["s2-eth2", "s4-eth2"],
    ("s1", "s3"): ["s1-eth3", "s3-eth1"],
    ("s3", "s4"): ["s3-eth2", "s4-eth3"],
}

CFG = {}
HISTORY = []
LOCK = threading.Lock()
KNOWN_LINKS = {k: c for k, c in
               {("s1", "s2"): 10, ("s2", "s4"): 10, ("s1", "s3"): 30, ("s3", "s4"): 30}.items()}
KNOWN_HOSTS = {}   # ip -> switch name (every host ever seen)

# adaptive monitor settings (same as d2_adaptive.py)
BURST, BURST_INTERVAL, BURST_TIMEOUT = 10, 0.1, 0.3
WINDOW_BURSTS, LOSS_THRESHOLD, RECHECK_S = 3, 10.0, 20
AUTO = {"on": False, "dst": "10.0.0.2", "log": [], "window_loss": None,
        "avoiding": False, "error": None}


def link_key(a, b):
    return tuple(sorted((a, b)))


# ---------------------------------------------------------------- controller
def ctrl_get(path):
    with urllib.request.urlopen(CFG["ctrl"] + path, timeout=3) as r:
        return json.load(r)


def ctrl_post(path, body):
    req = urllib.request.Request(CFG["ctrl"] + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


def load_topology():
    """Current topology from the controller, plus links seen before but gone now."""
    try:
        st = ctrl_get("/diag/state")
    except Exception as e:
        raise RuntimeError("cannot reach the D2 controller at %s (%s). Is ryu-manager "
                           "running with controller.py?" % (CFG["ctrl"], e))
    now = set()
    with LOCK:
        for l in st["links"]:
            k = link_key(l["a"], l["b"])
            now.add(k)
            KNOWN_LINKS[k] = l.get("cost_ms", KNOWN_LINKS.get(k, 10))
        for h in st["hosts"].values():
            if h.get("ip"):
                KNOWN_HOSTS[h["ip"]] = h["switch"]
        known = set(KNOWN_LINKS)
        hosts = [{"ip": ip, "switch": sw} for ip, sw in KNOWN_HOSTS.items()]
    switches = sorted(set(st["switches"]) | {s for k in known for s in k})
    return {"source": "controller", "switches": switches, "links": sorted(now),
            "all_links": sorted(known), "down": sorted(known - now),
            "hosts": hosts, "state": st, "note": ""}


def link_list(t):
    return [[a, b, "down" if (a, b) in t["down"] else "up"] for a, b in t["all_links"]]


def set_link(name_a, name_b, action):
    """Bring a link down/up by toggling both switch-side interfaces."""
    if action not in ("down", "up"):
        raise ValueError("action must be 'down' or 'up'")
    ifs = LINK_IFACES.get(link_key(name_a, name_b))
    if not ifs:
        raise ValueError("Unknown link %s-%s" % (name_a, name_b))
    done = []
    for i in ifs:
        try:
            r = subprocess.run(["ip", "link", "set", i, action],
                               capture_output=True, text=True)
        except OSError as e:
            raise RuntimeError("cannot run 'ip' command: %s" % e)
        if r.returncode == 0:
            done.append(i)
    if not done:
        raise RuntimeError("Could not set %s %s - run the dashboard with sudo on the "
                           "machine running Mininet." % (ifs, action))
    return {"link": [name_a, name_b], "action": action, "interfaces": done}


# -------------------------------------------------------------------- paths
def adjacency(links):
    g = {}
    for a, b in links:
        g.setdefault(a, set()).add(b)
        g.setdefault(b, set()).add(a)
    return g


def link_cost(a, b):
    return KNOWN_LINKS.get(link_key(a, b), 10)


def path_cost(p):
    return sum(link_cost(a, b) for a, b in zip(p, p[1:]))


def all_paths(links, a, b, limit=20):
    """All simple switch-level paths a->b, cheapest (lowest delay) first."""
    g = adjacency(links)
    out = []

    def dfs(node, path):
        if len(out) >= limit * 5:
            return
        if node == b:
            out.append(list(path))
            return
        for n in sorted(g.get(node, ())):
            if n not in path:
                path.append(n)
                dfs(n, path)
                path.pop()

    dfs(a, [a])
    out.sort(key=lambda p: (path_cost(p), len(p)))
    return out[:limit]


def host_switch(topo, ip):
    for h in topo["hosts"]:
        if h["ip"] == ip:
            return h["switch"]
    return None


def path_links_ids(names):
    """["s1","s2","s4"] -> [[1,2],[2,4]] (controller policy format)."""
    ids = [int(n[1:]) for n in names]
    return [[a, b] for a, b in zip(ids, ids[1:])]


# -------------------------------------------------------------------- probe
def find_mininet_pid(host):
    try:
        out = subprocess.run(["pgrep", "-f", "mininet:%s$" % host],
                             capture_output=True, text=True, timeout=3).stdout
        pids = out.split()
        return pids[0] if pids else None
    except Exception:
        return None


def run_probe(dst, count, interval=0.1, timeout=1.0, outfile="/tmp/dash_probe.json"):
    """Run udp_probe.py (from inside the Mininet host if possible)."""
    cmd = [sys.executable, PROBE_SCRIPT, dst, str(count), str(interval), str(timeout), outfile]
    where = "local"
    pid = find_mininet_pid(CFG["mn_host"]) if shutil.which("mnexec") else None
    if pid and not CFG["local_probe"]:
        cmd = ["mnexec", "-a", pid] + cmd
        where = "mininet:%s" % CFG["mn_host"]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE,
                       timeout=count * (interval + timeout) + 15)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "udp_probe.py failed")
    res = json.loads(r.stdout)
    res["probe_origin"] = where
    return res


# ----------------------------------------------------------------- analysis
def analyse(dst, count):
    probe = run_probe(dst, count)
    topo = load_topology()
    st = topo["state"]
    down = set(topo["down"])
    down_names = [list(k) for k in sorted(down)]
    avoid_nodes = set(st.get("avoid", []))
    avoid_set = {link_key(*x.split("-")) for x in st.get("avoid_links", [])}
    policy_on = bool(avoid_nodes or avoid_set)

    src_sw = host_switch(topo, CFG["src_ip"])
    dst_sw = host_switch(topo, dst)
    paths, primary, original, cost, warn = [], None, None, None, []
    if src_sw is None or dst_sw is None:
        missing = CFG["src_ip"] if src_sw is None else dst
        warn.append("Host %s not found in the controller - run pingall in Mininet so "
                    "the controller learns the hosts." % missing)
    else:
        cp = ctrl_get("/diag/path?src=%s&dst=%s" % (CFG["src_ip"], dst))
        primary, cost = cp.get("path"), cp.get("cost_ms")
        paths = all_paths(topo["all_links"], src_sw, dst_sw)
        original = paths[0] if paths else None

    loss, avg = probe["loss_pct"], probe["rtt_avg_ms"]
    route = " -> ".join(primary) if primary else "unknown path"
    rerouted = bool(primary and original and primary != original)
    predicted = 2 * cost if cost is not None else None
    cause = ""

    if src_sw and dst_sw and primary is None:
        status = "failed"
        diag = "Path FAILED - no route in the controller's topology"
        cause = ("Topology is disconnected: failed link(s) " +
                 ", ".join("-".join(x) for x in down_names) +
                 " left no route between the hosts.") if down_names else \
            "The controller has no route between the hosts."
    elif loss == 100:
        status = "failed"
        diag = "Path FAILED - no replies received"
        cause = ("The controller still has a route, but nothing came back: the UDP echo "
                 "server on the destination may not be running, or flows are stale "
                 "after a topology change.")
        if probe.get("errors"):
            cause += " Socket error: " + probe["errors"][0]
    elif loss > 0:
        status = "degraded"
        diag = "DEGRADED path - packet loss on " + route
        cause = ("Partial loss on the active path: a lossy or congested link. The "
                 "controller only reroutes on link up/down; switch on adaptive "
                 "rerouting to move traffic away from a lossy path.")
    else:
        status = "healthy"
        diag = "Path healthy"

    if rerouted and status != "failed":
        orig = " -> ".join(original)
        if down_names:
            diag += " (rerouted around failed link)"
            extra = "Preferred path %s is down; traffic now uses %s." % (orig, route)
        elif policy_on:
            diag += " (steered by controller policy)"
            extra = ("Controller policy (avoid switches %s, avoid links %s) steers "
                     "traffic off the preferred path %s onto %s." % (
                         sorted(avoid_nodes) or "none", sorted(st.get("avoid_links", [])) or "none",
                         orig, route))
        else:
            extra = "Traffic uses %s instead of the lowest-delay path %s." % (route, orig)
        cause = (cause + " " + extra).strip()

    rtt_note = None
    if predicted and avg:
        rtt_note = "Measured %.1f ms vs predicted %s ms (2 x path cost)." % (avg, predicted)

    plist = []
    for p in paths:
        links = [link_key(a, b) for a, b in zip(p, p[1:])]
        bad = [list(k) for k in links if k in down]
        avoided = any(k in avoid_set for k in links) or any(n in avoid_nodes for n in p[1:-1])
        if bad:
            st_p = "failed"
        elif p == primary:
            st_p = status
        else:
            st_p = "available"
        plist.append({
            "names": list(p), "hops": len(p), "primary": p == primary,
            "status": st_p, "down_links": bad, "avoided_by_policy": avoided,
            "cost_ms": path_cost(p),
            "measured_rtt_ms": avg if p == primary else None,
            "est_rtt_ms": 2 * path_cost(p),
        })
    plist.sort(key=lambda x: (x["status"] == "failed", x["cost_ms"], x["hops"]))

    suspects = []
    if status != "healthy":
        if down_names:
            suspects = down_names
        elif primary:
            suspects = [list(link_key(a, b)) for a, b in zip(primary, primary[1:])]

    recommend = None
    if status != "healthy":
        cands = [p for p in plist if p["status"] == "available"]
        alt = next((p for p in cands if not p["avoided_by_policy"]), cands[0] if cands else None)
        if alt:
            recommend = " -> ".join(alt["names"])
        elif down_names:
            recommend = "No working alternative - restore " + ", ".join("-".join(x) for x in down_names)
        else:
            recommend = "No alternative path exists (redundant link needed)."

    result = {
        "time": time.strftime("%H:%M:%S"),
        "src": CFG["src_ip"], "dst": dst, "probe": probe,
        "status": status, "diagnosis": diag, "cause": cause, "warnings": warn,
        "rerouted": rerouted, "down_links": down_names,
        "cost_ms": cost, "predicted_rtt_ms": predicted, "rtt_note": rtt_note,
        "policy": {"avoid": st.get("avoid", []), "avoid_links": st.get("avoid_links", [])},
        "events": st.get("events", [])[-15:],
        "topology": {
            "source": topo["source"], "note": topo["note"],
            "switches": topo["switches"], "links": link_list(topo),
            "hosts": [{"ip": h["ip"], "switch": h["switch"]} for h in topo["hosts"]],
        },
        "primary": primary, "paths": plist, "suspect_links": suspects,
        "recommend": recommend,
    }
    with LOCK:
        HISTORY.append({"time": result["time"], "dst": dst, "status": status,
                        "loss_pct": loss, "rtt_avg_ms": avg,
                        "jitter_ms": probe["jitter_ms"],
                        "path": " -> ".join(primary or []) or "n/a"})
        del HISTORY[:-50]
    return result


# ------------------------------------------------- adaptive rerouting monitor
class Monitor:
    """Moves traffic off the current path when loss over a window is too high."""

    def __init__(self):
        self.window, self.avoiding, self.t_avoid = [], False, None

    def window_loss(self):
        if len(self.window) < WINDOW_BURSTS:
            return None
        sent = sum(s for s, _ in self.window)
        lost = sum(l for _, l in self.window)
        return 100.0 * lost / sent

    def step(self, sent, lost, path, now):
        self.window = (self.window + [(sent, lost)])[-WINDOW_BURSTS:]
        loss = self.window_loss()
        if loss is not None and loss > LOSS_THRESHOLD and path:
            ctrl_post("/diag/policy", {"avoid_links": path_links_ids(path)})
            self.avoiding, self.t_avoid, self.window = True, now, []
            return "reroute away from %s (window loss %.1f%%)" % ("-".join(path), loss)
        if self.avoiding and now - self.t_avoid >= RECHECK_S and \
                not (loss is not None and loss > LOSS_THRESHOLD):
            ctrl_post("/diag/policy", {"avoid_links": []})
            self.avoiding, self.window = False, []
            return "trial: policy cleared, re-testing preferred path"
        return None


def monitor_loop():
    mon, was_on = Monitor(), False
    while True:
        try:
            if not AUTO["on"]:
                if was_on:
                    mon, was_on = Monitor(), False
                    AUTO["window_loss"], AUTO["avoiding"] = None, False
                time.sleep(0.5)
                continue
            was_on = True
            dst = AUTO["dst"]
            p = run_probe(dst, BURST, BURST_INTERVAL, BURST_TIMEOUT, "/tmp/dash_monitor.json")
            lost = p["sent"] - p["received"]
            try:
                path = ctrl_get("/diag/path?src=%s&dst=%s" % (CFG["src_ip"], dst)).get("path")
            except Exception:
                path = None
            action = mon.step(p["sent"], lost, path, time.time())
            with LOCK:
                AUTO["log"].append({"time": time.strftime("%H:%M:%S"),
                                    "path": "-".join(path) if path else "none",
                                    "loss_pct": p["loss_pct"], "rtt_avg_ms": p["rtt_avg_ms"],
                                    "action": action})
                del AUTO["log"][:-40]
                AUTO["window_loss"] = mon.window_loss()
                AUTO["avoiding"] = mon.avoiding
                AUTO["error"] = None
        except Exception as e:
            AUTO["error"] = str(e)
            time.sleep(2)


def auto_summary():
    with LOCK:
        return {"on": AUTO["on"], "dst": AUTO["dst"], "log": list(AUTO["log"]),
                "window_loss": AUTO["window_loss"], "avoiding": AUTO["avoiding"],
                "error": AUTO["error"],
                "settings": {"burst": BURST, "window_bursts": WINDOW_BURSTS,
                             "loss_threshold_pct": LOSS_THRESHOLD, "recheck_s": RECHECK_S}}


# --------------------------------------------------------------------- http
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("[web] " + fmt % args + "\n")

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/topology"):
            try:
                t = load_topology()
            except Exception as e:
                return self.send_json({"error": str(e)}, 500)
            return self.send_json({
                "source": t["source"], "note": t["note"], "src_ip": CFG["src_ip"],
                "switches": t["switches"], "links": link_list(t),
                "hosts": [{"ip": h["ip"], "switch": h["switch"]} for h in t["hosts"]]})
        if self.path.startswith("/api/history"):
            with LOCK:
                return self.send_json(HISTORY)
        if self.path.startswith("/api/auto"):
            return self.send_json(auto_summary())
        if self.path.startswith("/api/state"):
            try:
                st = ctrl_get("/diag/state")
            except Exception as e:
                return self.send_json({"error": "controller unreachable: %s" % e}, 500)
            route = None
            try:
                route = ctrl_get("/diag/path?src=%s&dst=%s" % (CFG["src_ip"], AUTO["dst"]))
            except Exception:
                pass
            return self.send_json({"controller": st, "route": route, "src_ip": CFG["src_ip"],
                                   "auto": auto_summary()})
        return super().do_GET()

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        try:
            req = self._body()
        except Exception:
            return self.send_json({"error": "Bad JSON"}, 400)
        if self.path.startswith("/api/link"):
            try:
                return self.send_json(set_link(str(req.get("a")), str(req.get("b")),
                                               str(req.get("action"))))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            except Exception as e:
                return self.send_json({"error": str(e)}, 500)
        if self.path.startswith("/api/policy"):
            body = {k: req[k] for k in ("avoid", "avoid_links") if k in req}
            try:
                return self.send_json(ctrl_post("/diag/policy", body))
            except Exception as e:
                return self.send_json({"error": str(e)}, 500)
        if self.path.startswith("/api/auto"):
            try:
                if "dst" in req:
                    AUTO["dst"] = str(ipaddress.ip_address(req["dst"]))
                if "on" in req:
                    AUTO["on"] = bool(req["on"])
                    if AUTO["on"]:
                        AUTO["log"] = []
            except Exception:
                return self.send_json({"error": "Invalid destination IP"}, 400)
            return self.send_json(auto_summary())
        if self.path.startswith("/api/reset"):
            with LOCK:
                HISTORY.clear()
                KNOWN_HOSTS.clear()
            return self.send_json({"ok": True})
        if not self.path.startswith("/api/diagnose"):
            return self.send_json({"error": "not found"}, 404)
        try:
            dst = str(ipaddress.ip_address(req.get("dst", "")))
            count = max(1, min(int(req.get("count", 20)), 200))
        except Exception:
            return self.send_json({"error": "Invalid destination IP or count"}, 400)
        try:
            self.send_json(analyse(dst, count))
        except subprocess.TimeoutExpired:
            self.send_json({"error": "Probe timed out"}, 500)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--ctrl", default="http://127.0.0.1:8080",
                    help="D2 controller REST base URL")
    ap.add_argument("--src-ip", default="10.0.0.1")
    ap.add_argument("--mn-host", default="h1",
                    help="Mininet host to send probes from (default h1)")
    ap.add_argument("--local-probe", action="store_true",
                    help="send probes from this machine, not from a Mininet host")
    a = ap.parse_args()
    CFG.update(ctrl=a.ctrl.rstrip("/"), src_ip=a.src_ip,
               mn_host=a.mn_host, local_probe=a.local_probe)
    threading.Thread(target=monitor_loop, daemon=True).start()
    print("Dashboard: http://%s:%d/d2.html  (controller: %s)" % (a.bind, a.port, CFG["ctrl"]))
    ThreadingHTTPServer((a.bind, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
