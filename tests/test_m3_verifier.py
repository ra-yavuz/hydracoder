"""Unit tests for the deterministic verifier (no model). It must: run passing
tests as passed, run failing tests as failed, and report 'not ran' when there
are no test files. Run: PYTHONPATH=lib python3 tests/test_m3_verifier.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from hydracoder import verifier  # noqa: E402


def test_passing_suite_reports_passed():
    wd = Path(tempfile.mkdtemp())
    (wd / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    (wd / "test_mod.py").write_text(
        "import unittest\nfrom mod import add\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
    r = verifier.run_tests(wd)
    assert r["ran"] and r["passed"], r


def test_failing_suite_reports_failed_with_output():
    wd = Path(tempfile.mkdtemp())
    (wd / "mod.py").write_text("def add(a, b):\n    return a - b\n")  # bug
    (wd / "test_mod.py").write_text(
        "import unittest\nfrom mod import add\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
    r = verifier.run_tests(wd)
    assert r["ran"] and not r["passed"], r
    assert "FAIL" in r["output"] or "AssertionError" in r["output"], r["output"]


def test_no_tests_reports_not_ran():
    wd = Path(tempfile.mkdtemp())
    (wd / "mod.py").write_text("x = 1\n")
    r = verifier.run_tests(wd)
    assert r["ran"] is False and r["passed"] is False, r


def test_state_isolation_bug_is_caught():
    # The exact failure mode the model reviewer missed: a test that leaks state
    # via a fixed file. The verifier must catch it deterministically.
    wd = Path(tempfile.mkdtemp())
    (wd / "store.py").write_text(
        "import json, os\n"
        "class S:\n"
        "    def __init__(self, p): self.p=p; self.d=json.load(open(p)) if os.path.exists(p) else []\n"
        "    def add(self, t): self.d.append(t); json.dump(self.d, open(self.p,'w'))\n"
        "    def list(self): return self.d\n")
    (wd / "test_store.py").write_text(
        "import unittest\nfrom store import S\n"
        "class T(unittest.TestCase):\n"
        "    def setUp(self): self.s = S('shared.json')\n"  # fixed file, leaks
        "    def test_add(self): self.s.add('x'); self.assertEqual(len(self.s.list()), 1)\n"
        "    def test_empty(self): self.assertEqual(len(self.s.list()), 0)\n")
    r = verifier.run_tests(wd)
    assert r["ran"] and not r["passed"], r  # the leak makes one test fail


def _leaky_workspace() -> Path:
    """A store + a non-isolated test suite: each test opens a FIXED relative
    file and the store APPENDS, so state leaks across tests and runs. This is
    the exact Build-2 failure observed live on gemma-4-26b."""
    wd = Path(tempfile.mkdtemp())
    (wd / "store.py").write_text(
        "import json, os\n"
        "class S:\n"
        "    def __init__(self, p):\n"
        "        self.p = p\n"
        "        self.d = json.load(open(p)) if os.path.exists(p) else []\n"
        "    def add(self, t):\n"
        "        self.d.append(t); json.dump(self.d, open(self.p, 'w'))\n"
        "    def count(self): return len(self.d)\n")
    (wd / "test_store.py").write_text(
        "import unittest\nfrom store import S\n"
        "class T(unittest.TestCase):\n"
        "    def test_one(self):\n"
        "        s = S('leaked.json'); s.add('x')\n"
        "        self.assertEqual(s.count(), 1)\n")
    return wd


def test_isolation_keeps_real_workspace_clean():
    # The structural fix (CODEX option C): a leaky test must NOT leave files in
    # the real workspace, so a re-run can never see polluted state.
    wd = _leaky_workspace()
    before = {p.name for p in wd.iterdir()}
    verifier.run_tests(wd)                  # isolate=True (default)
    after = {p.name for p in wd.iterdir()}
    created = after - before - {"__pycache__"}
    assert "leaked.json" not in after, f"leak escaped into real workspace: {after}"
    assert not any(n.endswith(".json") for n in created), created


def test_isolation_makes_a_leaky_suite_deterministic_across_runs():
    # The core bug: without isolation, run N sees state from run N-1 and counts
    # grow. With isolation every run starts clean, so the result is stable.
    wd = _leaky_workspace()
    r1 = verifier.run_tests(wd)
    r2 = verifier.run_tests(wd)
    r3 = verifier.run_tests(wd)
    assert r1["passed"] and r2["passed"] and r3["passed"], (r1, r2, r3)
    # And the real workspace still has no leaked data file after 3 runs.
    assert not (wd / "leaked.json").exists(), list(wd.iterdir())


def test_leak_signature_and_hint_on_failure():
    # A leaky suite whose assertion is right but fails ONLY because of its own
    # in-test leak: detect the created file and emit the isolation hint.
    wd = Path(tempfile.mkdtemp())
    (wd / "store.py").write_text(
        "import json, os\n"
        "class S:\n"
        "    def __init__(self, p):\n"
        "        self.p = p\n"
        "        self.d = json.load(open(p)) if os.path.exists(p) else []\n"
        "    def add(self, t):\n"
        "        self.d.append(t); json.dump(self.d, open(self.p, 'w'))\n"
        "    def count(self): return len(self.d)\n")
    # Two tests share ONE fixed file; the second sees the first's write, so its
    # correct expectation (count == 1) fails because state leaked.
    (wd / "test_store.py").write_text(
        "import unittest\nfrom store import S\n"
        "class T(unittest.TestCase):\n"
        "    def test_a_first(self):\n"
        "        s = S('shared.json'); s.add('x'); self.assertEqual(s.count(), 1)\n"
        "    def test_b_second(self):\n"
        "        s = S('shared.json'); s.add('y'); self.assertEqual(s.count(), 1)\n")
    r = verifier.run_tests(wd)
    assert r["ran"] and not r["passed"], r
    assert "shared.json" in r["leak"], r["leak"]
    assert "TEST ISOLATION DEFECT" in r["output"], r["output"]
    assert "tempfile" in r["output"], r["output"]
    assert "POLLUTION" in r["output"], r["output"]


def test_isolate_false_preserves_legacy_in_place_behavior():
    # The opt-out must run in-place (leaving the leaked file) and report no leak
    # signal, so existing direct callers/tests keep their old semantics.
    wd = _leaky_workspace()
    r = verifier.run_tests(wd, isolate=False)
    assert r["leak"] == [], r
    assert (wd / "leaked.json").exists(), "isolate=False should run in place"


def test_vacuous_test_file_fails_verification():
    # The original e2e weak link: a test file with ZERO test methods exits 0
    # and silently passes the gate. It must now fail verification by name.
    wd = Path(tempfile.mkdtemp())
    (wd / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    (wd / "test_mod.py").write_text(
        "import unittest\nfrom mod import add\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
    (wd / "test_server.py").write_text(
        "import unittest\n\nclass TestServer(unittest.TestCase):\n    pass\n")
    r = verifier.run_tests(wd)
    assert r["ran"] and not r["passed"], r
    assert r["vacuous"] == ["test_server"], r
    assert "vacuous" in r["summary"], r["summary"]
    assert "VACUOUS" in r["output"], r["output"]
    assert r["test_counts"]["test_mod"] == 1, r["test_counts"]


def test_vacuous_detail_present_even_when_suite_fails():
    # Vacuous info must reach the repair worker alongside real failures.
    wd = Path(tempfile.mkdtemp())
    (wd / "mod.py").write_text("def add(a, b):\n    return a - b\n")  # bug
    (wd / "test_mod.py").write_text(
        "import unittest\nfrom mod import add\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
    (wd / "test_empty.py").write_text("import unittest\n")
    r = verifier.run_tests(wd)
    assert r["ran"] and not r["passed"], r
    assert r["vacuous"] == ["test_empty"], r
    assert "VACUOUS" in r["output"], r["output"]
    # The real failure stays the primary summary.
    assert "vacuous" not in r["summary"], r["summary"]


def test_vacuous_gate_can_be_disabled():
    # One real test + one vacuous file: exactly the historical e2e hole.
    # With the gate off (explicit opt-out) the suite passes like it used to;
    # the all-vacuous case is NOT tested here because unittest itself exits 5
    # ("NO TESTS RAN") when zero tests run overall.
    wd = Path(tempfile.mkdtemp())
    (wd / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    (wd / "test_mod.py").write_text(
        "import unittest\nfrom mod import add\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
    (wd / "test_empty.py").write_text("import unittest\n")
    r = verifier.run_tests(wd, reject_vacuous=False)
    assert r["ran"] and r["passed"], r  # old behavior, explicitly opted into
    assert r["vacuous"] == [] and r["test_counts"] is None, r


def test_import_error_is_a_failure_not_vacuous():
    # A test module that cannot import must fail the run with the traceback,
    # not be misclassified as vacuous (loader yields a _FailedTest, count 1).
    wd = Path(tempfile.mkdtemp())
    (wd / "test_broken.py").write_text("import does_not_exist_xyz\n")
    r = verifier.run_tests(wd)
    assert r["ran"] and not r["passed"], r
    assert r["vacuous"] == [], r
    assert "does_not_exist_xyz" in r["output"], r["output"]


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t(); print(f"  PASS {t.__name__}")
        except Exception as e:
            failed += 1; print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"{len(tests)-failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
