#!/usr/bin/env python3
"""
plots_v4.py — Print-size figures and LaTeX tables for the ICC 2027 testbed paper.

Provides the figure style and the figure/table functions that
analysis/plots_paper_v3.py uses for the paper (results_v4). Run on its own:
  * reads one summary.csv as Ryu and, optionally, a second one as OS-Ken; it
    does not filter by the controller column (use plots_paper_v3.py for
    results_v4, which holds all three controllers)
  * figures are drawn at IEEE single-column print size (3.5 in wide, 8 pt fonts),
    so LaTeX does not shrink them
  * error bars are 95% confidence intervals of the mean (t-distribution),
    clipped at zero so they never go negative
  * labels use "Two-tier tree" (the topology id in the data is still "fattree")
  * smoke-test rows and runs below a success-rate threshold are excluded (reported)
  * controller CPU uses avg_cpu_percent_nofirst from an optional per-run CPU
    file (columns run_id, avg_cpu_percent_nofirst; --cpu / --osken-cpu), else
    avg_cpu_percent with a warning. plots_paper_v3.py uses cpu_percent_traffic.

Outputs (in --outdir):
  fig_latency_vs_fps.{pdf,png}        p50/p95 handler time vs flow rate at fixed N (default 64)
  fig_scalability_vs_hosts.{pdf,png}  p95 handler time + Packet-In rate vs N (2 panels, stacked)
  table_hosts.tex                     Table III body: per topology and N
  table_osken.tex                     Ryu vs OS-Ken table body (if --osken given)
  paper_numbers.txt                   values for the [TBD] markers in the paper

Usage:
  python3 analysis/plots_v4.py \
      --summary <summary.csv> [--osken <summary.csv>] --outdir <dir>
  (paper figures: analysis/plots_paper_v3.py)
"""

import argparse
import math
import os
import warnings

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

warnings.filterwarnings("ignore")

# ── Style (print size: IEEE column = 3.5 in) ─────────────────────────────────
COL_W = 3.5
plt.rcParams.update({
    "font.family":      "DejaVu Sans",
    "font.size":        8,
    "axes.titlesize":   8,
    "axes.labelsize":   8,
    "legend.fontsize":  7,
    "xtick.labelsize":  7,
    "ytick.labelsize":  7,
    "lines.linewidth":  1.2,
    "lines.markersize": 4,
    "figure.dpi":       150,
    "savefig.dpi":      300,
    "savefig.bbox":     "tight",
    "savefig.pad_inches": 0.02,
    "axes.grid":        True,
    "grid.alpha":       0.3,
    "grid.linestyle":   "--",
    "pdf.fonttype":     42,   # embed TrueType fonts (IEEE PDF eXpress friendly)
    "ps.fonttype":      42,
})

TOPOS      = ["star", "fattree"]
COLORS     = {"star": "#1f77b4", "fattree": "#d62728"}
MARKERS    = {"star": "o",       "fattree": "s"}
TOPO_LABEL = {"star": "Star",    "fattree": "Two-tier tree"}
TOPO_SHORT = {"star": "Star",    "fattree": "Tree"}

FPS_TICKS   = [25, 50, 100, 200]
HOST_TICKS  = [4, 8, 16, 32, 64]

# two-sided 95% t critical values, df = 1..30 (df > 30 -> 1.96)
_T975 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
         2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
         2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]


def t975(df):
    if df < 1:
        return float("nan")
    return _T975[df - 1] if df <= 30 else 1.96


# ── Data ─────────────────────────────────────────────────────────────────────

CPU_COL = "cpu_pct"   # controller CPU used for all CPU statistics (see load_cpu)


def load_cpu(df, cpu_path, controller):
    """Adds CPU_COL: avg_cpu_percent_nofirst from the per-run CPU file, or
    avg_cpu_percent (with a warning) where the file or a run is missing."""
    if cpu_path and os.path.exists(cpu_path):
        cpu = pd.read_csv(cpu_path, usecols=["run_id", "avg_cpu_percent_nofirst"])
        df = df.merge(cpu, on="run_id", how="left")
        miss = df["avg_cpu_percent_nofirst"].isna()
        if miss.any():
            print(f"  [WARN] {controller}: {int(miss.sum())} run(s) not in {cpu_path}; "
                  f"using avg_cpu_percent for them")
        df[CPU_COL] = df["avg_cpu_percent_nofirst"].fillna(df["avg_cpu_percent"])
        print(f"  {controller}: CPU from {cpu_path} (first sample dropped)")
    else:
        print(f"  [WARN] {controller}: {cpu_path} not found; CPU statistics use "
              f"avg_cpu_percent (for results_v4, the CPU column to use is "
              f"cpu_percent_traffic, as in analysis/plots_paper_v3.py)")
        df[CPU_COL] = df["avg_cpu_percent"]
    return df


