#!/usr/bin/env python3
"""
Web dashboard backend for the UDP Network Diagnostic & Path Analysis Tool.

Uses only the Python standard library.  It:
  * runs your existing udp_probe.py (UDP probes -> RTT, loss, jitter)
  * reads the SDN topology from the Ryu REST API (same one analyzer.py uses)
  * correlates the probe with the network path (BFS, same as analyzer.py)
  * enumerates ALTERNATIVE paths and compares them
  * serves the front end in ./static

Run (root namespace, so it can reach Ryu on 127.0.0.1:8080):
    sudo python3 dashboard_server.py
Then open  http://127.0.0.1:5000
"""
import argparse, ipaddress, json, os, shutil, subprocess, sys, threading, time
import urllib.request
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
PROBE_SCRIPT = os.path.join(HERE, "udp_probe.py")

# Topology identical to topo.py, used only if Ryu is not reachable.
DEMO = {
    "switches": ["0000000000000001", "0000000000000002", "0000000000000003"],
    "links": [("0000000000000001", "0000000000000002"),
              ("0000000000000002", "0000000000000003")],
    "hosts": [{"ip": "10.0.0.1", "dpid": "0000000000000001"},
              {"ip": "10.0.0.3", "dpid": "0000000000000002"},
              {"ip": "10.0.0.2", "dpid": "0000000000000003"}],
}

CFG = {}
HISTORY = []
LOCK = threading.Lock()
KNOWN_LINKS = {}   # (dpidA, dpidB) -> {dpid: interface name}  (every link ever seen)
KNOWN_HOSTS = {}   # ip -> dpid  (every host ever seen)


def short(d):
    return "s" + str(int(d, 16))


# ---------------------------------------------------------------- topology
def ryu_get(path):
    with urllib.request.urlopen(f"{CFG['ryu']}/{path}", timeout=2) as r:
        return json.load(r)


def _ifname(end, dpid):
    return end.get("name") or f"{short(dpid)}-eth{int(str(end.get('port_no', 1)))}"


def load_topology():
    """Current topology + links that were seen before but are gone now."""
    try:
        switches = [s["dpid"] for s in ryu_get("switches")]
        if not switches:
            raise ValueError("Ryu returned no switches")
        now = set()
        with LOCK:
            for l in ryu_get("links"):
                a, b = l["src"]["dpid"], l["dst"]["dpid"]
                k = tuple(sorted((a, b)))
                now.add(k)
                KNOWN_LINKS[k] = {a: _ifname(l["src"], a), b: _ifname(l["dst"], b)}
            for h in ryu_get("hosts"):
                for ip in h.get("ipv4", []):
                    KNOWN_HOSTS[ip] = h["port"]["dpid"]
            known = set(KNOWN_LINKS)
            hosts = [{"ip": ip, "dpid": d} for ip, d in KNOWN_HOSTS.items()]
        return {"source": "ryu", "switches": sorted(set(switches) |
                {d for k in known for d in k}),
                "links": sorted(now), "all_links": sorted(known),
                "down": sorted(known - now), "hosts": hosts, "note": ""}
    except Exception as e:
        d = DEMO
        links = sorted(tuple(sorted(l)) for l in d["links"])
        return {"source": "demo", "switches": d["switches"], "links": links,
                "all_links": links, "down": [], "hosts": d["hosts"],
                "note": f"Ryu controller not reachable ({e}). Showing the "
                        f"built-in topology from topo.py."}


def link_list(t):
    return [[short(a), short(b), "down" if (a, b) in t["down"] else "up"]
            for a, b in t["all_links"]]


def set_link(name_a, name_b, action):
    """Bring a link down/up by toggling both switch-side interfaces."""
    if action not in ("down", "up"):
        raise ValueError("action must be 'down' or 'up'")
    load_topology()
    with LOCK:
        key = next((k for k in KNOWN_LINKS
                    if {short(k[0]), short(k[1])} == {name_a, name_b}), None)
        ifs = list(KNOWN_LINKS[key].values()) if key else []
    if not key:
        raise ValueError(f"Unknown link {name_a}-{name_b} "
                         "(only works with the live Ryu topology)")
    done = []
    for i in ifs:
        try:
            r = subprocess.run(["ip", "link", "set", i, action],
                               capture_output=True, text=True)
        except OSError as e:
            raise RuntimeError(f"cannot run 'ip' command: {e}")
        if r.returncode == 0:
            done.append(i)
    if not done:
        raise RuntimeError(f"Could not set {ifs} {action} - run the dashboard "
                           f"with sudo from the machine running Mininet.")
    return {"link": [name_a, name_b], "action": action, "interfaces": done}


