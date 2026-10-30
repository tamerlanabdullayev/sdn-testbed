#!/usr/bin/env python3
"""
run_experiment.py: one experiment run for any registered controller.

    run_experiment.py --controller ryu --n 4 --run_id STAR_N4_F200_R1 \
        --fps 200 --total_time 20 --flow_duration 2 --seed 1241 --topo star

Phases (same order as the former run_experiment.sh): pre_cleanup,
controller_start (start + wait_ready + monitor), warmup (optional),
capture_start (tcpdump on the OpenFlow port), mininet_run, teardown,
wire_analysis, aggregation, cleanup.

CPU pinning (optional, recorded in controller_meta.json):
  CONTROLLER_CPUS   e.g. "3"    controller process and all its threads
  WORKLOAD_CPUS     e.g. "0-2"  Mininet, iperf3 clients/servers, warm-up, tcpdump
OVS (ovs-vswitchd, kernel datapath) is a system service and is not pinned. Output: <results_root>/raw/<controller>/<run_id>/ and
one row appended to <results_root>/summary/summary.csv.

Every run records scripts/code_hash.py's SHA-256 of the testbed code, taken
at the start of the run, in controller_meta.json and traffic_meta.json
(summary column code_hash).

Run it with the testbed virtualenv's python (psutil is needed for the
monitor); Mininet is started with the system python3 under sudo.
"""

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from controller.adapters import (ControllerNotReady, all_process_patterns,  # noqa: E402
                                 canonical_controller, get_adapter)
from scripts.code_hash import code_hash  # noqa: E402

PORT = 6653


def parse_cpus(spec):
    """'0-2,5' -> {0, 1, 2, 5}; '' / None -> None."""
    if not spec:
        return None
    cpus = set()
    for part in str(spec).split(","):
        lo, _, hi = part.strip().partition("-")
        cpus.update(range(int(lo), int(hi or lo) + 1))
    bad = cpus - os.sched_getaffinity(0)
    if bad:
        raise SystemExit(f"[ERROR] CPUs {sorted(bad)} not available on this machine")
    return cpus


def taskset(spec):
    return ["taskset", "-c", spec] if spec else []


class PhaseLog:
    """Writes phase_times.tsv (merged by phase_times.py); run_mininet_iperf.py
    appends its own phases to the same file."""

    def __init__(self, path):
        self.path = path
        self._t0 = {}
        path.write_text("")

    def _write(self, name, event, ts):
        with open(self.path, "a") as f:
            f.write(f"{name}\t{event}\t{ts:.6f}\n")

    def start(self, name):
        self._t0[name] = time.time()
        self._write(name, "start", self._t0[name])
        print(f"[PHASE] {name} start", flush=True)

    def end(self, name):
        ts = time.time()
        self._write(name, "end", ts)
        print(f"[PHASE] {name} end ({ts - self._t0.get(name, ts):.2f}s)", flush=True)


def sudo(*argv, check=False, **kw):
    return subprocess.run(["sudo", *argv], check=check, **kw)