def load_v2(path, controller, min_success, cpu_path=None):
    df = pd.read_csv(path)
    n0 = len(df)
    df = df[~df["run_id"].astype(str).str.startswith("SMOKE")].copy()
    n_smoke = n0 - len(df)
    low = df["success_rate"] < min_success
    n_low = int(low.sum())
    if n_low:
        print(f"  [WARN] {controller}: {n_low} run(s) below success_rate {min_success} excluded:")
        print("         " + ", ".join(df.loc[low, "run_id"].astype(str).head(20)))
    df = df[~low].copy()
    df["controller"] = controller
    df["tput_mbps"] = df["delivered_throughput_bps"] / 1e6
    df = load_cpu(df, cpu_path, controller)
    print(f"  {controller}: {len(df)} runs used "
          f"({n_smoke} smoke rows and {n_low} low-success rows excluded)")
    return df


def check_completeness(df, controller, reps_expected):
    cnt = df.groupby(["topology", "n_hosts", "flows_per_sec"]).size()
    short = cnt[cnt < reps_expected]
    if len(short):
        print(f"  [INFO] {controller}: {len(short)} config(s) have fewer than "
              f"{reps_expected} reps (sweep still running?):")
        for (topo, n, f), c in short.head(10).items():
            print(f"         {topo} N={n} FPS={f}: {c}")


def mean_ci(values):
    v = np.asarray(values, dtype=float)
    v = v[~np.isnan(v)]
    n = len(v)
    if n == 0:
        return float("nan"), float("nan"), 0
    m = v.mean()
    if n == 1:
        return m, 0.0, 1
    half = t975(n - 1) * v.std(ddof=1) / math.sqrt(n)
    return m, half, n


def series(df, xcol, ycol):
    """Mean and 95% CI half-width of ycol per x value; lower bar clipped at 0."""
    xs, ms, lo, hi = [], [], [], []
    for x, g in df.groupby(xcol):
        m, h, _ = mean_ci(g[ycol])
        xs.append(x); ms.append(m)
        lo.append(min(h, m) if m >= 0 else h)   # never below zero
        hi.append(h)
    return np.array(xs), np.array(ms), np.array([lo, hi])


def save(fig, outdir, name):
    for ext in ("pdf", "png"):
        path = os.path.join(outdir, f"{name}.{ext}")
        fig.savefig(path)
        print(f"  Saved: {path}")
    plt.close(fig)


def subset_n(df, n_sel):
    return df if n_sel == "all" else df[df["n_hosts"] == int(n_sel)]


# ── Figure: p50/p95 handler processing time vs flow rate ────────────────────

def fig_latency_vs_fps(df, outdir, n_sel):
    fig, ax = plt.subplots(figsize=(COL_W, 2.2))
    d = subset_n(df, n_sel)
    for topo in TOPOS:
        sub = d[d["topology"] == topo]
        for metric, ls in [("proc_latency_p95_ms", "--"), ("proc_latency_p50_ms", "-")]:
            x, m, e = series(sub, "flows_per_sec", metric)
            ax.errorbar(x, m, yerr=e, color=COLORS[topo], marker=MARKERS[topo],
                        linestyle=ls, capsize=2, capthick=0.8, elinewidth=0.8)
    ax.set_xscale("log", base=2)
    ax.set_xticks(FPS_TICKS); ax.set_xticklabels([str(v) for v in FPS_TICKS])
    ax.minorticks_off()
    ax.set_xlabel("Flow rate (flows/s)")
    ax.set_ylabel("Handler processing time (ms)")
    ax.set_ylim(bottom=0)
    handles = [Line2D([0], [0], color=COLORS[t], marker=MARKERS[t], linestyle="-",
                      label=TOPO_LABEL[t]) for t in TOPOS]
    handles += [Line2D([0], [0], color="black", linestyle="--", label="p95"),
                Line2D([0], [0], color="black", linestyle="-", label="p50")]
    ax.legend(handles=handles, ncol=4, loc="lower center", bbox_to_anchor=(0.5, 1.0),
              frameon=False, handlelength=1.8, columnspacing=1.0)
    fig.tight_layout()
    save(fig, outdir, "fig_latency_vs_fps")


