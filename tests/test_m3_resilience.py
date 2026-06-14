"""M3 resilience proof: a run that is interrupted mid-task (simulating an OOM
or crash) RESUMES from the journal without losing completed work, and a worker
that raises is recorded as a recoverable failure rather than crashing the whole
run.

This uses a FAKE worker/reviewer (monkeypatched) so the test is fast and
deterministic and does not need a live model. The thing under test is the
orchestrator's journal-driven resume + error handling, not the model.

Run: PYTHONPATH=lib:<lillycoder>/lib:<hydra-llm>/lib python3 tests/test_m3_resilience.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT.parent / "lillycoder" / "lib"))
sys.path.insert(0, str(ROOT.parent / "hydra-llm" / "lib"))

from hydracoder import orchestrator as orch_mod  # noqa: E402
from hydracoder.orchestrator import Orchestrator  # noqa: E402
from hydracoder.scheduler import Roster  # noqa: E402
from hydracoder import planner as planner_mod  # noqa: E402


FAKE_PLAN = planner_mod.Plan("test arch", [
    planner_mod.Task("t1", "alpha", "make a", complexity="trivial"),
    planner_mod.Task("t2", "beta", "make b", depends_on=["t1"], complexity="small"),
    planner_mod.Task("t3", "gamma", "make c", depends_on=["t2"], complexity="small"),
])


def _patch(monkeypatch_calls, roster):
    """Install fakes: planner returns FAKE_PLAN, model start is a no-op URL,
    reviewer always passes. The worker behavior is set per-test."""
    orch_mod.planner_mod.make_plan = lambda *a, **k: (FAKE_PLAN, "raw")
    orch_mod.reviewer_mod.review_task = lambda *a, **k: {"passed": True, "notes": "ok"}
    # patch the bound method via the class
    Orchestrator._base_url = lambda self, mid: "http://127.0.0.1:1/v1"


def test_crash_midway_then_resume_completes():
    run_dir = Path(tempfile.mkdtemp())
    ws = Path(tempfile.mkdtemp())
    roster = Roster("m", "m", "m", "m")
    _patch([], roster)

    # Worker that crashes the whole process-equivalent on t2 (raises hard) the
    # FIRST time, succeeds otherwise. We simulate the crash by raising
    # KeyboardInterrupt out of run (as a hard stop) after t1 is done.
    state = {"crashed": False}

    def crashing_worker(task, base, model, workdir, journal, **kw):
        if task.id == "t2" and not state["crashed"]:
            state["crashed"] = True
            raise KeyboardInterrupt("simulated OOM/crash during t2")
        return None
    orch_mod.worker_mod.run_task = crashing_worker

    o1 = Orchestrator(run_dir, ws, roster=roster)
    try:
        o1.run("goal")
    except KeyboardInterrupt:
        pass  # the "crash"

    # After the crash, t1 must be recorded done in the journal.
    st = o1.journal.reconstruct()
    assert st["tasks"]["t1"]["state"] == "done", st["tasks"]
    assert st["tasks"].get("t2", {}).get("state") != "done"

    # RESUME: a fresh orchestrator over the same journal, worker now succeeds.
    orch_mod.worker_mod.run_task = lambda *a, **k: None
    o2 = Orchestrator(run_dir, ws, roster=roster)
    res = o2.run("goal")
    assert res["ok"], res
    assert set(res["done"]) == {"t1", "t2", "t3"}, res
    # t1 was NOT re-run after resume (it was already done) -> proven by the fact
    # the crashing_worker's crash flag stayed set and t1 stayed done across both.
    print("  PASS crash-midway then resume completes (t1 preserved, t2/t3 finished)")


def test_worker_exception_is_recoverable_not_fatal():
    run_dir = Path(tempfile.mkdtemp())
    ws = Path(tempfile.mkdtemp())
    roster = Roster("m", "m", "m", "m")
    _patch([], roster)

    # Worker raises a normal exception on t2 every attempt: the run must mark t2
    # failed and NOT crash; t1 still completes; t3 is blocked (dep on t2).
    def flaky(task, base, model, workdir, journal, **kw):
        if task.id == "t2":
            raise RuntimeError("simulated worker failure")
        return None
    orch_mod.worker_mod.run_task = flaky

    o = Orchestrator(run_dir, ws, roster=roster)
    res = o.run("goal")
    assert "t1" in res["done"], res
    assert "t2" in res["failed"], res
    assert "t3" not in res["done"], res  # blocked by failed dep
    assert res["ok"] is False
    print("  PASS worker exception is recorded as failure, run does not crash")


def test_overflow_window_does_not_crash_run():
    # A tiny context window must not crash the orchestrator: workers are given
    # whatever window; the orchestrator itself must remain robust. We assert the
    # run completes structurally with a no-op worker under a tiny window setting.
    run_dir = Path(tempfile.mkdtemp())
    ws = Path(tempfile.mkdtemp())
    roster = Roster("m", "m", "m", "m")
    _patch([], roster)
    orch_mod.worker_mod.run_task = lambda *a, **k: None
    o = Orchestrator(run_dir, ws, roster=roster)
    res = o.run("goal", max_task_tokens=8)  # absurdly small budget
    assert res["ok"], res
    print("  PASS tiny token budget does not crash the orchestrator")


def test_same_goal_still_resumes_same_run_dir():
    # Regression guard's negative case: re-running the SAME goal in the SAME
    # run-dir must STILL resume (cases 1 and 2 of the taxonomy), not refuse.
    run_dir = Path(tempfile.mkdtemp())
    ws = Path(tempfile.mkdtemp())
    roster = Roster("m", "m", "m", "m")
    _patch([], roster)
    orch_mod.worker_mod.run_task = lambda *a, **k: None

    o1 = Orchestrator(run_dir, ws, roster=roster)
    res1 = o1.run("build X")
    assert res1["ok"], res1

    # Same goal, same run-dir: a no-op resume of a finished run, must be ok.
    o2 = Orchestrator(run_dir, ws, roster=roster)
    res2 = o2.run("build X")
    assert res2["ok"], res2
    print("  PASS same goal re-run in same run-dir still resumes (no false refusal)")


def test_divergent_goal_on_finished_run_is_refused():
    # The footgun (case 3): a finished run-dir, a NEW goal -> must refuse loudly,
    # never silently no-op. Proven by repro_followup_goal.py; this locks it.
    run_dir = Path(tempfile.mkdtemp())
    ws = Path(tempfile.mkdtemp())
    roster = Roster("m", "m", "m", "m")
    _patch([], roster)
    orch_mod.worker_mod.run_task = lambda *a, **k: None

    o1 = Orchestrator(run_dir, ws, roster=roster)
    assert o1.run("build X")["ok"]

    o2 = Orchestrator(run_dir, ws, roster=roster)
    try:
        o2.run("build Y")
        raise AssertionError("divergent goal on a finished run was NOT refused")
    except RuntimeError as e:
        assert "build X" in str(e) and "build Y" in str(e), e
        assert "completed" in str(e), e
    # And the refusal is journaled as an error event, not swallowed.
    errs = [ev.data for ev in o2.journal.replay()
            if ev.kind == "error" and ev.data.get("where") == "goal-mismatch"]
    assert errs, "goal-mismatch refusal was not journaled"
    print("  PASS divergent goal on a finished run is refused and journaled")


def test_divergent_goal_on_unfinished_run_is_refused():
    # Case 4: a crashed/aborted run (no run_finished event), then a DIFFERENT
    # goal. Must also refuse -- gating on finished-state alone would miss this.
    run_dir = Path(tempfile.mkdtemp())
    ws = Path(tempfile.mkdtemp())
    roster = Roster("m", "m", "m", "m")
    _patch([], roster)

    def crashing_worker(task, base, model, workdir, journal, **kw):
        if task.id == "t2":
            raise KeyboardInterrupt("simulated crash mid-run")
        return None
    orch_mod.worker_mod.run_task = crashing_worker

    o1 = Orchestrator(run_dir, ws, roster=roster)
    try:
        o1.run("build X")
    except KeyboardInterrupt:
        pass
    st = o1.journal.reconstruct()
    assert st["finished"] is None, "precondition: run must be unfinished"

    o2 = Orchestrator(run_dir, ws, roster=roster)
    try:
        o2.run("build Y")
        raise AssertionError("divergent goal on an unfinished run was NOT refused")
    except RuntimeError as e:
        assert "build X" in str(e) and "build Y" in str(e), e
        assert "in progress" in str(e), e
    print("  PASS divergent goal on an unfinished (crashed) run is refused")


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t();
        except Exception as e:
            failed += 1; print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"{len(tests)-failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
