#!/usr/bin/env python3
"""
Evaluation graphs for D2.  Run it OUTSIDE Mininet (no sudo needed):

    python3 plot_results.py            # reads the json files next to this script
    python3 plot_results.py ~/sdn_d2   # or give the folder explicitly

Reads  d2_results.json            (from d2_experiments.py)
       d2_adaptive_results.json   (from d2_adaptive.py)
Writes PNG files into a graphs/ folder:
  fig1_rtt_by_path.png        predicted vs measured RTT per scenario
  fig2_failover.png           RTT timeline across the s1-s2 link failure
  fig3_adaptive_timeline.png  loss and RTT per burst, static vs adaptive
  fig4_static_vs_adaptive.png total loss and average RTT, static vs adaptive

Needs matplotlib:  sudo apt install python3-matplotlib   (or pip install matplotlib)
"""
import json
import os
import statistics
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIR, "graphs")
os.makedirs(OUT, exist_ok=True)

C_STATIC, C_ADAPT, C_PRED = "#c0504d", "#2e7d5b", "#8c8c8c"


def load(name):
    path = os.path.join(DIR, name)
    if not os.path.exists(path):
        print("skipping (file not found): %s" % path)
        return None
    with open(path) as f:
        return json.load(f)


def median_rtt(samples):
    """Median of delivered probes, ignoring the first probe (flow set-up)."""
    vals = [s["rtt_ms"] for s in samples[1:] if s["rtt_ms"] is not None]
    return statistics.median(vals) if vals else None


def save(fig, name):
    path = os.path.join(OUT, name)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print("wrote", path)


# ---------------- fig 1: predicted vs measured RTT ----------------
def fig_rtt(results):
    names = ["fast_path", "slow_path", "fast_restored", "loss_on_fast"]
    rows = [r for n in names for r in results if r["scenario"] == n]
    if not rows:
        return
    labels = ["%s\n(%s)" % (r["scenario"].replace("_", " "), "-".join(r["controller_path"]))
              for r in rows]
    pred = [r["predicted_rtt_ms"] for r in rows]
    meas = [median_rtt(r["samples"]) for r in rows]
    x = range(len(rows))
    w = 0.38
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.bar([i - w / 2 for i in x], pred, w, label="predicted (2 x controller path cost)", color=C_PRED)
    ax.bar([i + w / 2 for i in x], meas, w, label="measured (median of delivered probes)", color=C_ADAPT)
    for i, r in enumerate(rows):
        ax.text(i + w / 2, (meas[i] or 0) + 2, "loss %.0f%%" % r["loss_pct"],
                ha="center", fontsize=8)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("round-trip time (ms)")
    ax.set_title("Predicted vs measured RTT per scenario")
    ax.legend(fontsize=8)
    save(fig, "fig1_rtt_by_path.png")


# ---------------- fig 2: failover timeline ----------------
def fig_failover(results):
    r = next((x for x in results if x["scenario"] == "failover"), None)
    if not r:
        return
    t0, t_up = r["t_down"], r["t_up"]
    ok = [(s["t"] - t0, s["rtt_ms"]) for s in r["samples"] if s["rtt_ms"] is not None]
    lost = [s["t"] - t0 for s in r["samples"] if s["rtt_ms"] is None]
    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.axvspan(0, t_up - t0, color="#f3d9d9", alpha=0.6, label="link s1-s2 down")
    ax.plot([a for a, _ in ok], [b for _, b in ok], ".-", ms=3, lw=0.8,
            color=C_ADAPT, label="RTT of delivered probes")
    if lost:
        ax.plot(lost, [0] * len(lost), "x", color=C_STATIC, ms=8, mew=2, label="lost probe")
    f = r.get("failover", {})
    if f.get("failover_time_s") is not None:
        ax.set_title("Link failure: traffic back after %.2f s (probe timeout limits resolution)"
                     % f["failover_time_s"], fontsize=10)
    ax.set_xlabel("time since link s1-s2 went down (s)")
    ax.set_ylabel("RTT (ms)")
    ax.text(1.5, 100, "s1-s3-s4 (slow path)", fontsize=8)
    ax.text(-2.8, 62, "s1-s2-s4 (fast path)", fontsize=8)
    ax.legend(fontsize=8, loc="center right")
    save(fig, "fig2_failover.png")


# ---------------- fig 3: adaptive timeline ----------------
def fig_adaptive_timeline(ad):
    if not ad:
        return
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    for mode, colour in (("static", C_STATIC), ("adaptive", C_ADAPT)):
        if mode not in ad:
            continue
        b = ad[mode]["bursts"]
        t = [e["t"] for e in b]
        a1.plot(t, [e["loss_pct"] for e in b], "o-", ms=3, lw=0.9, color=colour, label=mode)
        a2.plot(t, [e["rtt_avg_ms"] for e in b], "o-", ms=3, lw=0.9, color=colour, label=mode)
        for e in b:
            if e["action"] and mode == "adaptive":
                kind = "reroute" if e["action"].startswith("reroute") else "trial"
                for ax in (a1, a2):
                    ax.axvline(e["t"], ls="--" if kind == "reroute" else ":", color="k", lw=0.8)
                a1.text(e["t"], 62, kind, rotation=90, fontsize=7, va="top", ha="right")
    any_mode = next(iter(ad.values()))
    for ax in (a1, a2):
        ax.axvline(any_mode["t_fault"], color="orange", lw=1.5)
    a1.text(any_mode["t_fault"], 62, " fault injected", fontsize=7, va="top", color="darkorange")
    a1.set_ylabel("loss per burst (%)")
    a1.set_ylim(-3, 65)
    a1.set_title("Injected loss on s1-s2: static routing vs loss-aware rerouting")
    a1.legend(fontsize=8)
    a2.set_ylabel("avg RTT per burst (ms)")
    a2.set_xlabel("time (s)")
    save(fig, "fig3_adaptive_timeline.png")


# ---------------- fig 4: summary bars ----------------
def fig_adaptive_summary(ad):
    if not ad or "static" not in ad or "adaptive" not in ad:
        return
    modes = ["static", "adaptive"]
    loss = [ad[m]["loss_pct_during_fault"] for m in modes]
    rtt = [ad[m]["avg_rtt_during_fault_ms"] for m in modes]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(8, 4))
    a1.bar(modes, loss, color=[C_STATIC, C_ADAPT])
    for i, v in enumerate(loss):
        a1.text(i, v + 0.5, "%.1f%%" % v, ha="center")
    a1.set_ylabel("packet loss during fault (%)")
    a1.set_title("Loss")
    a2.bar(modes, rtt, color=[C_STATIC, C_ADAPT])
    for i, v in enumerate(rtt):
        a2.text(i, v + 1, "%.0f ms" % v, ha="center")
    a2.set_ylabel("avg RTT of delivered probes (ms)")
    a2.set_title("Latency cost")
    fig.suptitle("Trade-off: adaptive rerouting loses fewer probes but uses the slower path",
                 fontsize=10)
    save(fig, "fig4_static_vs_adaptive.png")


if __name__ == "__main__":
    res = load("d2_results.json")
    if res:
        fig_rtt(res)
        fig_failover(res)
    ad = load("d2_adaptive_results.json")
    fig_adaptive_timeline(ad)
    fig_adaptive_summary(ad)
