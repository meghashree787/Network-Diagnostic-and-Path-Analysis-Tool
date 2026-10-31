#!/usr/bin/env python3
"""
D2 experiment runner (replaces topo_d2.py for automated runs).

Builds the same diamond topology as topo_d2.py, then runs:
  1. fast_path       - default routing (controller picks s1-s2-s4)
  2. slow_path       - POST /diag/policy {"avoid":[2]} forces s1-s3-s4
  3. fast_restored   - POST /diag/policy {"avoid":[]} returns to fast path
  4. loss_on_fast    - 10% loss per direction on the s1-s2 link (controller still routes via it)
  5. failover        - s1-s2 link goes down mid-probe, then comes back up

Start order (same as before):
  sudo mn -c
  ryu-manager --observe-links controller.py          (in ryuenv)
  sudo /usr/bin/python3 d2_experiments.py            (this file)

Needs udp_probe.py (new version) and udp_server.py in the same folder.
Results are saved to d2_results.json next to this file.
"""
import json
import os
import statistics
import time
import urllib.request

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info

API = "http://127.0.0.1:8080"
HERE = os.path.dirname(os.path.abspath(__file__))
SRC, DST = "10.0.0.1", "10.0.0.2"
RESULTS = os.path.join(HERE, "d2_results.json")


# ---------------- controller REST helpers ----------------
def api_get(path):
    with urllib.request.urlopen(API + path, timeout=3) as r:
        return json.load(r)


