#!/usr/bin/env bash
# Brownfield e2e runner: point hydracoder at an EXISTING codebase and a goal,
# then verify the repo's own full test suite is green afterwards and that no
# test file was modified (the run must fix code, not bend tests).
#
# The workspace is used IN PLACE (the orchestrator works directly in it), so
# pass a disposable copy, never a checkout you care about.
#
# Usage: tests/e2e_existing_repo.sh <workspace> <goal-text> [worker_model_id]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT/lib:$ROOT/../lillycoder/lib:$ROOT/../hydra-llm/lib"

WS_DIR="${1:?usage: e2e_existing_repo.sh <workspace> <goal-text> [worker]}"
GOAL="${2:?usage: e2e_existing_repo.sh <workspace> <goal-text> [worker]}"
WORKER="${3:-gemma-4-26b-a4b-it-ud}"
RUN_DIR="$(mktemp -d)"

echo "== hydracoder brownfield e2e =="
echo "run_dir=$RUN_DIR  workspace=$WS_DIR  worker=$WORKER"

# Fingerprint the test files so we can prove the run did not touch them.
test_fingerprint() {
  find "$WS_DIR" -name 'test_*.py' -not -path '*/.git/*' -print0 \
    | sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1
}
before_tests="$(test_fingerprint)"

python3 - "$RUN_DIR" "$WS_DIR" "$WORKER" "$GOAL" <<'PY'
import sys
from pathlib import Path
from hydracoder.orchestrator import Orchestrator
from hydracoder.scheduler import Roster
run_dir, ws_dir, worker, goal = sys.argv[1:5]
roster = Roster(small="gemma-4-e2b-it", worker=worker, reviewer=worker, boss=worker)
orch = Orchestrator(Path(run_dir), Path(ws_dir), roster=roster,
                    log=lambda m: print("[orch]", m, flush=True))
res = orch.run(goal, max_task_tokens=4000)
print("ORCH_RESULT:", res, flush=True)
orch.shutdown_started()
sys.exit(0 if res.get("ok") else 1)
PY
orch_rc=$?
if [ "$orch_rc" -ne 0 ]; then
  echo "FAIL: orchestrator run did not finish ok (rc=$orch_rc)"
  exit "$orch_rc"
fi

# --- the repo's own full suite must be green (independent re-run) ---
echo "== running the repo's own test suite =="
cd "$WS_DIR" || { echo "FAIL: cannot cd into workspace $WS_DIR"; exit 1; }
PYTHONPATH="$ROOT/lib" python3 - <<'PY'
import json, sys
from pathlib import Path
from hydracoder import verifier
r = verifier.run_tests(Path("."), timeout=300.0)
print(r["summary"])
print(json.dumps(r["test_counts"], indent=1) if r["test_counts"] else "(no counts)")
sys.exit(0 if (r["ran"] and r["passed"]) else 1)
PY
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "FAIL: the repo's test suite is not green after the run"
  exit "$rc"
fi

# --- the tests themselves must be untouched ---
after_tests="$(test_fingerprint)"
if [ "$before_tests" != "$after_tests" ]; then
  echo "FAIL: the run MODIFIED test files (fingerprint changed)"
  exit 1
fi
echo "test files untouched: ok"

echo "PASS: hydracoder completed the brownfield goal; suite green, tests untouched"
exit 0
