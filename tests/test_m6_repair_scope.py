"""M6 repair-scope proof: a repair round that edits code the failing tests
never implicated is detected (and, when enforcing, reverted).

This is the structural answer to the gate-is-only-as-good-as-the-suite hole
(project-state caveats 9, 11): a worker fixing a bug ALSO refactored an
adjacent function, silently changing behavior the suite did not cover. The
scope gate is deterministic (git + traceback parsing, no model) and flags
the collateral the test result alone cannot.

Covers:
  - implicated_files: parses workspace-relative .py paths from tracebacks,
    ignores stdlib frames.
  - classify_scope: in-scope vs out-of-scope vs unflagged test edits, with the
    brownfield test-edit rule.
  - RepairScopeTracker: snapshot / changed_files / revert on a real workspace.
  - the orchestrator wiring: a collateral-editing repair worker produces a
    repair_scope violation event; enforcing reverts the collateral.

Run: PYTHONPATH=lib:<lillycoder>/lib:<hydra-llm>/lib python3 tests/test_m6_repair_scope.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT.parent / "lillycoder" / "lib"))
sys.path.insert(0, str(ROOT.parent / "hydra-llm" / "lib"))

from hydracoder import orchestrator as orch_mod  # noqa: E402
from hydracoder import repair_scope as rs  # noqa: E402
from hydracoder import config as config_mod  # noqa: E402
from hydracoder import planner as planner_mod  # noqa: E402
from hydracoder.orchestrator import Orchestrator  # noqa: E402
from hydracoder.scheduler import Roster  # noqa: E402


def _mkws():
    return Path(tempfile.mkdtemp())


# --- pure helpers -----------------------------------------------------------

def test_implicated_files_parses_tracebacks():
    ws = _mkws()
    (ws / "store.py").write_text("x = 1\n")
    (ws / "test_store.py").write_text("x = 1\n")
    out = (
        'FAIL: test_add (test_store.TestStore.test_add)\n'
        '  File "' + str(ws / "test_store.py") + '", line 5, in test_add\n'
        '  File "' + str(ws / "store.py") + '", line 9, in add\n'
        '  File "/usr/lib/python3.14/unittest/case.py", line 1, in run\n'
    )
    imp = rs.implicated_files(out, ws)
    assert imp == {"store.py", "test_store.py"}, imp  # stdlib frame excluded


def test_implicated_files_handles_relative_paths():
    ws = _mkws()
    (ws / "mod.py").write_text("x = 1\n")
    out = '  File "mod.py", line 3, in f\n'
    assert rs.implicated_files(out, ws) == {"mod.py"}


def test_implicated_files_includes_modules_imported_by_failing_test():
    # A plain assertEqual failure names ONLY the test file in its traceback,
    # not the module under test. The module under test must still be
    # implicated (via the test's imports), or fixing it reads as collateral.
    ws = _mkws()
    (ws / "parser.py").write_text("def parse(x):\n    return x - 1\n")
    (ws / "helper.py").write_text("def h():\n    return 1\n")
    (ws / "test_parser.py").write_text(
        "import unittest\nfrom parser import parse\nimport helper\n"
        "class T(unittest.TestCase):\n"
        "    def test_p(self):\n        self.assertEqual(parse(5), 5)\n")
    # Only the test file appears in the assertion-failure traceback.
    out = '  File "' + str(ws / "test_parser.py") + '", line 6, in test_p\n'
    imp = rs.implicated_files(out, ws)
    assert "parser.py" in imp, imp     # imported -> editable
    assert "helper.py" in imp, imp     # imported -> editable
    assert "test_parser.py" in imp, imp


def test_classify_scope_flags_collateral():
    # The exact failure: fixing parser.py is fine, touching server.py is not.
    v = rs.classify_scope(
        changed=["parser.py", "server.py"],
        implicated={"parser.py", "test_parser.py"},
        brownfield=True)
    assert v["in_scope"] == ["parser.py"], v
    assert v["out_of_scope"] == ["server.py"], v
    assert v["violation"] is True, v


def test_classify_scope_clean_round_is_no_violation():
    v = rs.classify_scope(
        changed=["parser.py"],
        implicated={"parser.py", "test_parser.py"},
        brownfield=True)
    assert v["violation"] is False, v
    assert v["out_of_scope"] == [], v


def test_classify_scope_brownfield_forbids_unflagged_test_edits():
    # Editing a human test file that was NOT itself failing is a violation
    # in brownfield (do not bend a suite the failures did not flag).
    v = rs.classify_scope(
        changed=["test_other.py"],
        implicated={"parser.py", "test_parser.py"},  # test_other not failing
        brownfield=True)
    assert v["test_edits"] == ["test_other.py"], v
    assert v["violation"] is True, v
    # Greenfield is lenient: a worker writing its own tests is expected.
    v2 = rs.classify_scope(["test_other.py"], {"parser.py"}, brownfield=False)
    assert v2["violation"] is False, v2


def test_classify_scope_failing_test_may_be_fixed():
    # A test file that WAS failing may be corrected (defective-test repair).
    v = rs.classify_scope(
        changed=["test_parser.py"],
        implicated={"parser.py", "test_parser.py"},
        brownfield=True)
    assert v["violation"] is False, v
    assert v["test_edits"] == [], v


def test_classify_scope_ignores_non_python():
    v = rs.classify_scope(["data.json", "parser.py"], {"parser.py"},
                          brownfield=True)
    assert v["violation"] is False, v


# --- git tracker on a real workspace ----------------------------------------

def test_tracker_detects_changed_files():
    if not rs.available():
        print("  SKIP tracker test: git not available")
        return
    ws = _mkws()
    (ws / "a.py").write_text("a = 1\n")
    (ws / "b.py").write_text("b = 1\n")
    t = rs.RepairScopeTracker(ws)
    assert t.snapshot(), "snapshot failed"
    assert t.changed_files() == [], "nothing changed yet"
    (ws / "b.py").write_text("b = 2\n")
    (ws / "c.py").write_text("c = 3\n")
    changed = set(t.changed_files())
    assert changed == {"b.py", "c.py"}, changed


def test_tracker_revert_restores_snapshot():
    if not rs.available():
        print("  SKIP revert test: git not available")
        return
    ws = _mkws()
    (ws / "a.py").write_text("original\n")
    t = rs.RepairScopeTracker(ws)
    assert t.snapshot()
    (ws / "a.py").write_text("MODIFIED\n")
    (ws / "new.py").write_text("created during round\n")
    assert t.revert_to_snapshot(), "revert failed"
    assert (ws / "a.py").read_text() == "original\n", "edit not reverted"
    assert not (ws / "new.py").exists(), "new file not removed"


def test_tracker_rebase_judges_each_round_alone():
    if not rs.available():
        print("  SKIP rebase test: git not available")
        return
    ws = _mkws()
    (ws / "a.py").write_text("1\n")
    t = rs.RepairScopeTracker(ws)
    assert t.snapshot()
    (ws / "a.py").write_text("2\n")
    assert set(t.changed_files()) == {"a.py"}
    t.rebase_to_current()
    assert t.changed_files() == [], "rebase did not advance the baseline"
    (ws / "b.py").write_text("9\n")
    assert set(t.changed_files()) == {"b.py"}, "next round saw stale changes"


# --- orchestrator wiring (fakes, no model) ----------------------------------

def test_orchestrator_flags_collateral_repair():
    if not rs.available():
        print("  SKIP wiring test: git not available")
        return
    run_dir, ws = _mkws(), _mkws()
    # An existing project (brownfield) with a bug in parser.py and a test that
    # fails pointing only at parser.py / test_parser.py.
    (ws / "parser.py").write_text("def parse(x):\n    return x - 1  # bug\n")
    (ws / "server.py").write_text("def serve():\n    return 'ok'\n")
    (ws / "test_parser.py").write_text(
        "import unittest\nfrom parser import parse\n"
        "class T(unittest.TestCase):\n"
        "    def test_p(self):\n        self.assertEqual(parse(5), 5)\n")

    orch_mod.planner_mod.make_plan = lambda *a, **k: (
        planner_mod.Plan("a", [planner_mod.Task("t1", "x")]), "raw")
    orch_mod.reviewer_mod.review_task = lambda *a, **k: {"passed": True, "notes": ""}
    Orchestrator._base_url = lambda self, mid: "http://127.0.0.1:1/v1"

    # Build tasks leave the bug in place (so final verification fails and the
    # repair loop runs). The REPAIR worker fixes parser.py (in scope) AND
    # edits server.py (collateral the failing test never implicated).
    def collateral_worker(task, base, model, workdir, journal, **kw):
        if not task.id.startswith("fix"):
            return  # build task: no-op, leave the planted bug
        wd = Path(workdir)
        (wd / "parser.py").write_text("def parse(x):\n    return x  # fixed\n")
        (wd / "server.py").write_text("def serve():\n    return 'CHANGED'\n")
    orch_mod.worker_mod.run_task = collateral_worker

    cfg = config_mod.from_dict({"run": {"reviewer_enabled": False}})
    o = Orchestrator(run_dir, ws, roster=Roster("m", "m", "m", "m"), config=cfg)
    o.run("fix the parser")

    events = [ev for ev in o.journal.replay() if ev.kind == "repair_scope"]
    assert events, "no repair_scope event journaled"
    first = events[0].data
    assert first["violation"] is True, first
    assert first["out_of_scope"] == ["server.py"], first
    assert "parser.py" in first["in_scope"], first
    # Default is flag-only: the collateral edit is NOT reverted.
    assert (ws / "server.py").read_text() == "def serve():\n    return 'CHANGED'\n", \
        "flag-only mode wrongly reverted"


def test_orchestrator_enforce_reverts_collateral():
    if not rs.available():
        print("  SKIP enforce test: git not available")
        return
    run_dir, ws = _mkws(), _mkws()
    (ws / "parser.py").write_text("def parse(x):\n    return x - 1\n")
    (ws / "server.py").write_text("ORIGINAL\n")
    (ws / "test_parser.py").write_text(
        "import unittest\nfrom parser import parse\n"
        "class T(unittest.TestCase):\n"
        "    def test_p(self):\n        self.assertEqual(parse(5), 5)\n")

    orch_mod.planner_mod.make_plan = lambda *a, **k: (
        planner_mod.Plan("a", [planner_mod.Task("t1", "x")]), "raw")
    orch_mod.reviewer_mod.review_task = lambda *a, **k: {"passed": True, "notes": ""}
    Orchestrator._base_url = lambda self, mid: "http://127.0.0.1:1/v1"

    def collateral_worker(task, base, model, workdir, journal, **kw):
        if not task.id.startswith("fix"):
            return  # build task: leave the planted bug for repair
        wd = Path(workdir)
        (wd / "parser.py").write_text("def parse(x):\n    return x\n")
        (wd / "server.py").write_text("CHANGED\n")
    orch_mod.worker_mod.run_task = collateral_worker

    cfg = config_mod.from_dict({"run": {"reviewer_enabled": False,
                                        "enforce_repair_scope": True}})
    o = Orchestrator(run_dir, ws, roster=Roster("m", "m", "m", "m"), config=cfg)
    o.run("fix the parser")

    events = [ev for ev in o.journal.replay() if ev.kind == "repair_scope"]
    assert events and events[0].data["violation"], events
    decisions = [ev.data for ev in o.journal.replay()
                 if ev.kind == "decision"
                 and ev.data.get("what") == "repair-scope-enforced"]
    assert decisions, "enforcement decision not journaled"
    # Enforcement reverted the whole round (collateral AND the in-scope fix),
    # restoring the snapshot so the next round retries clean.
    assert (ws / "server.py").read_text() == "ORIGINAL\n", "collateral not reverted"


def test_config_accepts_scope_knobs():
    c = config_mod.from_dict({"run": {"check_repair_scope": False,
                                      "enforce_repair_scope": True}})
    assert c.run.check_repair_scope is False
    assert c.run.enforce_repair_scope is True
    # wrong type is still refused
    try:
        config_mod.from_dict({"run": {"enforce_repair_scope": "yes"}})
        raise AssertionError("non-bool accepted")
    except ValueError as e:
        assert "enforce_repair_scope" in str(e), e


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