def api_post(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


def controller_path():
    return api_get("/diag/path?src=%s&dst=%s" % (SRC, DST))


def set_avoid(switch_ids):
    out = api_post("/diag/policy", {"avoid": switch_ids})
    time.sleep(1.0)  # let the flush settle
    return out


# ---------------- topology ----------------
def build():
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    s1, s2, s3, s4 = [net.addSwitch('s%d' % i, protocols='OpenFlow13') for i in range(1, 5)]
    net.addLink(h1, s1)
    net.addLink(h2, s4)
    l12 = net.addLink(s1, s2, delay='10ms')   # fast path
    net.addLink(s2, s4, delay='10ms')
    net.addLink(s1, s3, delay='30ms')         # slow path
    net.addLink(s3, s4, delay='30ms')
    return net, l12


# ---------------- probing ----------------
def run_probe(h1, count=50, interval=0.1, timeout=1.0, name="probe", background=False):
    out = "/tmp/d2_%s.json" % name
    if os.path.exists(out):
        os.remove(out)
    cmd = "python3 %s/udp_probe.py %s %d %s %s %s > /dev/null 2>&1" % (
        HERE, DST, count, interval, timeout, out)
    if background:
        h1.cmd(cmd + " &")
        return out
    h1.cmd(cmd)
    return load_probe(out)


def wait_probe(out, max_wait=120):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        if os.path.exists(out):
            time.sleep(0.2)
            return load_probe(out)
        time.sleep(0.5)
    raise RuntimeError("probe did not finish: " + out)


def load_probe(path):
    with open(path) as f:
        return json.load(f)


def summarize(name, ctrl, probe):
    cost = ctrl.get("cost_ms")
    return {
        "scenario": name,
        "controller_path": ctrl.get("path"),
        "predicted_rtt_ms": 2 * cost if cost is not None else None,
        "sent": probe["sent"], "received": probe["received"],
        "timed_out": probe["sent"] - probe["received"],
        "loss_pct": probe["loss_pct"],
        "rtt_min_ms": probe["rtt_min_ms"], "rtt_avg_ms": probe["rtt_avg_ms"],
        "rtt_max_ms": probe["rtt_max_ms"], "jitter_ms": probe["jitter_ms"],
        "samples": probe["samples"],
    }


def run_scenario(h1, name, count=50):
    ctrl = controller_path()
    run_probe(h1, count=3, name="warm")           # install flows before measuring
    probe = run_probe(h1, count=count, name=name)
    s = summarize(name, ctrl, probe)
    info("*** %-14s path=%s predicted=%sms measured avg=%sms loss=%s%% jitter=%sms\n" % (
        name, "-".join(s["controller_path"] or ["none"]), s["predicted_rtt_ms"],
        s["rtt_avg_ms"], s["loss_pct"], s["jitter_ms"]))
    return s


# ---------------- failover analysis ----------------
def avg(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.mean(vals), 2) if vals else None


def analyze_failover(samples, t_down, t_up):
    lost_after_down = [s for s in samples if s["t"] >= t_down and s["rtt_ms"] is None]
    first_loss = min((s["t"] for s in lost_after_down), default=None)
    first_ok = None
    if first_loss is not None:
        first_ok = next((s for s in samples
                         if s["t"] > first_loss and s["rtt_ms"] is not None), None)
    failover_s = round(first_ok["t"] - t_down, 3) if first_ok else None
    return {
        "lost_probes_total": sum(1 for s in samples if s["rtt_ms"] is None),
        "lost_after_link_down": sum(1 for s in lost_after_down if s["t"] < t_up),
        "lost_after_link_up": sum(1 for s in lost_after_down if s["t"] >= t_up),
        "failover_time_s": failover_s,
        "rtt_before_ms": avg([s["rtt_ms"] for s in samples if s["t"] < t_down]),
        "rtt_during_failure_ms": avg([s["rtt_ms"] for s in samples
                                      if first_ok and first_ok["t"] <= s["t"] < t_up]),
        "rtt_after_restore_ms": avg([s["rtt_ms"] for s in samples if s["t"] >= t_up + 4]),
    }


def run_failover(net, h1):
    ctrl_before = controller_path()
    run_probe(h1, count=3, name="warm")
    out = run_probe(h1, count=200, interval=0.05, timeout=0.3,
                    name="failover", background=True)
    time.sleep(3)
    t_down = time.time()
    info("*** link s1-s2 DOWN\n")
    net.configLinkStatus('s1', 's2', 'down')
    time.sleep(5)
    ctrl_during = controller_path()
    t_up = time.time()
    info("*** link s1-s2 UP\n")
    net.configLinkStatus('s1', 's2', 'up')
    probe = wait_probe(out)
    time.sleep(3)
    ctrl_after = controller_path()
    result = summarize("failover", ctrl_before, probe)
    result["controller_path_during_failure"] = ctrl_during.get("path")
    result["controller_path_after_restore"] = ctrl_after.get("path")
    result["t_down"], result["t_up"] = t_down, t_up
    result["failover"] = analyze_failover(probe["samples"], t_down, t_up)
    info("*** failover: %s\n" % json.dumps(result["failover"]))
    return result


# ---------------- main ----------------
def main():
    net, l12 = build()
    net.start()
    h1, h2 = net.get('h1'), net.get('h2')
    results = []
    try:
        info("*** waiting for the controller to see the topology\n")
        time.sleep(5)
        net.ping([h1, h2])          # lets the controller learn both hosts
        net.ping([h1, h2])
        h2.cmd("python3 %s/udp_server.py > /tmp/udp_server.log 2>&1 &" % HERE)
        time.sleep(1)

        set_avoid([])
        results.append(run_scenario(h1, "fast_path"))

        set_avoid([2])              # avoid s2 -> controller must use s1-s3-s4
        results.append(run_scenario(h1, "slow_path"))

        set_avoid([])
        results.append(run_scenario(h1, "fast_restored"))

        for intf in (l12.intf1, l12.intf2):
            intf.config(delay='10ms', loss=10)
        results.append(run_scenario(h1, "loss_on_fast", count=100))
        for intf in (l12.intf1, l12.intf2):
            intf.config(delay='10ms', loss=0)
        time.sleep(1)

        results.append(run_failover(net, h1))
    finally:
        with open(RESULTS, "w") as f:
            json.dump(results, f, indent=1)
        info("*** results saved to %s\n" % RESULTS)
        print("\n%-14s %-18s %9s %9s %7s %8s" % (
            "scenario", "path", "pred RTT", "avg RTT", "loss%", "jitter"))
        for r in results:
            print("%-14s %-18s %9s %9s %7s %8s" % (
                r["scenario"], "-".join(r["controller_path"] or ["none"]),
                r["predicted_rtt_ms"], r["rtt_avg_ms"], r["loss_pct"], r["jitter_ms"]))
        if os.environ.get("D2_CLI"):
            CLI(net)
        net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
