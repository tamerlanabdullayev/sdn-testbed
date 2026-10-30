#!/usr/bin/env python3
"""
plots_paper_v3.py — the paper's Ryu figures and tables from results_v4, drawn
with plots_v4.py's code (same style: 3.5 in, 8 pt, 95% t confidence intervals).

Ryu rows only (column controller == "ryu"); latency is handler time
(proc_latency_p50_ms / proc_latency_p95_ms), not wire latency. CPU is
cpu_percent_traffic: the controller's mean CPU over the traffic window
(traffic_launch start -> client_drain end); avg_cpu_percent (measure start ->
last monitor sample) is also listed in paper_numbers.txt for reference.

Code version: every row carries code_hash (scripts/code_hash.py). The script
warns if the summary (all controllers) holds more than one code_hash, or rows
without one, and lists the values in paper_numbers.txt.

Outputs (in --outdir):
  fig_latency_vs_fps.{pdf,png}        p50 (solid) / p95 (dashed) vs flow rate at N=--fps-n
  fig_scalability_vs_hosts.{pdf,png}  (a) p95, (b) Packet-In rate vs N, all FPS pooled
  table_hosts.tex                     per topology and N
  paper_numbers.txt

Usage:
  venv_py310/bin/python analysis/plots_paper_v3.py \
      --summary results_v4/summary/summary.csv --outdir results_v4/figures_paper
"""

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plots_v4 as P  # noqa: E402


def code_hashes(path):
    """code_hash value -> {controller: rows} over the whole summary
    ("" = rows without a code_hash, e.g. data from before the column)."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df = df[~df["run_id"].str.startswith("SMOKE")]
    col = df["code_hash"] if "code_hash" in df else pd.Series("", index=df.index)
    out = {}
    for (h, c), n in df.groupby([col, df["controller"]]).size().items():
        out.setdefault(h, {})[c] = int(n)
    if len(out) > 1 or "" in out:
        print(f"  [WARN] the summary holds {len(out)} code versions; runs with different "
              f"code_hash ran different code:", file=sys.stderr)
        for h, per in out.items():
            print(f"         {h or '(no code_hash)'}: {per}", file=sys.stderr)
    else:
        print(f"  code_hash: one value for all rows ({next(iter(out))})")
    return out


def load_ryu(path, min_success):
    df = pd.read_csv(path)
    df = df[(df["controller"] == "ryu") & ~df["run_id"].astype(str).str.startswith("SMOKE")].copy()
    low = df["success_rate"] < min_success
    if low.any():
        print(f"  [WARN] {int(low.sum())} Ryu run(s) below success_rate {min_success} excluded: "
              + ", ".join(df.loc[low, "run_id"]))
    df = df[~low].copy()
    df["tput_mbps"] = df["delivered_throughput_bps"] / 1e6
    df[P.CPU_COL] = df["cpu_percent_traffic"]
    print(f"  Ryu: {len(df)} runs used")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", default="results_v4/summary/summary.csv")
    ap.add_argument("--outdir", default="results_v4/figures_paper")
    ap.add_argument("--min-success", type=float, default=0.98)
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--fps-n", default="64")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    print(f"Loading: {args.summary}")
    hashes = code_hashes(args.summary)
    ryu = load_ryu(args.summary, args.min_success)
    P.check_completeness(ryu, "Ryu", args.reps)

    print("Generating figures and tables...")
    P.fig_latency_vs_fps(ryu, args.outdir, args.fps_n)
    P.fig_scalability_vs_hosts(ryu, args.outdir)
    P.table_hosts(ryu, args.outdir)
    P.paper_numbers(ryu, None, args.outdir)

    # Record the source, definitions and code versions next to the numbers.
    path = os.path.join(args.outdir, "paper_numbers.txt")
    with open(path) as f:
        body = f.read()
    with open(path, "w") as f:
        f.write(f"== Source ==\n{args.summary}, controller = ryu, {len(ryu)} runs\n"
                "latency: proc_latency_p50_ms / proc_latency_p95_ms (handler time)\n"
                "CPU: cpu_percent_traffic (traffic_launch start -> client_drain end)\n"
                f"code_hash values in the summary (rows per controller): {len(hashes)}"
                f"{'  <-- WARNING: more than one code version' if len(hashes) > 1 or '' in hashes else ''}\n")
        for h, per in hashes.items():
            f.write(f"  {h or '(no code_hash)'}: {per}\n")
        f.write("\n" + body)
        f.write("\n== For reference: avg_cpu_percent (measure start -> last monitor sample), "
                "by topology x N ==\n")
        f.write(ryu.groupby(["topology", "n_hosts"])["avg_cpu_percent"].mean()
                .unstack().round(2).to_string() + "\n")
    print(f"\nDone. Outputs in {args.outdir}/")


if __name__ == "__main__":
    main()
