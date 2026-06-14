#!/usr/bin/env bash
# Test-first e2e: hydracoder builds a small project in TEST-FIRST mode with
# local models only. The point being proven is not just "tests pass at the
# end" but that the tests were authored and AUDITED (proven to fail-first for
# a missing-implementation reason) BEFORE the implementation existed. We then
# confirm the final suite is green and that the journal recorded a passing
# test audit for the module(s).
#
# Exit 0 only if the build succeeds, the audit passed, and the suite is green.
#
# Usage: tests/e2e_test_first.sh [worker_model_id]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT/lib:$ROOT/../lillycoder/lib:$ROOT/../hydra-llm/lib"

WORKER="${1:-gemma-4-26b-a4b-it-ud}"
RUN_DIR="$(mktemp -d)"
WS_DIR="$(mktemp -d)"
echo "== hydracoder test-first e2e =="
echo "run_dir=$RUN_DIR  workspace=$WS_DIR  worker=$WORKER"

GOAL=$(cat <<'EOF'
Build a small stack data structure in Python using ONLY the standard library
(no pip, no virtualenv). A single module stack.py exposing a Stack class with:
push(item) appends an item; pop() removes and returns the last item and raises
IndexError("pop from empty stack") when empty; peek() returns the last item
without removing it and raises IndexError("peek from empty stack") when empty;
__len__ returns the number of items; is_empty() returns True iff empty.
EOF
)

python3 - "$RUN_DIR" "$WS_DIR" "$WORKER" "$GOAL" <<'PY'
import sys
from pathlib import Path
from hydracoder.orchestrator import Orchestrator
from hydracoder.scheduler import Roster
from hydracoder import config as config_mod
run_dir, ws_dir, worker, goal = sys.argv[1:5]
roster = Roster(small="gemma-4-e2b-it", worker=worker, reviewer=worker, boss=worker)
cfg = config_mod.from_dict({"run": {"test_first": True}})
orch = Orchestrator(Path(run_dir), Path(ws_dir), roster=roster, config=cfg,
                    log=lambda m: print("[orch]", m, flush=True))
res = orch.run(goal, max_task_tokens=3000)
print("ORCH_RESULT:", res, flush=True)
# Confirm a test audit passed in the journal (the test-first proof).
audits = [ev.data for ev in orch.journal.replay() if ev.kind == "test_audit"]
ok_audits = [a for a in audits if a.get("ok")]
print("TEST_AUDITS:", len(audits), "passed:", len(ok_audits), flush=True)
orch.shutdown_started()
sys.exit(0 if (res.get("ok") and ok_audits) else 1)
PY
orch_rc=$?
if [ "$orch_rc" -ne 0 ]; then
  echo "FAIL: test-first run did not finish ok with a passing audit (rc=$orch_rc)"
  exit "$orch_rc"
fi

echo "== files produced =="
ls -la "$WS_DIR"
cd "$WS_DIR" || { echo "FAIL: cannot cd into workspace $WS_DIR"; exit 1; }

if [ ! -f stack.py ] || [ ! -f test_stack.py ]; then
  echo "FAIL: expected stack.py and test_stack.py"
  exit 1
fi

echo "== running the generated suite =="
python3 -m unittest -v test_stack
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "FAIL: generated suite did not pass"
  exit "$rc"
fi

echo "PASS: hydracoder built a stack test-first (tests proven to fail-first, then green)"
exit 0
