#!/usr/bin/env python3
"""
sweep.py: the whole experiment in one command (after setup, see README).

    scripts/sweep.py                          # paper matrix, all controllers
    scripts/sweep.py --controllers ryu --topos star
    scripts/sweep.py --smoke                  # go/no-go check with the same settings
    scripts/sweep.py --dry_run                # list the runs and the estimated time

Defaults are the paper's: N = 4,8,16,32,64; FPS = 25,50,100,200; 10 reps;
20 s runs; 2 s flows; seed = B_T + 10N + FPS + rep (B_T: star 1000, tree
2000, mesh 3000); controller on CPU 3, workload on CPUs 0-2; one warm-up
round. Order: controller -> topology -> N -> FPS -> rep.

Runs already in <results_root>/summary/summary.csv (same controller and
run_id) are skipped, so an interrupted sweep continues where it stopped
(--no_resume reruns everything). A run that fails is logged and the sweep
goes on. Each sweep writes <results_root>/sweeps/sweep_<time>.json with its
parameters, the machine/software environment and the outcome of every run.

Never run two sweeps at the same time (shared OpenFlow port, `mn -c`).
Standard library only; each run is scripts/run_experiment.py in the testbed
virtualenv.
"""

import argparse
import csv
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from code_hash import code_hash

ROOT = Path(__file__).resolve().parents[1]
SEED_BASE = {"star": 1000, "tree": 2000, "mesh": 3000}
RUN_PREFIX = {"star": "STAR", "tree": "FATTREE", "mesh": "MESH"}   # as in earlier data
TOPO_ALIASES = {"fattree": "tree"}
CONTROLLERS = ("ryu", "osken", "onos")
# rough seconds per run, only for --dry_run / the first ETA
EST_S = {"ryu": 38, "osken": 38, "onos": 75}


def csv_list(cast):
    return lambda s: [cast(x) for x in s.split(",") if x.strip()]


def parse_args():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--controllers", type=csv_list(str), default=list(CONTROLLERS))
    ap.add_argument("--topos", type=csv_list(str), default=["star", "tree"],
                    help="star, tree (alias fattree), mesh (experimental)")
    ap.add_argument("--hosts", type=csv_list(int), default=[4, 8, 16, 32, 64])
    ap.add_argument("--fps", type=csv_list(int), default=[25, 50, 100, 200])
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--total_time", type=int, default=20)
    ap.add_argument("--flow_duration", type=int, default=2)
    ap.add_argument("--controller_cpus", default="3")
    ap.add_argument("--workload_cpus", default="0-2")
    ap.add_argument("--warmup_rounds", type=int, default=1)
    ap.add_argument("--fq_rate", default="2M")
    ap.add_argument("--host_bw", default="0")
    ap.add_argument("--no_capture", action="store_true", help="no OpenFlow capture / wire metrics")
    ap.add_argument("--results_root", default=str(ROOT / "results_v4"))
    ap.add_argument("--no_resume", action="store_true")
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--smoke", action="store_true",
                    help="run scripts/smoke_test.sh with these settings instead of the sweep")
    a = ap.parse_args()
    a.results_root = str(Path(a.results_root).resolve())   # passed on as RESULTS_ROOT
    a.topos = [TOPO_ALIASES.get(t.lower(), t.lower()) for t in a.topos]
    a.controllers = [c.lower().replace("os-ken", "osken") for c in a.controllers]
    for t in a.topos:
        if t not in SEED_BASE:
            ap.error(f"unknown topology {t}")
    for c in a.controllers:
        if c not in CONTROLLERS:
            ap.error(f"unknown controller {c}")
    return a


def plan(a):
    runs = []
    for ctrl in a.controllers:
        for topo in a.topos:
            for n in a.hosts:
                for fps in a.fps:
                    for rep in range(1, a.reps + 1):
                        runs.append({
                            "controller": ctrl, "topo": topo, "n": n, "fps": fps, "rep": rep,
                            "seed": SEED_BASE[topo] + 10 * n + fps + rep,
                            "run_id": f"{RUN_PREFIX[topo]}_N{n}_F{fps}_R{rep}",
                        })
    return runs


def done_runs(summary):
    if not summary.exists():
        return set()
    with open(summary, newline="") as f:
        return {(r.get("controller", ""), r["run_id"]) for r in csv.DictReader(f)}


