#!/usr/bin/env python3
"""
Loss-aware adaptive rerouting experiment (D2 dynamic SDN functionality).

The same fault is injected twice on the fast link s1-s2:
  static   - controller only reacts to link up/down, so it keeps using the lossy path
  adaptive - a monitor probes h1 -> h2 in bursts; when loss over the last
             WINDOW_BURSTS bursts exceeds LOSS_THRESHOLD it asks the controller
             (POST /diag/policy {"avoid_links": [...]}) to avoid the links of the
             current path. After RECHECK_S it clears the policy for a trial and
             re-evaluates, so a recovered path is used again.

Start order (needs the NEW controller.py with avoid_links support):
  sudo mn -c
  ryu-manager --observe-links controller.py          (in ryuenv)
  sudo /usr/bin/python3 d2_adaptive.py

Needs d2_experiments.py, udp_probe.py (new version) and udp_server.py in the same folder.
Results go to d2_adaptive_results.json.
"""
import json
import os
import statistics
import sys
import time

from mininet.log import setLogLevel, info

from d2_experiments import (build, api_get, api_post, controller_path,
                            run_probe, HERE)

BURST, INTERVAL, TIMEOUT = 10, 0.1, 0.3   # probes per burst, pacing, per-probe timeout
WINDOW_BURSTS = 3                         # decision window = last 3 bursts (30 probes)
LOSS_THRESHOLD = 10.0                     # % loss over the window that triggers a reroute
RECHECK_S = 20                            # seconds on the alternate path before a trial
HEALTHY_BURSTS = 3                        # baseline bursts before the fault
FAULT_S = 60                              # how long the fault lasts
FAULT_LOSS = 15                           # % loss per direction on s1-s2 (about 28% round trip)
RESULTS = os.path.join(HERE, "d2_adaptive_results.json")


# ---------------- helpers ----------------
def set_policy(avoid_links=None, avoid=None):
    body = {}
    if avoid is not None:
        body["avoid"] = avoid
    if avoid_links is not None:
        body["avoid_links"] = avoid_links
    api_post("/diag/policy", body)
    time.sleep(1.0)  # let the flow flush settle


def set_loss(link, loss):
    for intf in (link.intf1, link.intf2):
        intf.config(delay='10ms', loss=loss)


def path_links(path_names):
    """["s1","s2","s4"] -> [[1,2],[2,4]]"""
    ids = [int(n[1:]) for n in path_names]
    return [[a, b] for a, b in zip(ids, ids[1:])]


class Monitor:
    """Decides from burst results whether to move traffic off the current path."""

    def __init__(self):
        self.window = []        # [(sent, lost), ...] of the most recent bursts
        self.avoiding = False
        self.t_avoid = None

    def reset(self):
        self.window = []

    def window_loss(self):
        if len(self.window) < WINDOW_BURSTS:
            return None
        sent = sum(s for s, _ in self.window)
        lost = sum(l for _, l in self.window)
        return 100.0 * lost / sent

    def step(self, sent, lost, now):
        self.window = (self.window + [(sent, lost)])[-WINDOW_BURSTS:]
        loss = self.window_loss()
        if loss is not None and loss > LOSS_THRESHOLD:
            ctrl = controller_path()
            if ctrl.get("path"):
                set_policy(avoid_links=path_links(ctrl["path"]))
                self.avoiding, self.t_avoid = True, now
                self.reset()
                return "reroute away from %s (window loss %.1f%%)" % (
                    "-".join(ctrl["path"]), loss)
        elif self.avoiding and now - self.t_avoid >= RECHECK_S:
            set_policy(avoid_links=[])
            self.avoiding = False
            self.reset()
            return "trial: policy cleared, re-testing preferred path"
        return None


