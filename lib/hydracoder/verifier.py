"""Deterministic verification: actually RUN a task's tests instead of asking a
model whether they pass.

A model reviewer guesses from reading code and, on local models, guesses badly
(it will hallucinate a "syntax error" in a file that parses fine, and miss a
real test-isolation bug). A test runner does not guess: it executes the code
and reports the truth. So whenever a task produces a runnable Python test file,
the orchestrator verifies it here first; the model reviewer becomes an advisory
second opinion, not the gate.

This module shells out to `python3 -m unittest` (stdlib, always present) in the
workspace, with a timeout, and returns a structured result the orchestrator can
journal and feed back to a worker on failure.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional


def find_test_files(workspace: Path) -> list[str]:
    """Test files in the workspace, by the unittest naming convention."""
    wd = Path(workspace)
    return sorted(p.name for p in wd.glob("test_*.py"))


def run_tests(workspace: Path, test_module: Optional[str] = None,
              timeout: float = 120.0) -> dict:
    """Run the workspace's unittest tests (one module, or all `test_*` if None).

    Returns a dict:
      {"ran": bool,           # did we find and execute any tests
       "passed": bool,        # all tests passed
       "returncode": int,
       "output": str,         # combined stdout+stderr, tail-truncated
       "summary": str}        # one-line human summary
    `ran` is False (and passed False) when there are no test files, so a task
    that was supposed to produce tests but did not is treated as not-verified.
    """
    wd = Path(workspace)
    files = find_test_files(wd)
    if not files:
        return {"ran": False, "passed": False, "returncode": -1,
                "output": "", "summary": "no test_*.py files in workspace"}

    if test_module:
        target = [test_module]
    else:
        # discover all test modules by name (drop the .py)
        target = [f[:-3] for f in files]

    cmd = [sys.executable, "-m", "unittest", "-v", *target]
    try:
        proc = subprocess.run(cmd, cwd=str(wd), capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ran": True, "passed": False, "returncode": -1,
                "output": "(tests timed out)", "summary": "tests timed out"}
    out = (proc.stdout + "\n" + proc.stderr).strip()
    passed = proc.returncode == 0
    # The unittest summary line is the last "OK" or "FAILED (...)".
    tail = out.splitlines()[-1] if out else ""
    return {
        "ran": True,
        "passed": passed,
        "returncode": proc.returncode,
        "output": out[-2000:],   # keep the journal/feedback lean
        "summary": tail or ("passed" if passed else "failed"),
    }
