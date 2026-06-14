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

Supported layouts (both may coexist; results are merged):
  - test_*.py in the workspace root (run as modules)
  - a "tests"/"test" package directory with test_*.py AND __init__.py
    (run via `unittest discover`; without __init__.py unittest cannot import
    the directory, which is reported as an explicit warning, never silently)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


# Directories never copied into a disposable verification workspace: they are
# either huge and irrelevant (VCS / caches) or actively harmful to duplicate.
_COPY_SKIP = {".git", "__pycache__", ".pytest_cache", ".mypy_cache",
              ".venv", "venv", "node_modules", ".tox", ".hydracoder-snap"}


def _copy_workspace(src: Path, dst: Path) -> None:
    """Copy the workspace into dst for an isolated test run, skipping VCS and
    cache dirs. Best-effort: an unreadable file is skipped, not fatal, because
    verification must run on whatever source is present."""
    def ignore(_dir, names):
        return [n for n in names if n in _COPY_SKIP]
    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True,
                    ignore_dangling_symlinks=True)


def _data_leak_files(before: set[str], after: set[str]) -> list[str]:
    """Root-level data files the test RUN created that did not exist before:
    the deterministic signature of a non-isolated test (a fixed relative path
    passed to a file-backed object instead of a unique tempfile). __pycache__
    and .pyc are normal interpreter output, not a leak."""
    new = after - before
    out = []
    for name in sorted(new):
        if name.endswith(".pyc") or name == "__pycache__":
            continue
        # Source the worker legitimately writes is .py; anything else a test
        # materialized in the workspace root is leaked state.
        if not name.endswith(".py"):
            out.append(name)
    return out


def find_test_files(workspace: Path) -> list[str]:
    """Root-level test files, by the unittest naming convention."""
    wd = Path(workspace)
    return sorted(p.name for p in wd.glob("test_*.py"))


def find_test_dirs(workspace: Path) -> list[tuple[str, bool]]:
    """Conventional test-package dirs ("test"/"tests") that contain test_*.py,
    with whether unittest can import them (requires __init__.py)."""
    wd = Path(workspace)
    out = []
    for name in ("test", "tests"):
        d = wd / name
        if d.is_dir() and any(d.glob("test_*.py")):
            out.append((name, (d / "__init__.py").is_file()))
    return out


# Runs inside the workspace and prints {"module": collected_test_count}.
# Uses unittest's own loader so the count matches exactly what `-m unittest`
# would execute. A module that fails to import yields a _FailedTest with
# count >= 1, so import errors surface in the real run, not as "vacuous".
_COUNT_SNIPPET = """\
import json, sys, unittest
counts = {}
for m in sys.argv[1:]:
    try:
        counts[m] = unittest.defaultTestLoader.loadTestsFromName(m).countTestCases()
    except Exception:
        counts[m] = 1
print(json.dumps(counts))
"""

# Same idea for a discovered test package: count collected tests per module.
# Import-error placeholders (_FailedTest, module "unittest.loader") are
# attributed back to the expected module named in the test id, so a module
# that fails to import is never misclassified as vacuous.
_DIR_COUNT_SNIPPET = """\
import json, sys, unittest
dirname = sys.argv[1]
expected = sys.argv[2:]
counts = {m: 0 for m in expected}
def walk(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            walk(item)
            continue
        mod = item.__class__.__module__
        if mod.startswith("unittest"):
            tid = item.id()
            for m in expected:
                if m.rsplit(".", 1)[-1] in tid:
                    counts[m] = counts.get(m, 0) + 1
                    break
            continue
        counts[mod] = counts.get(mod, 0) + 1
try:
    walk(unittest.defaultTestLoader.discover(dirname, top_level_dir="."))
except Exception:
    pass
print(json.dumps(counts))
"""


def _run_count(workspace: Path, argv: list[str],
               timeout: float) -> Optional[dict[str, int]]:
    try:
        proc = subprocess.run([sys.executable, "-c", *argv],
                              cwd=str(workspace), capture_output=True,
                              text=True, timeout=timeout)
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
        return None