# ---------------- one run ----------------
def run_mode(h1, l12, adaptive):
    name = "adaptive" if adaptive else "static"
    info("*** ===== %s run =====\n" % name)
    set_policy(avoid_links=[], avoid=[])
    set_loss(l12, 0)
    run_probe(h1, count=3, name="warm")
    mon = Monitor()
    log = []
    t0 = time.time()

    def burst(phase):
        ctrl = controller_path()
        p = run_probe(h1, count=BURST, interval=INTERVAL, timeout=TIMEOUT, name="burst")
        lost = p["sent"] - p["received"]
        return {"t": round(time.time() - t0, 2), "phase": phase,
                "path": ctrl.get("path"), "sent": p["sent"], "lost": lost,
                "loss_pct": p["loss_pct"], "rtt_avg_ms": p["rtt_avg_ms"],
                "action": None}

    for _ in range(HEALTHY_BURSTS):
        log.append(burst("healthy"))

    set_loss(l12, FAULT_LOSS)
    t_fault = round(time.time() - t0, 2)
    info("*** fault injected: %d%% loss per direction on s1-s2\n" % FAULT_LOSS)
    t_end = time.time() + FAULT_S
    while time.time() < t_end:
        entry = burst("fault")
        if adaptive:
            entry["action"] = mon.step(entry["sent"], entry["lost"], time.time())
        log.append(entry)
        info("    t=%5.1fs path=%-9s loss=%5.1f%% rtt=%s %s\n" % (
            entry["t"], "-".join(entry["path"] or ["none"]), entry["loss_pct"],
            entry["rtt_avg_ms"], entry["action"] or ""))

    set_loss(l12, 0)
    set_policy(avoid_links=[])
    return summarize(name, log, t_fault)


def summarize(name, log, t_fault):
    fault = [e for e in log if e["phase"] == "fault"]
    sent = sum(e["sent"] for e in fault)
    lost = sum(e["lost"] for e in fault)
    rtts = [e["rtt_avg_ms"] for e in fault if e["rtt_avg_ms"] is not None]
    reroutes = [e for e in fault if e["action"] and e["action"].startswith("reroute")]
    return {
        "mode": name,
        "fault_loss_per_direction_pct": FAULT_LOSS,
        "fault_duration_s": FAULT_S,
        "probes_sent_during_fault": sent,
        "probes_lost_during_fault": lost,
        "loss_pct_during_fault": round(100.0 * lost / sent, 2) if sent else None,
        "avg_rtt_during_fault_ms": round(statistics.mean(rtts), 2) if rtts else None,
        "reroute_count": len(reroutes),
        "time_to_first_reroute_s": (round(reroutes[0]["t"] - t_fault, 1)
                                    if reroutes else None),
        "t_fault": t_fault,
        "bursts": log,
    }


# ---------------- main ----------------
def main():
    net, l12 = build()
    net.start()
    h1, h2 = net.get('h1'), net.get('h2')
    results = {}
    try:
        info("*** waiting for the controller to see the topology\n")
        time.sleep(5)
        net.ping([h1, h2])
        net.ping([h1, h2])
        try:
            state = api_get("/diag/state")
        except Exception as e:
            sys.exit("Cannot reach the controller REST API: %s" % e)
        if "avoid_links" not in state:
            sys.exit("The running controller has no avoid_links support - "
                     "replace controller.py with the new version and restart it.")
        h2.cmd("python3 %s/udp_server.py > /tmp/udp_server.log 2>&1 &" % HERE)
        time.sleep(1)
        for adaptive in (False, True):
            r = run_mode(h1, l12, adaptive)
            results[r["mode"]] = r
            time.sleep(3)
    finally:
        with open(RESULTS, "w") as f:
            json.dump(results, f, indent=1)
        info("*** results saved to %s\n" % RESULTS)
        print("\n%-9s %12s %12s %14s %9s %14s" % (
            "mode", "probes sent", "probes lost", "loss% (fault)", "reroutes",
            "avg RTT (ms)"))
        for r in results.values():
            print("%-9s %12s %12s %14s %9s %14s" % (
                r["mode"], r["probes_sent_during_fault"], r["probes_lost_during_fault"],
                r["loss_pct_during_fault"], r["reroute_count"],
                r["avg_rtt_during_fault_ms"]))
        if "adaptive" in results:
            print("time to first reroute: %s s" % results["adaptive"]["time_to_first_reroute_s"])
        net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