def quiet_sudo(*argv):
    sudo(*argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Capture:
    """tcpdump of the OpenFlow control channel on lo (sudo; file owned by the user)."""

    def __init__(self, outdir, port, cpus=None):
        self.cpus = cpus
        self.pcap = outdir / "openflow.pcap"
        self.pidfile = outdir / "tcpdump_pid.txt"
        self.errlog = outdir / "tcpdump_stderr.log"
        self.port = port
        self.proc = None

    def start(self, timeout=10.0):
        self.pidfile.unlink(missing_ok=True)
        pin = f"taskset -c {self.cpus} " if self.cpus else ""
        script = (f'echo $$ > "$1"; exec {pin}tcpdump -i lo -n -s 0 -B 32768 '
                  '--time-stamp-precision=nano -Z "$2" -w "$3" tcp port "$4"')
        self.proc = subprocess.Popen(
            ["sudo", "sh", "-c", script, "sh", str(self.pidfile), getpass.getuser(),
             str(self.pcap), str(self.port)],
            stdout=subprocess.DEVNULL, stderr=open(self.errlog, "w"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "listening on" in self.errlog.read_text():
                return True
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        return False

    def stop(self, grace=5.0):
        if not self.proc or self.proc.poll() is not None:
            return
        if self.pidfile.exists():
            quiet_sudo("kill", "-INT", self.pidfile.read_text().strip())
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            quiet_sudo("kill", "-KILL", self.pidfile.read_text().strip())
            self.proc.wait()


def parse_args():
    env = os.environ.get
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", default=env("CONTROLLER", "ryu"))
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--run_id", default="devrun_001")
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--total_time", type=int, default=20)
    ap.add_argument("--flow_duration", type=int, default=2)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--topo", default="star",
                    help="star | tree (alias: fattree) | mesh (experimental)")
    ap.add_argument("--host_bw", default=env("HOST_BW", "0"),
                    help="Mbit/s on host-switch links; 0 = no limit")
    ap.add_argument("--fq_rate", default=env("FQ_RATE", "2M"),
                    help="per-flow iperf3 --fq-rate; 0 = unpaced")
    ap.add_argument("--keep_iperf_json", action="store_true",
                    default=env("KEEP_IPERF_JSON", "0") == "1")
    ap.add_argument("--no_capture", action="store_true",
                    default=env("CAPTURE", "1") == "0",
                    help="skip the OpenFlow tcpdump (no wire_* metrics)")
    ap.add_argument("--keep_pcap", action="store_true",
                    default=env("KEEP_PCAP", "0") == "1")
    ap.add_argument("--keep_logs", action="store_true",
                    default=env("KEEP_LOGS", "0") == "1",
                    help="keep controller/Mininet/warm-up logs after aggregation")
    ap.add_argument("--results_root", default=env("RESULTS_ROOT", str(ROOT / "results_v4")))
    ap.add_argument("--ready_timeout", type=float, default=float(env("READY_TIMEOUT", "120")))
    ap.add_argument("--controller_cpus", default=env("CONTROLLER_CPUS", ""))
    ap.add_argument("--workload_cpus", default=env("WORKLOAD_CPUS", ""))
    ap.add_argument("--warmup_rounds", type=int, default=int(env("WARMUP_ROUNDS", "0")),
                    help="warm-up networks run before the measured one; 0 = none")
    ap.add_argument("--warmup_hosts", type=int, default=int(env("WARMUP_HOSTS", "16")))
    return ap.parse_args()


def main():
    args = parse_args()
    controller = canonical_controller(args.controller)
    adapter = get_adapter(controller, ROOT)
    # absolute: controllers write to OUTDIR from their own working directory
    results_root = Path(args.results_root).resolve()
    outdir = results_root / "raw" / controller / args.run_id
    summary = results_root / "summary" / "summary.csv"
    outdir.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    run_code_hash = code_hash(ROOT)
    ctrl_cpus = parse_cpus(args.controller_cpus)
    parse_cpus(args.workload_cpus)
    if ctrl_cpus and args.workload_cpus and ctrl_cpus & parse_cpus(args.workload_cpus):
        print("[WARNING] CONTROLLER_CPUS and WORKLOAD_CPUS overlap", file=sys.stderr)

    for k, v in [("OUTDIR", outdir), ("CONTROLLER", controller), ("N", args.n),
                 ("TOPO", args.topo), ("HOST_BW", args.host_bw), ("FQ_RATE", args.fq_rate),
                 ("CONTROLLER_CPUS", args.controller_cpus or "-"),
                 ("WORKLOAD_CPUS", args.workload_cpus or "-"),
                 ("WARMUP_ROUNDS", args.warmup_rounds), ("CODE_HASH", run_code_hash)]:
        print(f"[INFO] {k}={v}")

    phases = PhaseLog(outdir / "phase_times.tsv")

    phases.start("pre_cleanup")
    quiet_sudo("mn", "-c")
    for pat in all_process_patterns():
        quiet_sudo("pkill", "-f", pat)
    # iperf3 servers/clients orphaned by an interrupted run (they outlive
    # `mn -c`); anchored so the system iperf3 service is not matched
    quiet_sudo("pkill", "-f", "^iperf3 -[sc] ")
    phases.end("pre_cleanup")

    meta = {"controller": controller, "port": PORT, "code_hash": run_code_hash,
            "controller_cpus": args.controller_cpus, "workload_cpus": args.workload_cpus,
            "warmup_rounds": args.warmup_rounds,
            "warmup_hosts": args.warmup_hosts if args.warmup_rounds else 0}
    try:
        meta.update(adapter.version())
    except Exception as e:  # recorded, not fatal
        meta["version_error"] = str(e)

    sys_proc = None
    capture = None
    run_ok = True
    try:
        phases.start("controller_start")
        adapter.start(outdir, PORT, cpus=ctrl_cpus)
        meta["ready_time_s"] = round(adapter.wait_ready(args.ready_timeout), 3)
        # The monitor starts once the controller is ready: adapter.pid is then
        # the process doing the work (for ONOS the JVM, not its launcher).
        pid = adapter.pid
        (outdir / f"{controller}_pid.txt").write_text(f"{pid}\n")
        print(f"[INFO] {controller} ready after {meta['ready_time_s']}s, PID={pid}")
        sys_proc = subprocess.Popen(
            [py, str(ROOT / "scripts" / "log_system_metrics.py"),
             "--pid", str(pid), "--outdir", str(outdir), "--interval", "1.0"],
            stdout=open(outdir / "syslog_stdout.log", "w"),
            stderr=open(outdir / "syslog_stderr.log", "w"))
        (outdir / "syslog_pid.txt").write_text(f"{sys_proc.pid}\n")
        phases.end("controller_start")

        if args.warmup_rounds > 0:
            phases.start("warmup")
            with open(outdir / "warmup.log", "w") as log:
                wu = sudo(*taskset(args.workload_cpus), "/usr/bin/python3",
                          str(ROOT / "scripts" / "warmup.py"),
                          "--controller_port", str(PORT),
                          "--rounds", str(args.warmup_rounds),
                          "--hosts", str(args.warmup_hosts),
                          cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            meta["warmup_exit"] = wu.returncode
            # Let one controller_metrics row pass so no row mixes warm-up and
            # measured counts; rows before measure_start_ts are dropped by the
            # aggregator.
            time.sleep(1.5)
            meta["measure_start_ts"] = time.time()
            phases.end("warmup")

        if not args.no_capture:
            phases.start("capture_start")
            capture = Capture(outdir, PORT, cpus=args.workload_cpus or None)
            if not capture.start():
                print("[WARNING] tcpdump did not start; no wire metrics "
                      f"(see {capture.errlog})", file=sys.stderr)
            phases.end("capture_start")

        phases.start("mininet_run")
        with open(outdir / "mininet_iperf.log", "w") as log:
            mn = sudo(*taskset(args.workload_cpus),
                 "/usr/bin/python3", str(ROOT / "scripts" / "run_mininet_iperf.py"),
                 "--n", str(args.n),
                 "--controller_ip", "127.0.0.1",
                 "--controller_port", str(PORT),
                 "--flows_per_sec", str(args.fps),
                 "--flow_duration", str(args.flow_duration),
                 "--total_time", str(args.total_time),
                 "--seed", str(args.seed),
                 "--outdir", str(outdir),
                 "--topo", args.topo,
                 "--host_bw", str(args.host_bw),
                 "--fq_rate", str(args.fq_rate),
                 "--code_hash", run_code_hash,
                 cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        phases.end("mininet_run")
        # Non-zero exit (e.g. switches never connected): nothing was measured,
        # so the run is not aggregated and stays retryable by the sweep.
        meta["mininet_exit"] = mn.returncode
        if mn.returncode != 0:
            print(f"[ERROR] run_mininet_iperf.py exited {mn.returncode} "
                  f"(see {outdir / 'mininet_iperf.log'})", file=sys.stderr)
            run_ok = False
    except ControllerNotReady as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        meta["ready_error"] = str(e)
        run_ok = False
    finally:
        phases.start("teardown")
        if capture:
            capture.stop()
        if sys_proc and sys_proc.poll() is None:
            sys_proc.terminate()
        adapter.stop()
        time.sleep(1)
        quiet_sudo("mn", "-c")
        quiet_sudo("chown", "-R", f"{os.getuid()}:{os.getgid()}", str(outdir))
        with open(outdir / "controller_meta.json", "w") as f:
            json.dump(meta, f, indent=2)
        phases.end("teardown")

    print(f"[DONE] Run complete: {args.run_id}")
    print(f"[DONE] Files in: {outdir}")

    if run_ok and capture and capture.pcap.exists():
        phases.start("wire_analysis")
        subprocess.run([py, str(ROOT / "analysis" / "openflow_latency.py"),
                        "--rundir", str(outdir), "--port", str(PORT)])
        phases.end("wire_analysis")

    if run_ok:
        print(f"[AGG] Aggregating {args.run_id} ...")
        phases.start("aggregation")
        agg = subprocess.run([py, str(ROOT / "analysis" / "aggregate_results.py"),
                              "--rundir", str(outdir), "--summary", str(summary)])
        phases.end("aggregation")
        if agg.returncode == 0:
            phases.start("cleanup")
            print(f"[CLEANUP] Removing bulky raw files for {args.run_id} ...")
            if args.keep_iperf_json:
                print(f"[CLEANUP] KEEP_IPERF_JSON=1: keeping {outdir / 'iperf_json'}")
            else:
                shutil.rmtree(outdir / "iperf_json", ignore_errors=True)
            if not args.keep_pcap:
                (outdir / "openflow.pcap").unlink(missing_ok=True)
            if args.keep_logs:
                print(f"[CLEANUP] KEEP_LOGS=1: keeping logs in {outdir}")
            else:
                for name in (f"{controller}_stdout.log", f"{controller}_stderr.log",
                             "mininet_iperf.log", "syslog_stdout.log", "syslog_stderr.log",
                             "warmup.log"):
                    (outdir / name).unlink(missing_ok=True)
            phases.end("cleanup")
            print("[CLEANUP] Done. Kept: controller_metrics.csv, flow_events.csv, "
                  "system_metrics.csv, traffic_meta.json, iperf_summary.json, "
                  "controller_meta.json, wire_events.csv, wire_summary.json, "
                  "phase_times.json")
        else:
            print(f"[WARNING] Aggregation failed for {args.run_id} — raw files kept "
                  "for manual inspection.")
    else:
        print(f"[WARNING] Run failed for {args.run_id} (controller not ready or "
              "Mininet error) — not aggregated; raw files kept.")

    subprocess.run([py, str(ROOT / "scripts" / "phase_times.py"), "--rundir", str(outdir)])
    return 0 if run_ok else 1


if __name__ == "__main__":
    sys.exit(main())
