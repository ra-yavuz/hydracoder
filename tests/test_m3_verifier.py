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