# ── Figure: scalability vs host count (2 stacked panels) ────────────────────

def fig_scalability_vs_hosts(df, outdir):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(COL_W, 3.6), sharex=True)
    for topo in TOPOS:
        sub = df[df["topology"] == topo]
        for ax, metric in [(ax1, "proc_latency_p95_ms"), (ax2, "avg_packet_in_rate")]:
            x, m, e = series(sub, "n_hosts", metric)
            ax.errorbar(x, m, yerr=e, color=COLORS[topo], marker=MARKERS[topo],
                        label=TOPO_LABEL[topo], capsize=2, capthick=0.8, elinewidth=0.8)
    ax1.set_ylabel("p95 handler time (ms)")
    ax2.set_ylabel("Packet-In rate (pkt/s)")
    ax2.set_xlabel("Number of hosts ($N$)")
    for ax in (ax1, ax2):
        ax.set_xscale("log", base=2)
        ax.set_xticks(HOST_TICKS); ax.set_xticklabels([str(v) for v in HOST_TICKS])
        ax.minorticks_off()
        ax.set_ylim(bottom=0)
    ax1.text(0.01, 0.95, "(a)", transform=ax1.transAxes, va="top", fontweight="bold")
    ax2.text(0.01, 0.95, "(b)", transform=ax2.transAxes, va="top", fontweight="bold")
    ax1.legend(loc="upper left", bbox_to_anchor=(0.06, 1.0))
    fig.tight_layout(h_pad=0.4)
    save(fig, outdir, "fig_scalability_vs_hosts")


# ── Tables (LaTeX bodies, ready to paste between \midrule ... \bottomrule) ───

def fmt(v, nd):
    return "--" if (v is None or (isinstance(v, float) and math.isnan(v))) else f"{v:.{nd}f}"


def table_hosts(df, outdir):
    lines = []
    for i, topo in enumerate(["fattree", "star"]):
        if i:
            lines.append("\\midrule")
        for n in HOST_TICKS:
            g = df[(df["topology"] == topo) & (df["n_hosts"] == n)]
            if g.empty:
                continue
            lines.append(f"{TOPO_SHORT[topo]} & {n} & {fmt(g['tput_mbps'].mean(),1)} & "
                         f"{fmt(g['avg_packet_in_rate'].mean(),2)} & "
                         f"{fmt(g['proc_latency_p95_ms'].mean(),2)} & "
                         f"{fmt(g[CPU_COL].mean(),2)} \\\\")
    path = os.path.join(outdir, "table_hosts.tex")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  Saved: {path}")


def table_osken(both, outdir):
    lines = []
    for ctrl in ["Ryu", "OS-Ken"]:
        for topo in TOPOS:
            g = both[(both["controller"] == ctrl) & (both["topology"] == topo)]
            if g.empty:
                continue
            p95, h, _ = mean_ci(g["proc_latency_p95_ms"])
            lines.append(f"{ctrl} & {TOPO_SHORT[topo]} & {fmt(p95,2)}\\,$\\pm$\\,{fmt(h,2)} & "
                         f"{fmt(g['avg_packet_in_rate'].mean(),1)} & "
                         f"{fmt(g[CPU_COL].mean(),1)} & "
                         f"{fmt(g['avg_mem_mb'].mean(),1)} \\\\")
    path = os.path.join(outdir, "table_osken.tex")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  Saved: {path}")


# ── Numbers for the [TBD] markers ────────────────────────────────────────────