def build_graph(topo):
    g = {s: set() for s in topo["switches"]}
    for a, b in topo["links"]:
        g[a].add(b)
        g[b].add(a)
    return g


def host_switch(topo, ip):
    for h in topo["hosts"]:
        if h["ip"] == ip:
            return h["dpid"]
    return None


def all_paths(g, a, b, limit=20):
    """All simple switch-level paths a->b, shortest first (DFS)."""
    out = []

    def dfs(node, path):
        if len(out) >= limit * 5:
            return
        if node == b:
            out.append(list(path))
            return
        for n in sorted(g[node]):
            if n not in path:
                path.append(n)
                dfs(n, path)
                path.pop()
    dfs(a, [a])
    out.sort(key=len)
    return out[:limit]


def bfs(g, a, b):
    q, seen = deque([[a]]), {a}
    while q:
        p = q.popleft()
        if p[-1] == b:
            return p
        for n in g[p[-1]]:
            if n not in seen:
                seen.add(n)
                q.append(p + [n])
    return None


# ------------------------------------------------------------------- probe
def find_mininet_pid(host):
    try:
        out = subprocess.run(["pgrep", "-f", f"mininet:{host}$"],
                             capture_output=True, text=True, timeout=3).stdout
        pids = out.split()
        return pids[0] if pids else None
    except Exception:
        return None


def run_probe(dst, count):
    """Run udp_probe.py (from inside the Mininet host if possible)."""
    cmd = [sys.executable, PROBE_SCRIPT, dst, str(count)]
    where = "local"
    pid = find_mininet_pid(CFG["mn_host"]) if shutil.which("mnexec") else None
    if pid and not CFG["local_probe"]:
        cmd = ["mnexec", "-a", pid] + cmd
        where = f"mininet:{CFG['mn_host']}"
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE,
                       timeout=count * 1.3 + 15)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "udp_probe.py failed")
    res = json.loads(r.stdout)
    res["probe_origin"] = where
    return res


