#!/usr/bin/env bash
# Complex-task e2e: hydracoder builds a small expression-language interpreter
# (lexer -> parser -> evaluator -> REPL) with LOCAL MODELS ONLY, then we run
# the generated test suite, enforce per-module test depth, and behaviorally
# smoke-test the REPL. This is a deliberate step up from e2e_test_project.sh:
# deeper dependency chain, algorithmic correctness (precedence/associativity),
# and error handling as part of the contract.
#
# Exit 0 only if all gates pass.
#
# Usage: tests/e2e_interpreter.sh [worker_model_id]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LILLY="$ROOT/../lillycoder/lib"
HYDRALLM="$ROOT/../hydra-llm/lib"
export PYTHONPATH="$ROOT/lib:$LILLY:$HYDRALLM"

WORKER="${1:-gemma-4-26b-a4b-it-ud}"
RUN_DIR="$(mktemp -d)"
WS_DIR="$(mktemp -d)"
echo "== hydracoder interpreter e2e =="
echo "run_dir=$RUN_DIR  workspace=$WS_DIR  worker=$WORKER"

GOAL=$(cat <<'EOF'
Build a small arithmetic expression language interpreter in Python using ONLY
the standard library (no pip, no virtualenv, do NOT install anything). Files:
(1) lexer.py: tokenize(text) returns a list of (kind, value, pos) tuples.
    Kinds: "NUM" (int or float value), "IDENT" (variable names), "OP" (one of
    + - * / % ** ( ) =). Skip spaces. On an illegal character raise
    LexError(message, pos) where pos is the character index. Define LexError
    in lexer.py.
(2) parser.py: parse(tokens) returns a nested-tuple AST:
    ("num", value), ("var", name), ("assign", name, expr),
    ("binop", op, left, right), ("neg", expr).
    Precedence, tightest first: ** (RIGHT-associative), unary minus, then
    * / %, then + - (both LEFT-associative). Parentheses group. "name = expr"
    is an assignment and only allowed at the top level. On bad syntax raise
    ParseError(message). Define ParseError in parser.py. Import the token
    kinds produced by lexer.py; do not invent different ones.
(3) evaluator.py: class Evaluator with a persistent dict of variables.
    method eval_ast(ast) evaluates a parse(...) result and returns int/float
    (Python semantics). Assignment stores the value AND returns it. Using an
    undefined variable raises EvalError("undefined variable: NAME"). Division
    or modulo by zero raises EvalError("division by zero"). Define EvalError
    in evaluator.py.
(4) cli.py: a REPL on stdin/stdout. Read lines; skip blank lines; the line
    "quit" exits. For each other line: tokenize, parse, evaluate with ONE
    Evaluator shared across lines, and print ONLY the numeric result on its
    own line (no prompt text). On LexError/ParseError/EvalError print
    "error: <message>" and continue. Must work non-interactively:
    printf 'x = 20\nx * 2 + 2\nquit\n' | python3 cli.py prints 20 then 42.
(5) test_lexer.py, test_parser.py, test_evaluator.py: unittest TestCase files,
    AT LEAST 4 real test methods EACH, with meaningful assertions. They must
    cover at minimum: number and float tokenization, LexError position,
    precedence (2+3*4 == 14), parentheses ((2+3)*4 == 20), right-associative
    power (2**3**2 == 512), unary minus (-3**2 == -9), assignment then reuse
    of a variable, undefined-variable error, and division-by-zero error.
All modules must import each other's REAL interfaces (read the files first).
Do not run pip or create a virtual environment.
EOF
)

# --- run the orchestrator (local models only) ---
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

echo "== files produced =="
ls -la "$WS_DIR"
cd "$WS_DIR" || { echo "FAIL: cannot cd into workspace $WS_DIR"; exit 1; }

# --- the three demanded test files must exist ---
for f in test_lexer.py test_parser.py test_evaluator.py; do
  if [ ! -f "$f" ]; then
    echo "FAIL: generated project is missing $f"
    exit 1
  fi
done

# --- run the full generated suite independently (do NOT mask the exit code) ---
echo "== running the generated test suite =="
mods=""
for f in test_*.py; do mods="$mods ${f%.py}"; done
# shellcheck disable=SC2086  # word-splitting the module list is intended
python3 -m unittest -v $mods
rc=$?
echo "generated-tests exit: $rc"
if [ "$rc" -ne 0 ]; then
  echo "FAIL: generated test suite did not pass"
  exit "$rc"
fi

# --- per-module test depth: at least 3 collected tests each ---
echo "== checking per-module test depth (>= 3 each) =="
python3 - <<'PY'
import sys, unittest
from pathlib import Path
bad = []
for p in sorted(Path(".").glob("test_*.py")):
    n = unittest.defaultTestLoader.loadTestsFromName(p.stem).countTestCases()
    print(f"  {p.name}: {n} tests")
    if n < 3:
        bad.append(f"{p.name} ({n})")
if bad:
    print("FAIL: too few tests in:", ", ".join(bad))
    sys.exit(1)
PY
rc=$?
if [ "$rc" -ne 0 ]; then
  exit "$rc"
fi

# --- behavioral smoke test: the REPL actually computes ---
echo "== REPL smoke test =="
out=$(printf 'x = 20\nx * 2 + 2\n2 ** 3 ** 2\n-3 ** 2\n1 / 0\nquit\n' | timeout 30 python3 cli.py 2>&1)
echo "$out"
echo "$out" | grep -qE '^42(\.0)?$'  || { echo "FAIL: REPL did not print 42";  exit 1; }
echo "$out" | grep -qE '^512(\.0)?$' || { echo "FAIL: REPL did not print 512 (right-assoc **)"; exit 1; }
echo "$out" | grep -qE '^-9(\.0)?$'  || { echo "FAIL: REPL did not print -9 (unary minus vs **)"; exit 1; }
echo "$out" | grep -qi 'error' || { echo "FAIL: REPL did not report the division-by-zero error"; exit 1; }

echo "PASS: hydracoder built a working interpreter with local models only"
exit 0