def paper_numbers(ryu, osken, outdir):
    out = []
    w = out.append
    w("== Validation (Ryu) ==")
    w(f"min success_rate: {ryu['success_rate'].min():.4f}")
    dev = (ryu["achieved_fps"] - ryu["flows_per_sec"]).abs() / ryu["flows_per_sec"] * 100
    w(f"max achieved-FPS deviation: {dev.max():.2f}%")
    w("")
    w("== Delivered throughput (Mbps), mean by topology x FPS ==")
    w(ryu.groupby(["topology", "flows_per_sec"])["tput_mbps"].mean().unstack().round(1).to_string())
    w("")
    w("== Packet-In rate, mean by topology x FPS ==")
    pi = ryu.groupby(["topology", "flows_per_sec"])["avg_packet_in_rate"].mean().unstack()
    w(pi.round(2).to_string())
    if {"star", "fattree"} <= set(pi.index):
        w(f"tree/star Packet-In ratio by FPS: {(pi.loc['fattree'] / pi.loc['star']).round(2).to_dict()}")
    w("")
    for metric in ["proc_latency_p50_ms", "proc_latency_p95_ms"]:
        w(f"== {metric}, mean by topology x FPS ==")
        w(ryu.groupby(["topology", "flows_per_sec"])[metric].mean().unstack().round(3).to_string())
        w("")
    w("== p95 by topology x N (mean over FPS and reps) ==")
    p95n = ryu.groupby(["topology", "n_hosts"])["proc_latency_p95_ms"].mean().unstack()
    w(p95n.round(3).to_string())
    for topo in p95n.index:
        r = p95n.loc[topo]
        if 4 in r and 64 in r and r[4] > 0:
            w(f"{topo}: N=4 -> N=64 growth {r[64] / r[4]:.1f}x")
    if {"star", "fattree"} <= set(p95n.index) and 64 in p95n.columns:
        w(f"star/tree p95 ratio at N=64: {p95n.loc['star', 64] / p95n.loc['fattree', 64]:.2f}x")
    w("")
    w("== Controller CPU (%) ==")
    w(f"max per-run mean CPU: {ryu[CPU_COL].max():.2f}")
    w(ryu.groupby(["topology", "n_hosts"])[CPU_COL].mean().unstack().round(2).to_string())
    if osken is not None and len(osken):
        w("")
        w("== Ryu vs OS-Ken ==")
        for col in ["avg_mem_mb", CPU_COL, "proc_latency_p95_ms", "avg_packet_in_rate"]:
            a, ha, _ = mean_ci(ryu[col]); b, hb, _ = mean_ci(osken[col])
            label = "avg_cpu_percent (first sample dropped)" if col == CPU_COL else col
            w(f"{label}: Ryu {a:.2f} (+/-{ha:.2f})  OS-Ken {b:.2f} (+/-{hb:.2f})  "
              f"OS-Ken vs Ryu {((b - a) / a * 100) if a else float('nan'):+.1f}%")
    path = os.path.join(outdir, "paper_numbers.txt")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    print(f"  Saved: {path}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", default="results_v4/summary/summary.csv")
    ap.add_argument("--osken",   default=None, help="second summary.csv, plotted as OS-Ken (optional)")
    ap.add_argument("--outdir",  default="results/figures_v4")
    ap.add_argument("--cpu", default=None,
                    help="per-run CPU file for Ryu (optional; default: looked up next to --summary)")
    ap.add_argument("--osken-cpu", default=None,
                    help="per-run CPU file for OS-Ken (optional; default: looked up next to --osken)")
    ap.add_argument("--min-success", type=float, default=0.98)
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--fps-n", default="64",
                    help="host count for the latency-vs-FPS figure (default 64), or 'all' to pool all N "
                         "(pooling makes error bars reflect differences between N, not run-to-run variability)")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    print(f"Loading Ryu data: {args.summary}")
    ryu_cpu = args.cpu or os.path.join(os.path.dirname(args.summary), "cpu_recomputed.csv")
    ryu = load_v2(args.summary, "Ryu", args.min_success, ryu_cpu)
    check_completeness(ryu, "Ryu", args.reps)
    osken = None
    if args.osken and os.path.exists(args.osken):
        print(f"Loading OS-Ken data: {args.osken}")
        osken_cpu = args.osken_cpu or os.path.join(os.path.dirname(args.osken), "cpu_recomputed.csv")
        osken = load_v2(args.osken, "OS-Ken", args.min_success, osken_cpu)
        check_completeness(osken, "OS-Ken", args.reps)
    print()

    print("Generating figures and tables...")
    fig_latency_vs_fps(ryu, args.outdir, args.fps_n)
    fig_scalability_vs_hosts(ryu, args.outdir)
    table_hosts(ryu, args.outdir)
    if osken is not None and len(osken):
        table_osken(pd.concat([ryu, osken]), args.outdir)
    paper_numbers(ryu, osken, args.outdir)
    print(f"\nDone. Outputs in {args.outdir}/")


if __name__ == "__main__":
    main()
