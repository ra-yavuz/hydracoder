"""M5 brownfield proof: pointing hydracoder at an EXISTING codebase.

Covers the three pieces that make brownfield runs possible:
  - survey.survey_workspace: deterministic, bounded snapshot for the planner
    (file tree, tests, spec files, README excerpt); empty for greenfield.
  - planner.make_plan: the survey reaches the planner prompt verbatim.
  - verifier: tests in a "tests"/"test" package dir (the common real-repo
    layout) are discovered, counted, vacuous-gated, and merged with the root
    layout; a non-importable test dir is an explicit warning, never silence.

Run: PYTHONPATH=lib:<lillycoder>/lib:<hydra-llm>/lib python3 tests/test_m5_brownfield.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT.parent / "lillycoder" / "lib"))
sys.path.insert(0, str(ROOT.parent / "hydra-llm" / "lib"))

from hydracoder import planner, survey, verifier  # noqa: E402


def _mkws() -> Path:
    return Path(tempfile.mkdtemp())


# --- survey -------------------------------------------------------------------

def test_survey_empty_workspace_is_greenfield():
    ws = _mkws()
    assert survey.survey_workspace(ws) == ""
    (ws / "hydracoder.toml").write_text("version = 1\n")
    assert survey.survey_workspace(ws) == "", "config alone is not a codebase"


def test_survey_reports_tree_tests_specs_and_readme():
    ws = _mkws()
    (ws / "core.py").write_text("def f():\n    return 1\n")
    (ws / "README.md").write_text("# myproj\n\nThe canonical spec is docs/spec.md.\n")
    (ws / "docs").mkdir()
    (ws / "docs" / "spec.md").write_text("# spec\n")
    (ws / "tests").mkdir()
    (ws / "tests" / "__init__.py").write_text("")
    (ws / "tests" / "test_core.py").write_text("import unittest\n")
    (ws / ".git").mkdir()
    (ws / ".git" / "junk").write_text("never listed")
    s = survey.survey_workspace(ws)
    assert "core.py" in s and "docs/spec.md" in s, s
    assert "tests/ directory (importable package)" in s, s
    assert "README (start):" in s and "# myproj" in s, s
    assert "SPEC/DOC FILES" in s, s
    assert "junk" not in s, "skip-dir leaked into the survey"


def test_survey_is_bounded():
    ws = _mkws()
    for i in range(400):
        (ws / f"f{i:03}.py").write_text("x = 1\n")
    s = survey.survey_workspace(ws, max_chars=4000)
    assert len(s) <= 4000, len(s)
    assert "more files" in s, "truncation must be stated, not silent"


# --- planner sees the survey ---------------------------------------------------

def test_make_plan_includes_survey_in_prompt():
    captured = {}

    def fake_complete(base, model, messages, **kw):
        captured["messages"] = messages
        return {"content": '{"architecture": "a", "tasks": '
                           '[{"id": "t1", "title": "x"}]}'}
    orig = planner.llm.complete
    planner.llm.complete = fake_complete
    try:
        plan, _ = planner.make_plan("http://x/v1", "m", "fix the parser",
                                    survey="FILE TREE:\n  core.py")
    finally:
        planner.llm.complete = orig
    assert plan is not None and plan.tasks[0].id == "t1"
    user = captured["messages"][1]["content"]
    assert "WORKSPACE SURVEY" in user and "core.py" in user, user
    assert "ALREADY EXISTS" in captured["messages"][0]["content"], \
        "PLAN_SYSTEM lost its brownfield rule"


def test_make_plan_without_survey_is_unchanged():
    captured = {}

    def fake_complete(base, model, messages, **kw):
        captured["messages"] = messages
        return {"content": '{"architecture": "a", "tasks": [{"id": "t1"}]}'}
    orig = planner.llm.complete
    planner.llm.complete = fake_complete
    try:
        planner.make_plan("http://x/v1", "m", "build a thing")
    finally:
        planner.llm.complete = orig
    assert "WORKSPACE SURVEY" not in captured["messages"][1]["content"]


# --- verifier: package-dir test layouts ----------------------------------------

def _write_pkg_tests(ws: Path, name: str = "tests", body: str = "") -> None:
    d = ws / name
    d.mkdir()
    (d / "__init__.py").write_text("")
    (d / "test_core.py").write_text(body or (
        "import unittest\nfrom core import f\n"
        "class T(unittest.TestCase):\n"
        "    def test_f(self):\n        self.assertEqual(f(), 1)\n"))


def test_verifier_runs_package_dir_layout():
    ws = _mkws()
    (ws / "core.py").write_text("def f():\n    return 1\n")
    _write_pkg_tests(ws)
    r = verifier.run_tests(ws)
    assert r["ran"] and r["passed"], r
    assert r["test_counts"] == {"tests.test_core": 1}, r["test_counts"]


def test_verifier_merges_root_and_package_layouts():
    ws = _mkws()
    (ws / "core.py").write_text("def f():\n    return 1\n")
    (ws / "test_root.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n"
        "    def test_a(self):\n        self.assertTrue(True)\n")
    _write_pkg_tests(ws)
    r = verifier.run_tests(ws)
    assert r["ran"] and r["passed"], r
    assert r["test_counts"] == {"test_root": 1, "tests.test_core": 1}, r


def test_verifier_vacuous_inside_package_dir():
    ws = _mkws()
    (ws / "core.py").write_text("def f():\n    return 1\n")
    _write_pkg_tests(ws)
    (ws / "tests" / "test_empty.py").write_text("import unittest\n")
    r = verifier.run_tests(ws)
    assert r["ran"] and not r["passed"], r
    assert r["vacuous"] == ["tests.test_empty"], r["vacuous"]
    assert "tests/test_empty.py" in r["output"], r["output"]


def test_verifier_import_error_in_package_dir_not_vacuous():
    ws = _mkws()
    _write_pkg_tests(ws, body="import does_not_exist_xyz\n")
    r = verifier.run_tests(ws)
    assert r["ran"] and not r["passed"], r
    assert r["vacuous"] == [], r
    assert "does_not_exist_xyz" in r["output"], r["output"]


def test_verifier_warns_on_non_importable_dir():
    ws = _mkws()
    d = ws / "tests"
    d.mkdir()
    (d / "test_core.py").write_text("import unittest\n")  # no __init__.py
    r = verifier.run_tests(ws)
    assert r["ran"] is False and r["passed"] is False, r
    assert "__init__.py" in r["summary"], r["summary"]


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
