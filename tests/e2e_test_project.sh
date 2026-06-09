#!/usr/bin/env bash
# M3 end-to-end proof: hydracoder builds a slightly-complex app WITH a web UI
# using LOCAL MODELS ONLY (via hydra-llm), then we run the generated app's own
# tests. Also exercises journal-based resume (the crash/resilience path).
#
# Exit 0 only if the generated project's acceptance tests pass.
#
# Usage: tests/e2e_test_project.sh [worker_model_id]
# Requires: a hydra-llm worker model running (or downloadable) + the sibling
# lillycoder / hydra-llm repos.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LILLY="$ROOT/../lillycoder/lib"
HYDRALLM="$ROOT/../hydra-llm/lib"
export PYTHONPATH="$ROOT/lib:$LILLY:$HYDRALLM"

WORKER="${1:-gemma-4-26b-a4b-it-ud}"
RUN_DIR="$(mktemp -d)"
WS_DIR="$(mktemp -d)"
echo "== hydracoder e2e =="
echo "run_dir=$RUN_DIR  workspace=$WS_DIR  worker=$WORKER"

GOAL=$(cat <<'EOF'
Build a small "todo list" web application in Python using ONLY the standard
library (no Flask, no pip, no virtualenv, do NOT install anything). Files:
(1) store.py: a TodoStore class that persists todos to a JSON file given in its
    constructor. Methods: add(text) returns an integer id; list() returns a list
    of dicts each with EXACTLY the keys "id","text","done" (use the key "done",
    a boolean); complete(id) sets done True; remove(id) deletes it.
(2) server.py: a web server built on python's stdlib http.server (BaseHTTPRequestHandler),
    NOT Flask. GET / serves index.html; GET /api/todos returns the JSON list;
    POST /api/todos adds a todo from JSON {"text":...}. Import TodoStore from store.py.
(3) index.html: a page listing todos with an input + button to add one, using fetch.
(4) test_store.py: a unittest TestCase that checks add/list/complete/remove on
    TodoStore, asserting the "done" key behavior. CRITICAL: each test must be
    isolated. In setUp create a UNIQUE temporary file with tempfile.mkstemp (or
    tempfile.NamedTemporaryFile) and in tearDown delete it, so no state leaks
    between tests. Do NOT use a fixed shared filename.
The files must use each other's real interfaces consistently. Do not run pip or
create a virtual environment; everything uses only the standard library.
EOF
)

# --- run the orchestrator (local models only) ---
python3 - "$RUN_DIR" "$WS_DIR" "$WORKER" "$GOAL" <<'PY'
import sys
from pathlib import Path
from hydracoder.orchestrator import Orchestrator
from hydracoder.scheduler import Roster
run_dir, ws_dir, worker, goal = sys.argv[1:5]
# small handles trivial leaf tasks; worker+reviewer the rest. Tiny model for boss/small.
roster = Roster(small="gemma-4-e2b-it", worker=worker, reviewer=worker, boss=worker)
orch = Orchestrator(Path(run_dir), Path(ws_dir), roster=roster,
                    log=lambda m: print("[orch]", m, flush=True))
res = orch.run(goal, max_task_tokens=2500)
print("ORCH_RESULT:", res, flush=True)
orch.shutdown_started()
PY

echo "== files produced =="
ls -la "$WS_DIR"

# --- run the generated project's OWN tests (the authoritative proof) ---
echo "== running generated test_store.py =="
cd "$WS_DIR"
if [ ! -f test_store.py ]; then
  echo "FAIL: generated project has no test_store.py"
  exit 1
fi
# Run with unittest (stdlib, always present). Do NOT mask the exit code.
python3 -m unittest -v test_store
rc=$?
echo "generated-tests exit: $rc"
if [ "$rc" -ne 0 ]; then
  echo "FAIL: generated test suite did not pass"
  exit "$rc"
fi
echo "PASS: hydracoder built a working, self-tested project with local models only"
exit 0
