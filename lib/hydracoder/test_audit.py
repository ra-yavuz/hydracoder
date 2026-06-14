"""Test audit: prove a test file is a REAL test before any implementation
exists, in test-first mode.

The framework's weak point is that the gate is only as strong as the test
suite (project-state caveats 9-11). Test-first development strengthens the
suite at its root: write the tests before the code and prove they FAIL for
the right reason. A test that passes against absent code asserts nothing; a
test that fails with SyntaxError is broken, not meaningful. Only a test that
fails with the symptoms of missing-implementation (ImportError, AttributeError,
NameError, or an AssertionError because a stub returns the wrong thing)
demonstrably exercises the code it is supposed to gate.

This module is deterministic (run the test, classify the failure, no model
judges correctness). It answers two questions per test file:
  1. Does it collect real tests? (reuse the verifier's non-vacuous count)
  2. Does it fail-first for the RIGHT reason against the current workspace,
     where the implementation does not yet exist?
And one structural-sense question:
  3. Does it import the module it is supposed to target?

What it deliberately does NOT decide: whether the asserted VALUES are the
semantically correct answer. That is the oracle problem; a local model's
opinion on it is advisory (the reviewer role), never a deterministic gate.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import verifier as verifier_mod

# Failure signatures that mean "the implementation is genuinely missing" (a
# good fail-first), versus signatures that mean "the test itself is broken".
_GOOD_FAIL = ("ImportError", "ModuleNotFoundError", "AttributeError",
              "NameError", "AssertionError", "TypeError")
_BROKEN_TEST = ("SyntaxError", "IndentationError", "TabError")

_IMPORT = re.compile(r'^\s*(?:from|import)\s+([a-zA-Z_][\w]*)', re.MULTILINE)


def _module_imports(test_path: Path) -> set[str]:
    try:
        text = test_path.read_text(errors="replace")
    except OSError:
        return set()
    return {m.group(1) for m in _IMPORT.finditer(text)}


def audit_test_file(workspace: Path, test_module: str,
                    targets: Optional[list[str]] = None,
                    timeout: float = 60.0) -> dict:
    """Audit one test_*.py BEFORE its implementation exists.

    Returns:
      {"ok": bool,            # passes all deterministic checks
       "reason": str,         # one-line human verdict
       "collected": int,      # number of tests collected (0 = vacuous)
       "fail_kind": str,      # "missing-impl" | "broken-test" | "passed" |
                              #   "no-tests" | "error"
       "imports_target": bool,# imports at least one named target module
       "output": str}         # tail of the run output

    ok requires: non-vacuous AND fails-for-the-right-reason (missing-impl) AND
    (if targets given) imports a target. A test that PASSES here is NOT ok:
    against absent code, passing means it asserts nothing real."""
    wd = Path(workspace)
    test_path = wd / f"{test_module}.py"
    if not test_path.is_file():
        return {"ok": False, "reason": f"{test_module}.py not found",
                "collected": 0, "fail_kind": "error",
                "imports_target": False, "output": ""}

    # 1. non-vacuous (reuse the verifier's loader-based count)
    counts = verifier_mod.count_tests(wd, [test_module]) or {}
    collected = counts.get(test_module, 0)

    # 3. structural sense: imports a target module. Normalize targets first:
    # a weak planner sometimes names the TEST file as its own target
    # ("test_store.py") instead of the module under test ("store.py"); strip a
    # leading "test_" so the check is against the real module, and drop any
    # target that resolves to the test module itself (nothing to import).
    imports_target = True
    norm_targets = set()
    for t in targets or []:
        stem = Path(t).stem
        if stem.startswith("test_"):
            stem = stem[len("test_"):]
        if stem and stem != test_module:
            norm_targets.add(stem)
    if norm_targets:
        imported = _module_imports(test_path)
        imports_target = bool(imported & norm_targets)

    # 2. run it and classify the failure
    cmd = [sys.executable, "-m", "unittest", "-v", test_module]
    try:
        proc = subprocess.run(cmd, cwd=str(wd), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "test run timed out", "collected": collected,
                "fail_kind": "error", "imports_target": imports_target,
                "output": "(timed out)"}
    out = (proc.stdout + "\n" + proc.stderr).strip()

    if collected == 0:
        return {"ok": False, "reason": "vacuous: collects 0 tests",
                "collected": 0, "fail_kind": "no-tests",
                "imports_target": imports_target, "output": out[-1500:]}

    if any(b in out for b in _BROKEN_TEST):
        return {"ok": False,
                "reason": "broken test (syntax/indentation error), not a real failure",
                "collected": collected, "fail_kind": "broken-test",
                "imports_target": imports_target, "output": out[-1500:]}

    if proc.returncode == 0:
        # Passed against absent implementation: it asserts nothing meaningful.
        return {"ok": False,
                "reason": "passes against absent implementation (asserts nothing real)",
                "collected": collected, "fail_kind": "passed",
                "imports_target": imports_target, "output": out[-1500:]}

    if not any(g in out for g in _GOOD_FAIL):
        return {"ok": False,
                "reason": "failed, but not with a missing-implementation signature",
                "collected": collected, "fail_kind": "error",
                "imports_target": imports_target, "output": out[-1500:]}

    if norm_targets and not imports_target:
        return {"ok": False,
                "reason": "does not import its module(s) under test: " +
                          ", ".join(sorted(f"{m}.py" for m in norm_targets)),
                "collected": collected, "fail_kind": "missing-impl",
                "imports_target": False, "output": out[-1500:]}

    return {"ok": True,
            "reason": f"good fail-first: {collected} real test(s) fail because "
                      f"the implementation is absent",
            "collected": collected, "fail_kind": "missing-impl",
            "imports_target": imports_target, "output": out[-1500:]}
