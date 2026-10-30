#!/usr/bin/env bash
# Thin wrapper around run_experiment.py with a positional interface (used by
# smoke_test.sh). Controller: CONTROLLER=ryu|osken|onos.
#   scripts/run_experiment.sh N RUN_ID FPS TOTAL_TIME FLOW_DURATION SEED TOPO
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/venv_py310/bin/python"
[ -x "$PY" ] || PY="$ROOT/venv/bin/python"

exec "$PY" "$ROOT/scripts/run_experiment.py" \
  --controller "${CONTROLLER:-ryu}" \
  --n "${1:-4}" \
  --run_id "${2:-devrun_001}" \
  --fps "${3:-25}" \
  --total_time "${4:-20}" \
  --flow_duration "${5:-2}" \
  --seed "${6:-1}" \
  --topo "${7:-star}"