def count_tests(workspace: Path, modules: list[str],
                timeout: float = 60.0) -> Optional[dict[str, int]]:
    """Collected-test count per root module, via the real unittest loader in a
    subprocess. Returns None when counting itself failed; the caller then
    falls back to the plain run rather than inventing counts."""
    return _run_count(workspace, [_COUNT_SNIPPET, *modules], timeout)


def count_dir_tests(workspace: Path, dirname: str, expected: list[str],
                    timeout: float = 60.0) -> Optional[dict[str, int]]:
    """Collected-test count per module of a discovered test package."""
    return _run_count(workspace, [_DIR_COUNT_SNIPPET, dirname, *expected],
                      timeout)


def run_tests(workspace: Path, test_module: Optional[str] = None,
              timeout: float = 120.0, reject_vacuous: bool = True,
              isolate: bool = True) -> dict:
    """Run the workspace's unittest tests (one root module, or everything the
    supported layouts provide if None).

    Returns a dict:
      {"ran": bool,           # did we find and execute any tests
       "passed": bool,        # all tests passed AND no test file is vacuous
       "returncode": int,
       "output": str,         # combined stdout+stderr, tail-truncated
       "summary": str,        # one-line human summary
       "test_counts": dict|None,  # module -> collected test count (None if
                                  # counting failed; then only the run gates)
       "vacuous": [str],      # test modules that collect ZERO tests
       "leak": [str]}         # root data files a test RUN created (isolation
                              # leak signature); empty when isolate is off
    `ran` is False (and passed False) when there are no runnable test files,
    so a task that was supposed to produce tests but did not is treated as
    not-verified.

    Isolation (`isolate`, on by default): the suite runs in a disposable COPY
    of the workspace, so a test that writes to a fixed relative path (instead
    of a unique tempfile) cannot leave state behind to pollute the next repair
    round. Files the run created in that copy are reported in `leak` and named
    in the output, so the repair loop can tell a worker the failure is a test
    -isolation defect (and that any growing counts are pollution, not real
    expected values), not a code bug. The real workspace is never mutated by a
    verification run. Tests calling run_tests directly may pass isolate=False
    to assert on the legacy in-place behavior.

    Vacuous-test gate (`reject_vacuous`, on by default): a test file that
    collects zero test methods proves nothing but exits 0, so it would pass
    the gate silently. That was the weak link in the original e2e proof
    (a generated test_server.py with no test methods). Such a file now FAILS
    verification, and the summary/output name it so the repair loop can feed
    a worker the exact defect: write real tests for that module.
    """
    src = Path(workspace)
    if isolate:
        tmp_root = Path(tempfile.mkdtemp(prefix="hydracoder-verify-"))
        wd = tmp_root / "ws"
        try:
            _copy_workspace(src, wd)
            before = {p.name for p in wd.iterdir()}
            result = _run_tests_in(wd, test_module, timeout, reject_vacuous)
            after = {p.name for p in wd.iterdir()}
            leak = _data_leak_files(before, after)
            # Rewrite the disposable copy's path back to the real workspace so
            # reported tracebacks name real files. The repair-scope gate parses
            # these paths to decide which files a failure implicates; if they
            # pointed at the temp copy, every repair edit would look unimplicated
            # (out of scope). Isolation must be invisible in the output.
            result["output"] = result["output"].replace(str(wd), str(src))
            _apply_leak(result, leak)
            return result
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)
    return _run_tests_in(src, test_module, timeout, reject_vacuous)


def _apply_leak(result: dict, leak: list[str]) -> None:
    """Record an isolation-leak signature in a result and, when the run failed,
    prepend a precise, named repair hint. Does NOT itself fail a passing run:
    isolation made the leak harmless this round, but if the suite also FAILED
    the leak is the likely cause and the worker must be told so explicitly."""
    result["leak"] = leak
    if not leak or result.get("passed"):
        return
    hint = (
        "TEST ISOLATION DEFECT: the test run created these files in the "
        "workspace: " + ", ".join(leak) + ". The tests use FIXED relative "
        "filenames for file-backed objects instead of a UNIQUE per-test "
        "tempfile, so state leaks between tests and across runs. Any totals "
        "that grow run-to-run are POLLUTION, not the real expected values: "
        "do NOT change the assertions to match polluted numbers. Fix the "
        "TESTS: in setUp create a unique path with "
        "tempfile.TemporaryDirectory() or tempfile.mkstemp(), use it for "
        "every store/file in that test, and remove it in tearDown. Each test "
        "must start from empty state.")
    result["output"] = (result["output"] + "\n\n" + hint).strip()[-2600:]


