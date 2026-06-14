"""M7 test-first proof: tests are authored and PROVEN to fail-first before any
implementation exists, so the gate has a real suite to gate on.

This strengthens the framework's weak point (the gate is only as strong as the
suite) at its root. The audit is deterministic: a test is accepted only if it
is non-vacuous AND fails for a missing-implementation reason (Import/Attribute/
AssertionError) AND imports its target. A test that PASSES against absent code,
or fails with SyntaxError, is rejected.

Covers:
  - audit_test_file: every classification branch (good fail-first, vacuous,
    syntax-broken, passes-against-absent, wrong-failure-kind, missing import).
  - planner test_first: emits kind="test" tasks ordered before code tasks.
  - orchestrator: a test task is audited; a bad test file fails the task; the
    repair loop fixes a fixable one.

Run: PYTHONPATH=lib:<lillycoder>/lib:<hydra-llm>/lib python3 tests/test_m7_test_first.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT.parent / "lillycoder" / "lib"))
sys.path.insert(0, str(ROOT.parent / "hydra-llm" / "lib"))

from hydracoder import test_audit as ta  # noqa: E402
from hydracoder import orchestrator as orch_mod  # noqa: E402
from hydracoder import config as config_mod  # noqa: E402
from hydracoder import planner as planner_mod  # noqa: E402
from hydracoder.orchestrator import Orchestrator  # noqa: E402
from hydracoder.scheduler import Roster  # noqa: E402


# Captured before any test patches it, so planner tests always reach the real
# implementation even though orchestrator-wiring tests stub make_plan globally.
_REAL_MAKE_PLAN = planner_mod.make_plan


def _mkws():
    return Path(tempfile.mkdtemp())


# --- the audit classifier ---------------------------------------------------

def test_good_fail_first_is_accepted():
    # A real test against an ABSENT module: imports fail -> good fail-first.
    ws = _mkws()
    (ws / "test_store.py").write_text(
        "import unittest\nfrom store import Store\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        s = Store()\n        s.add('x')\n"
        "        self.assertEqual(s.list(), ['x'])\n")
    r = ta.audit_test_file(ws, "test_store", targets=["store.py"])
    assert r["ok"], r
    assert r["fail_kind"] == "missing-impl", r
    assert r["collected"] == 1, r


def test_vacuous_test_is_rejected():
    ws = _mkws()
    (ws / "test_store.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    pass\n")
    r = ta.audit_test_file(ws, "test_store", targets=["store.py"])
    assert not r["ok"] and r["fail_kind"] == "no-tests", r


def test_syntax_broken_test_is_rejected():
    ws = _mkws()
    (ws / "test_store.py").write_text(
        "import unittest\nfrom store import Store\n"
        "class T(unittest.TestCase):\n"
        "    def test_x(self)\n        pass\n")  # missing colon
    r = ta.audit_test_file(ws, "test_store", targets=["store.py"])
    assert not r["ok"] and r["fail_kind"] == "broken-test", r


def test_passing_against_absent_impl_is_rejected():
    # A "test" that asserts nothing about absent code passes -> rejected.
    ws = _mkws()
    (ws / "test_store.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n"
        "    def test_trivial(self):\n        self.assertTrue(True)\n")
    r = ta.audit_test_file(ws, "test_store", targets=["store.py"])
    assert not r["ok"] and r["fail_kind"] == "passed", r


def test_missing_target_import_is_rejected():
    # Fails (good) but never imports the module it is supposed to test.
    ws = _mkws()
    (ws / "test_store.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n"
        "    def test_x(self):\n        self.assertEqual(1, 2)\n")
    r = ta.audit_test_file(ws, "test_store", targets=["store.py"])
    assert not r["ok"], r
    assert r["imports_target"] is False, r


def test_audit_normalizes_test_file_named_as_target():
    # Weak planners sometimes name the TEST file as its own target
    # (targets=["test_store.py"]) instead of the module under test. The audit
    # must strip "test_" and check against "store.py", not fail spuriously.
    ws = _mkws()
    (ws / "test_store.py").write_text(
        "import unittest\nfrom store import Store\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(Store().list(), [])\n")
    r = ta.audit_test_file(ws, "test_store", targets=["test_store.py"])
    assert r["ok"], r  # normalized to store.py, which IS imported
    # And if the module is genuinely not imported, it still fails (with the
    # normalized name in the message).
    (ws / "test_store.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n"
        "    def test_x(self):\n        self.assertEqual(1, 2)\n")
    r2 = ta.audit_test_file(ws, "test_store", targets=["test_store.py"])
    assert not r2["ok"] and "store.py" in r2["reason"], r2


def test_audit_no_target_constraint_when_targets_empty():
    # Without declared targets, the import check is skipped (still must
    # fail-first non-vacuously).
    ws = _mkws()
    (ws / "test_x.py").write_text(
        "import unittest\nfrom thing import f\n"
        "class T(unittest.TestCase):\n"
        "    def test_f(self):\n        self.assertEqual(f(), 1)\n")
    r = ta.audit_test_file(ws, "test_x")
    assert r["ok"], r


# --- planner emits test-first plans -----------------------------------------

def test_planner_test_first_emits_test_tasks():
    captured = {}

    def fake_complete(base, model, messages, **kw):
        captured["sys"] = messages[0]["content"]
        return {"content":
                '{"architecture": "a", "tasks": ['
                '{"id": "t1", "title": "test store", "kind": "test", '
                ' "targets": ["store.py"]},'
                '{"id": "t2", "title": "build store", "kind": "code", '
                ' "depends_on": ["t1"]}]}'}
    orig = planner_mod.llm.complete
    planner_mod.llm.complete = fake_complete
    try:
        plan, _ = _REAL_MAKE_PLAN("u", "m", "build a store", test_first=True)
    finally:
        planner_mod.llm.complete = orig
    assert "TEST-FIRST" in captured["sys"], "test-first prompt not used"
    t1, t2 = plan.tasks
    assert t1.kind == "test" and t1.targets == ["store.py"], t1
    assert t2.kind == "code" and "t1" in t2.depends_on, t2


def test_planner_default_is_code_kind():
    def fake_complete(base, model, messages, **kw):
        return {"content": '{"architecture": "a", "tasks": [{"id": "t1"}]}'}
    orig = planner_mod.llm.complete
    planner_mod.llm.complete = fake_complete
    try:
        plan, _ = _REAL_MAKE_PLAN("u", "m", "g")  # test_first defaults off
    finally:
        planner_mod.llm.complete = orig
    assert plan.tasks[0].kind == "code", plan.tasks[0]


# --- orchestrator wiring ----------------------------------------------------

def _patch_plan(tasks):
    # Patch make_plan on the orchestrator's reference only, and keep the real
    # one available so the planner tests (which call it directly) are not
    # clobbered by test ordering.
    orch_mod.planner_mod.make_plan = lambda *a, **k: (
        planner_mod.Plan("a", tasks), "raw")
    orch_mod.reviewer_mod.review_task = lambda *a, **k: {"passed": True, "notes": ""}
    Orchestrator._base_url = lambda self, mid: "http://127.0.0.1:1/v1"


def test_orchestrator_audits_test_task_and_fails_bad_one():
    run_dir, ws = _mkws(), _mkws()
    tasks = [planner_mod.Task("t1", "write store tests", kind="test",
                              targets=["store.py"])]
    _patch_plan(tasks)

    # Worker writes a VACUOUS test: the audit must fail the task.
    def w(task, base, model, workdir, journal, **kw):
        (Path(workdir) / "test_store.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    pass\n")
    orch_mod.worker_mod.run_task = w

    cfg = config_mod.from_dict({"run": {"reviewer_enabled": False,
                                        "test_first": True,
                                        "test_audit_rounds": 0}})
    o = Orchestrator(run_dir, ws, roster=Roster("m", "m", "m", "m"), config=cfg)
    res = o.run("build a store")
    assert "t1" in res["failed"], res
    audits = [ev.data for ev in o.journal.replay() if ev.kind == "test_audit"]
    assert audits and not audits[-1]["ok"], audits


def test_orchestrator_accepts_good_fail_first_test():
    run_dir, ws = _mkws(), _mkws()
    tasks = [planner_mod.Task("t1", "write store tests", kind="test",
                              targets=["store.py"])]
    _patch_plan(tasks)

    def w(task, base, model, workdir, journal, **kw):
        (Path(workdir) / "test_store.py").write_text(
            "import unittest\nfrom store import Store\n"
            "class T(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        s = Store()\n        self.assertEqual(s.list(), [])\n")
    orch_mod.worker_mod.run_task = w

    cfg = config_mod.from_dict({"run": {"reviewer_enabled": False,
                                        "test_first": True}})
    o = Orchestrator(run_dir, ws, roster=Roster("m", "m", "m", "m"), config=cfg)
    res = o.run("build a store")
    assert "t1" in res["done"], res
    audits = [ev.data for ev in o.journal.replay() if ev.kind == "test_audit"]
    assert audits and audits[-1]["ok"] and audits[-1]["fail_kind"] == "missing-impl", audits


def test_orchestrator_repairs_a_fixable_test():
    run_dir, ws = _mkws(), _mkws()
    tasks = [planner_mod.Task("t1", "write store tests", kind="test",
                              targets=["store.py"])]
    _patch_plan(tasks)

    state = {"round": 0}

    def w(task, base, model, workdir, journal, **kw):
        # First write is vacuous; the audit-repair task fixes it.
        p = Path(workdir) / "test_store.py"
        if task.id.startswith("t1-audit"):
            p.write_text(
                "import unittest\nfrom store import Store\n"
                "class T(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(Store().list(), [])\n")
        else:
            p.write_text("import unittest\nclass T(unittest.TestCase):\n    pass\n")
        state["round"] += 1
    orch_mod.worker_mod.run_task = w

    cfg = config_mod.from_dict({"run": {"reviewer_enabled": False,
                                        "test_first": True,
                                        "test_audit_rounds": 2}})
    o = Orchestrator(run_dir, ws, roster=Roster("m", "m", "m", "m"), config=cfg)
    res = o.run("build a store")
    assert "t1" in res["done"], res
    audits = [ev.data for ev in o.journal.replay() if ev.kind == "test_audit"]
    assert audits[-1]["ok"], audits
    assert state["round"] >= 2, "repair worker was not invoked"


def test_config_accepts_test_first_knobs():
    c = config_mod.from_dict({"run": {"test_first": True, "test_audit_rounds": 3}})
    assert c.run.test_first is True and c.run.test_audit_rounds == 3
    try:
        config_mod.from_dict({"run": {"test_first": "yes"}})
        raise AssertionError("non-bool accepted")
    except ValueError as e:
        assert "test_first" in str(e), e


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
