#!/usr/bin/env bash
# Go/no-go smoke test before a sweep: three single runs, then checks the pass
# criteria (success_rate >= 0.98, per-run CSVs non-empty, summary columns set).
#   scripts/smoke_test.sh            # all three cases
#   CASES="ryu:star:4" scripts/smoke_test.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/venv_py310/bin/python"
[ -x "$PY" ] || PY="$ROOT/venv/bin/python"
RESULTS_ROOT="${RESULTS_ROOT:-$ROOT/results_v4}"
export RESULTS_ROOT
STAMP="$(date +%m%d_%H%M)"
CASES="${CASES:-ryu:star:4 osken:star:4 onos:star:4 ryu:tree:64 onos:tree:64}"

sudo -v || exit 1

RUNS=()
for c in $CASES; do
  IFS=: read -r CTRL TOPO N <<< "$c"
  case "$TOPO" in star) BT=1000 ;; tree|fattree) BT=2000 ;; mesh) BT=3000 ;; esac
  FPS=200; REP=1
  SEED=$((BT + N*10 + FPS + REP))
  RUN_ID="SMOKE_${STAMP}_${TOPO^^}_N${N}_F${FPS}_R${REP}"
  echo "=============================================================="
  echo "[SMOKE] $CTRL $TOPO N=$N FPS=$FPS seed=$SEED -> $RUN_ID"
  T0=$(date +%s.%N)
  CONTROLLER="$CTRL" "$ROOT/scripts/run_experiment.sh" "$N" "$RUN_ID" "$FPS" 20 2 "$SEED" "$TOPO"
  echo "[SMOKE] exit=$? wall=$(awk -v a="$T0" -v b="$(date +%s.%N)" 'BEGIN{printf "%.1f", b-a}')s"
  RUNS+=("$CTRL/$RUN_ID")
  sudo -v
done

echo "=============================================================="
"$PY" - "$RESULTS_ROOT" "${RUNS[@]}" <<'EOF'
import csv, sys
from pathlib import Path
root = Path(sys.argv[1])
rows = {(r["controller"], r["run_id"]): r for r in csv.DictReader(open(root / "summary" / "summary.csv"))}
ok_all = True
for run in sys.argv[2:]:
    ctrl, run_id = run.split("/")
    d = root / "raw" / ctrl / run_id
    problems = []
    for f in ("flow_events.csv", "controller_metrics.csv", "system_metrics.csv"):
        p = d / f
        if not p.exists() or sum(1 for _ in open(p)) < 2:
            problems.append(f"{f} missing/empty")
    r = rows.get((ctrl, run_id))
    if r is None:
        problems.append("no summary row")
    else:
        empty = [k for k, v in r.items() if v == "" and k not in ("port_collisions", "controller_cpus", "workload_cpus")]
        if empty:
            problems.append(f"empty columns: {empty}")
        sr = float(r["success_rate"] or 0)
        if sr < 0.98:
            problems.append(f"success_rate {sr} < 0.98")
    try:
        import json
        total = json.load(open(d / "phase_times.json"))["total_s"]
    except Exception:
        total = None
    print(f"[{'PASS' if not problems else 'FAIL'}] {run}")
    if r:
        print(f"    success_rate={r['success_rate']} attempted={r['attempted_flows']} "
              f"pi_rate={r['avg_packet_in_rate']} fm_rate={r['avg_flow_mod_rate']} "
              f"p50={r['proc_latency_p50_ms']}ms p95={r['proc_latency_p95_ms']}ms "
              f"cpu={r['avg_cpu_percent']}% ready={r['ready_time_s']}s "
              f"{r['controller']} {r['controller_version']} ({r['controller_runtime']}) "
              f"total={total}s")
    for p in problems:
        print(f"    - {p}")
    ok_all &= not problems
print("[SMOKE] GO" if ok_all else "[SMOKE] NO-GO")
sys.exit(0 if ok_all else 1)
EOF