def _run_tests_in(workspace: Path, test_module: Optional[str],
                  timeout: float, reject_vacuous: bool) -> dict:
    """The actual count+run, in whatever directory `workspace` points at (the
    real workspace, or a disposable copy when run_tests isolates)."""
    wd = Path(workspace)
    root_files = find_test_files(wd)
    dirs = find_test_dirs(wd)

    warnings = [f"{name}/ contains test files but no __init__.py, so unittest "
                f"cannot import it; add {name}/__init__.py"
                for name, importable in dirs if not importable]
    importable_dirs = [name for name, importable in dirs if importable]

    if test_module:
        root_targets = [test_module]
        importable_dirs = []
    else:
        root_targets = [f[:-3] for f in root_files]

    if not root_targets and not importable_dirs:
        summary = warnings[0] if warnings else "no test_*.py files in workspace"
        return {"ran": False, "passed": False, "returncode": -1,
                "output": "\n".join(warnings), "summary": summary,
                "test_counts": None, "vacuous": [], "leak": []}

    # --- count collected tests (the vacuous gate) ---------------------------
    counts: dict[str, int] = {}
    counting_ok = reject_vacuous
    if reject_vacuous:
        if root_targets:
            c = count_tests(wd, root_targets)
            counting_ok = counting_ok and c is not None
            counts.update(c or {})
        for name in importable_dirs:
            expected = [f"{name}.{p.stem}"
                        for p in sorted((wd / name).glob("test_*.py"))]
            c = count_dir_tests(wd, name, expected)
            counting_ok = counting_ok and c is not None
            counts.update(c or {})
    final_counts: Optional[dict[str, int]] = counts if counting_ok else None
    vacuous = sorted(m for m, n in (final_counts or {}).items() if n == 0)

    # --- the actual runs -----------------------------------------------------
    cmds = []
    if root_targets:
        cmds.append([sys.executable, "-m", "unittest", "-v", *root_targets])
    for name in importable_dirs:
        cmds.append([sys.executable, "-m", "unittest", "discover", "-v",
                     "-s", name, "-t", "."])
    outputs: list[str] = []
    rcs: list[int] = []
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, cwd=str(wd), capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ran": True, "passed": False, "returncode": -1,
                    "output": "(tests timed out)", "summary": "tests timed out",
                    "test_counts": final_counts, "vacuous": vacuous, "leak": []}
        outputs.append((proc.stdout + "\n" + proc.stderr).strip())
        rcs.append(proc.returncode)

    out = "\n\n".join(o for o in outputs if o)
    if warnings:
        out = (out + "\n\nWARNING: " + "; ".join(warnings)).strip()
    passed = all(rc == 0 for rc in rcs)
    returncode = next((rc for rc in rcs if rc != 0), 0)
    # The unittest summary line is the last "OK" or "FAILED (...)" per run.
    tails = [o.splitlines()[-1] for o in outputs if o]
    summary = " | ".join(tails) if tails else ("passed" if passed else "failed")

    if vacuous:
        # A green suite that collects zero tests for a module proves nothing;
        # that is a verification failure. State it precisely (and even when the
        # suite already failed for other reasons, append it) so one repair
        # round can fix both the failures and the missing tests.
        detail = ("VACUOUS TESTS: " +
                  ", ".join(m.replace(".", "/") + ".py" for m in vacuous) +
                  " define ZERO test methods. Write real unittest.TestCase "
                  "test_* methods with meaningful assertions covering the "
                  "module under test.")
        out = (out + "\n\n" + detail).strip()
        if passed:
            passed = False
            summary = f"vacuous test files ({', '.join(vacuous)}): 0 tests collected"

    return {
        "ran": True,
        "passed": passed,
        "returncode": returncode,
        "output": out[-2000:],   # keep the journal/feedback lean
        "summary": summary,
        "test_counts": final_counts,
        "vacuous": vacuous,
        "leak": [],              # set by _apply_leak when run_tests isolates
    }