# ---------------------------------------------------------------- analysis
def analyse(dst, count):
    probe = run_probe(dst, count)
    topo = load_topology()
    g_now = build_graph(topo)                       # links that are up right now
    g_all = build_graph({"switches": topo["switches"], "links": topo["all_links"]})
    down = set(topo["down"])
    down_names = [[short(a), short(b)] for a, b in sorted(down)]
    src_sw = host_switch(topo, CFG["src_ip"])
    dst_sw = host_switch(topo, dst)

    paths, primary, original, warn = [], None, None, []
    if src_sw is None or dst_sw is None:
        missing = CFG["src_ip"] if src_sw is None else dst
        warn.append(f"Host {missing} not found in topology - run pingall "
                    f"in Mininet so the controller learns the hosts.")
    else:
        primary = bfs(g_now, src_sw, dst_sw)        # path available now
        original = bfs(g_all, src_sw, dst_sw)       # path when nothing is down
        paths = all_paths(g_all, src_sw, dst_sw)

    loss, avg = probe["loss_pct"], probe["rtt_avg_ms"]
    route = " -> ".join(short(d) for d in primary) if primary else "unknown path"
    rerouted = bool(primary and original and primary != original)
    cause = ""
    if src_sw and dst_sw and primary is None:
        status = "failed"
        diag = "Path FAILED - no route in the topology"
        cause = ("Topology is disconnected: failed link(s) " +
                 ", ".join("-".join(x) for x in down_names) +
                 " left no route between the hosts.") if down_names else \
                "No route between the hosts in the controller's topology."
    elif loss == 100:
        status = "failed"
        diag = "Path FAILED - no replies received"
        cause = ("Controller still sees a route, but nothing came back: the "
                 "destination UDP echo server may be down, or the switches "
                 "are not forwarding (flows missing / stale after a failure).")
        if probe.get("errors"):
            cause += " Socket error: " + probe["errors"][0]
    elif loss > 0:
        status = "degraded"
        diag = "DEGRADED path - packet loss on " + route
        cause = "Partial loss on the active path: congestion, a lossy link, or a link that is flapping."
    else:
        status = "healthy"
        diag = "Path healthy"
    if rerouted and status != "failed":
        diag += " (rerouted around failed link)"
        cause = ("Original path " + " -> ".join(short(d) for d in original) +
                 " is down; traffic now uses " + route + ".")

    # Per-link RTT estimate (assumes equal delay on every inter-switch link).
    per_link = None
    if primary and len(primary) > 1 and probe["rtt_min_ms"] is not None:
        per_link = probe["rtt_min_ms"] / (len(primary) - 1)

    plist = []
    for p in paths:
        bad = [[short(a), short(b)] for a, b in zip(p, p[1:])
               if tuple(sorted((a, b))) in down]
        is_primary = (p == primary)
        if bad:
            st = "failed"
        elif is_primary:
            st = status
        else:
            st = "available"
        plist.append({
            "names": [short(d) for d in p], "hops": len(p), "primary": is_primary,
            "status": st, "down_links": bad,
            "measured_rtt_ms": avg if is_primary else None,
            "est_rtt_ms": round(per_link * (len(p) - 1), 2) if per_link else None,
        })
    plist.sort(key=lambda x: (x["status"] == "failed", x["hops"]))

    suspects = []
    if status != "healthy":
        if down_names:
            suspects = down_names
        elif primary:
            suspects = [[short(a), short(b)] for a, b in zip(primary, primary[1:])]
    recommend = None
    if status != "healthy":
        alt = next((p for p in plist if p["status"] == "available"), None)
        if alt:
            recommend = " -> ".join(alt["names"])
        elif down_names:
            recommend = "No working alternative - restore " + \
                        ", ".join("-".join(x) for x in down_names)
        else:
            recommend = "No alternative path exists (redundant link needed)."

    result = {
        "time": time.strftime("%H:%M:%S"),
        "src": CFG["src_ip"], "dst": dst, "probe": probe,
        "status": status, "diagnosis": diag, "cause": cause, "warnings": warn,
        "rerouted": rerouted, "down_links": down_names,
        "topology": {
            "source": topo["source"], "note": topo["note"],
            "switches": [short(s) for s in topo["switches"]],
            "links": link_list(topo),
            "hosts": [{"ip": h["ip"], "switch": short(h["dpid"])}
                      for h in topo["hosts"]],
        },
        "primary": [short(d) for d in primary] if primary else None,
        "paths": plist, "suspect_links": suspects, "recommend": recommend,
    }
    with LOCK:
        HISTORY.append({"time": result["time"], "dst": dst, "status": status,
                        "loss_pct": loss, "rtt_avg_ms": avg,
                        "jitter_ms": probe["jitter_ms"],
                        "path": " -> ".join(result["primary"] or []) or "n/a"})
        del HISTORY[:-50]
    return result


# -------------------------------------------------------------------- http
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
            t = load_topology()
            return self.send_json({
                "source": t["source"], "note": t["note"],
                "src_ip": CFG["src_ip"],
                "switches": [short(s) for s in t["switches"]],
                "links": link_list(t),
                "hosts": [{"ip": h["ip"], "switch": short(h["dpid"])}
                          for h in t["hosts"]]})
        if self.path.startswith("/api/history"):
            with LOCK:
                return self.send_json(HISTORY)
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
        if self.path.startswith("/api/reset"):
            with LOCK:
                KNOWN_LINKS.clear(); KNOWN_HOSTS.clear(); HISTORY.clear()
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
    ap.add_argument("--ryu", default="http://127.0.0.1:8080/v1.0/topology")
    ap.add_argument("--src-ip", default="10.0.0.1")
    ap.add_argument("--mn-host", default="h1",
                    help="Mininet host to send probes from (default h1)")
    ap.add_argument("--local-probe", action="store_true",
                    help="send probes from this machine, not from a Mininet host")
    a = ap.parse_args()
    CFG.update(ryu=a.ryu.rstrip("/"), src_ip=a.src_ip,
               mn_host=a.mn_host, local_probe=a.local_probe)
    print(f"Dashboard: http://{a.bind}:{a.port}   (Ryu API: {CFG['ryu']})")
    ThreadingHTTPServer((a.bind, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