def cmd_out(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def environment(a, py):
    env = {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "os": cmd_out(["sh", "-c", ". /etc/os-release && echo $PRETTY_NAME"]),
        "cpus": os.cpu_count(),
        "cpu_model": cmd_out(["sh", "-c", "grep -m1 'model name' /proc/cpuinfo | cut -d: -f2"]),
        "mem_total": cmd_out(["sh", "-c", "grep MemTotal /proc/meminfo"]),
        "ovs": cmd_out(["ovs-vsctl", "--version"]),
        "mininet": cmd_out(["mn", "--version"]),
        "iperf3": cmd_out(["iperf3", "--version"]),
        "tcpdump": cmd_out(["tcpdump", "--version"]),
        "git_commit": cmd_out(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
        "git_dirty": bool(cmd_out(["git", "-C", str(ROOT), "status", "--porcelain"])),
        "code_hash": code_hash(ROOT),
        "controllers": {},
    }
    probe = ("import sys, json; sys.path.insert(0, sys.argv[1]); "
             "from controller.adapters import get_adapter; "
             "print(json.dumps(get_adapter(sys.argv[2], sys.argv[1]).version()))")
    for c in a.controllers:
        r = subprocess.run([py, "-c", probe, str(ROOT), c], capture_output=True, text=True, cwd="/")
        env["controllers"][c] = json.loads(r.stdout) if r.returncode == 0 else {"error": r.stderr.strip()[-300:]}
    return env


def check_cpus(spec):
    if not spec:
        return
    cpus = set()
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        cpus.update(range(int(lo), int(hi or lo) + 1))
    missing = cpus - os.sched_getaffinity(0)
    if missing:
        sys.exit(f"[ERROR] CPUs {sorted(missing)} do not exist on this machine "
                 f"({os.cpu_count()} CPUs); set --controller_cpus/--workload_cpus")


def fmt_h(s):
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.0f} min"


def main():
    a = parse_args()
    py = ROOT / "venv_py310" / "bin" / "python"
    py = str(py if py.exists() else ROOT / "venv" / "bin" / "python")
    check_cpus(a.controller_cpus)
    check_cpus(a.workload_cpus)

    run_env = dict(os.environ, CONTROLLER_CPUS=a.controller_cpus, WORKLOAD_CPUS=a.workload_cpus,
                   WARMUP_ROUNDS=str(a.warmup_rounds), FQ_RATE=a.fq_rate, HOST_BW=a.host_bw,
                   RESULTS_ROOT=a.results_root, CAPTURE="0" if a.no_capture else "1")

    if a.smoke:
        sys.exit(subprocess.call([str(ROOT / "scripts" / "smoke_test.sh")], env=run_env))

    results_root = Path(a.results_root)
    summary = results_root / "summary" / "summary.csv"
    runs = plan(a)
    skip = set() if a.no_resume else done_runs(summary)
    todo = [r for r in runs if (r["controller"], r["run_id"]) not in skip]
    est = sum(EST_S[r["controller"]] for r in todo)
    print(f"[SWEEP] {len(runs)} runs planned, {len(runs) - len(todo)} already in {summary}, "
          f"{len(todo)} to do (estimated {fmt_h(est)})")
    print(f"[SWEEP] controllers={','.join(a.controllers)} topos={','.join(a.topos)} "
          f"N={a.hosts} FPS={a.fps} reps={a.reps} controller_cpus={a.controller_cpus or '-'} "
          f"workload_cpus={a.workload_cpus or '-'} warmup_rounds={a.warmup_rounds}")
    if a.dry_run:
        for r in todo[:10]:
            print(f"  {r['controller']:6s} {r['run_id']:22s} seed={r['seed']}")
        if len(todo) > 10:
            print(f"  ... {len(todo) - 10} more")
        return 0
    if not todo:
        return 0

    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0:
        print("[WARNING] sudo needs a password: runs will stop at the first sudo prompt. "
              "Run `sudo -v` (and keep it fresh) or configure passwordless sudo.", file=sys.stderr)

    (results_root / "sweeps").mkdir(parents=True, exist_ok=True)
    record_path = results_root / "sweeps" / f"sweep_{datetime.now():%Y%m%d_%H%M%S}.json"
    record = {"started": time.time(), "args": vars(a), "environment": environment(a, py),
              "runs": []}

    def save():
        with open(record_path, "w") as f:
            json.dump(record, f, indent=2)

    save()
    print(f"[SWEEP] record: {record_path}")

    t_sweep, spent = time.time(), {}
    failed = 0
    try:
        for i, r in enumerate(todo, 1):
            print(f"\n[RUN {i}/{len(todo)}] {r['controller']} {r['run_id']} seed={r['seed']}", flush=True)
            now_hash = code_hash(ROOT)   # run_experiment.py records the same value
            t0 = time.time()
            rc = subprocess.call(
                [py, str(ROOT / "scripts" / "run_experiment.py"),
                 "--controller", r["controller"], "--n", str(r["n"]), "--run_id", r["run_id"],
                 "--fps", str(r["fps"]), "--total_time", str(a.total_time),
                 "--flow_duration", str(a.flow_duration), "--seed", str(r["seed"]),
                 "--topo", r["topo"]], env=run_env)
            dt = time.time() - t0
            spent.setdefault(r["controller"], []).append(dt)
            failed += rc != 0
            if now_hash != record["environment"]["code_hash"]:
                print(f"[WARNING] testbed code changed during the sweep (code_hash "
                      f"{now_hash[:12]} != {record['environment']['code_hash'][:12]})",
                      file=sys.stderr, flush=True)
            record["runs"].append({**r, "exit": rc, "wall_s": round(dt, 1), "code_hash": now_hash})
            save()
            left = todo[i:]
            eta = sum((sum(spent[x["controller"]]) / len(spent[x["controller"]]))
                      if x["controller"] in spent else EST_S[x["controller"]] for x in left)
            print(f"[SWEEP] {r['run_id']} {'ok' if rc == 0 else f'FAIL (exit {rc})'} in {dt:.0f}s; "
                  f"{i}/{len(todo)} done, {failed} failed, ETA {fmt_h(eta)}", flush=True)
    except KeyboardInterrupt:
        print("\n[SWEEP] interrupted; rerun the same command to continue", file=sys.stderr)
        record["interrupted"] = True
    finally:
        record["finished"] = time.time()
        save()

    print(f"[SWEEP] done in {fmt_h(time.time() - t_sweep)}: {len(record['runs'])} runs, "
          f"{failed} failed. Summary: {summary}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
