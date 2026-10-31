#!/usr/bin/env python3
"""
D2 Network Diagnostic Report.

Correlates the UDP probe result (RTT, loss, jitter) with what the SDN controller
knows: the route it is using, that route's configured delay, any failed links,
the active routing policy and the recent controller events.

Usage (probe first, from inside Mininet, then analyse):
    mininet> h1 python3 udp_probe.py 10.0.0.2 50
    $ python3 analyzer.py                       # defaults: 10.0.0.1 -> 10.0.0.2
    $ python3 analyzer.py --dst 10.0.0.2 --events 15

Needs the D2 controller.py running (REST on 127.0.0.1:8080).
"""
import argparse
import json
import sys
import urllib.request


def get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=3) as r:
            return json.load(r)
    except Exception as e:
        sys.exit("ERROR: cannot reach the D2 controller REST API at %s (%s)\n"
                 "Is ryu-manager running with controller.py?" % (base, e))


def down_links_from_events(events):
    """Replay 'link DOWN/UP sX-sY' events to find links that are down now."""
    down = set()
    for e in events:
        text = e.get("event", "")
        if text.startswith("link DOWN "):
            down.add(text.split()[-1])
        elif text.startswith("link UP "):
            down.discard(text.split()[-1])
    return sorted(down)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--ctrl", default="http://127.0.0.1:8080")
    ap.add_argument("--src", default="10.0.0.1")
    ap.add_argument("--dst", default=None,
                    help="destination IP (default: the one in the probe file)")
    ap.add_argument("--probe-file", default="/tmp/probe_result.json")
    ap.add_argument("--events", type=int, default=8,
                    help="how many recent controller events to show")
    a = ap.parse_args()

    try:
        with open(a.probe_file) as f:
            probe = json.load(f)
    except (OSError, ValueError):
        sys.exit("ERROR: no valid %s - run udp_probe.py first "
                 "(mininet> h1 python3 udp_probe.py 10.0.0.2 50)" % a.probe_file)
    dst = a.dst or probe["dst"]

    state = get(a.ctrl, "/diag/state")
    route = get(a.ctrl, "/diag/path?src=%s&dst=%s" % (a.src, dst))
    if route.get("error"):
        sys.exit("Controller: %s" % route["error"])

    path = route.get("path")
    cost = route.get("cost_ms")
    predicted = 2 * cost if cost is not None else None
    down = down_links_from_events(state.get("events", []))
    avoid, avoid_links = state.get("avoid", []), state.get("avoid_links", [])
    loss, avg = probe["loss_pct"], probe["rtt_avg_ms"]
    sent, received = probe["sent"], probe["received"]

    print("=== Network Diagnostic Report (D2) ===")
    print("Topology: %d switches, %d links up, %d hosts known" % (
        len(state["switches"]), len(state["links"]), len(state["hosts"])))
    for l in state["links"]:
        print("  link %s-%s  configured delay %s ms" % (l["a"], l["b"], l["cost_ms"]))
    print("Links down: %s" % (", ".join(down) if down else "none"))
    print("Routing policy: avoid switches=%s, avoid links=%s" % (
        avoid or "none", avoid_links or "none"))
    print()
    print("Path %s -> %s: %s" % (a.src, dst, " -> ".join(path) if path else "NO PATH"))
    if path:
        print("Hops (switches): %d" % len(path))
        print("Controller path cost: %s ms one way, predicted RTT %s ms" % (cost, predicted))
    print()
    print("RTT min/avg/max: %s/%s/%s ms" % (
        probe["rtt_min_ms"], avg, probe["rtt_max_ms"]))
    print("Probes sent: %s" % sent)
    print("Replies received: %s" % received)
    print("Probes timed out: %s" % (sent - received))
    print("Observed loss: %s%%" % loss)
    print("Jitter: %s ms" % probe["jitter_ms"])
    if predicted and avg:
        print("Measured vs predicted RTT: %.1f ms vs %s ms (%.0f%% above prediction)" % (
            avg, predicted, 100.0 * (avg - predicted) / predicted))
    print()

    if not path:
        print("DIAGNOSIS: path FAILED - controller has no route between the hosts"
              + (" (down: %s)" % ", ".join(down) if down else ""))
    elif loss == 100:
        print("DIAGNOSIS: path FAILED - route %s exists but no replies "
              "(server down, or flows stale after a topology change)" % " -> ".join(path))
    elif loss > 0:
        print("DIAGNOSIS: DEGRADED path - %.1f%% packet loss on %s" % (
            loss, " -> ".join(path)))
        print("  The controller only reroutes on link up/down. Use the adaptive "
              "monitor (d2_adaptive.py / dashboard) or set a policy:")
        print("  POST /diag/policy {\"avoid_links\": [[a, b]]}")
    else:
        print("DIAGNOSIS: path healthy")
        if down:
            print("  (traffic is using a backup route; failed: %s)" % ", ".join(down))
        elif avoid or avoid_links:
            print("  (route is being steered by the active routing policy)")

    events = state.get("events", [])[-a.events:]
    if events:
        print()
        print("Recent controller events:")
        for e in events:
            print("  %.3f  %s" % (e["time"], e["event"]))


if __name__ == "__main__":
    main()
