"""Unit tests for the M1 orchestrator core that need NO model: journal append +
replay + reconstruct + crash-tolerance, plan validation, ready-frontier, and
roster selection. Run: PYTHONPATH=lib python3 tests/test_m1_core.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from hydracoder.journal import Journal  # noqa: E402
from hydracoder.planner import Plan, Task  # noqa: E402
from hydracoder.scheduler import choose_roster  # noqa: E402


def _counter_clock():
    n = {"t": 0.0}
    def c():
        n["t"] += 1.0
        return n["t"]
    return c


def test_journal_append_replay_roundtrip():
    d = tempfile.mkdtemp()
    j = Journal(Path(d), clock=_counter_clock())
    j.append("run_started", {"goal": "g", "models_dir": d})
    j.append("plan_created", {"architecture": "a",
                              "tasks": [{"id": "t1", "title": "x", "depends_on": []}]})
    events = list(j.replay())
    assert [e.kind for e in events] == ["run_started", "plan_created"]
    assert events[0].seq == 1 and events[1].seq == 2


def test_journal_reconstruct_state():
    d = tempfile.mkdtemp()
    j = Journal(Path(d), clock=_counter_clock())
    j.append("run_started", {"goal": "build x", "models_dir": d})
    j.append("plan_created", {"architecture": "a", "tasks": [
        {"id": "t1", "title": "one", "depends_on": []},
        {"id": "t2", "title": "two", "depends_on": ["t1"]}]})
    j.append("task_state", {"task_id": "t1", "state": "done"})
    st = j.reconstruct()
    assert st["goal"] == "build x"
    assert st["tasks"]["t1"]["state"] == "done"
    assert st["tasks"]["t2"]["state"] == "queued"


def test_journal_resume_continues_seq():
    d = tempfile.mkdtemp()
    j1 = Journal(Path(d), clock=_counter_clock())
    j1.append("run_started", {"goal": "g"})
    j1.append("decision", {"what": "x", "why": "y"})
    # New Journal over the same dir == a restart.
    j2 = Journal(Path(d), clock=_counter_clock())
    ev = j2.append("decision", {"what": "z", "why": "w"})
    assert ev.seq == 3, ev.seq  # continues past the existing tail


def test_journal_tolerates_truncated_tail():
    d = tempfile.mkdtemp()
    j = Journal(Path(d), clock=_counter_clock())
    j.append("run_started", {"goal": "g"})
    # Simulate a crash mid-write: append a partial line.
    with open(j.path, "a") as f:
        f.write('{"seq": 2, "ts": 2.0, "kind": "task_sta')  # truncated
    events = list(j.replay())
    assert len(events) == 1  # the good event survives, partial is skipped


def test_journal_subscribe_live():
    d = tempfile.mkdtemp()
    j = Journal(Path(d), clock=_counter_clock())
    seen = []
    unsub = j.subscribe(lambda ev: seen.append(ev.kind))
    j.append("decision", {"what": "a", "why": "b"})
    unsub()
    j.append("decision", {"what": "c", "why": "d"})
    assert seen == ["decision"]  # only events while subscribed


def test_plan_validation_detects_dangling_and_cycle():
    good = Plan("a", [Task("t1", "x"), Task("t2", "y", depends_on=["t1"])])
    assert good.validate() == []
    dangling = Plan("a", [Task("t1", "x", depends_on=["nope"])])
    assert any("unknown" in p for p in dangling.validate())
    cycle = Plan("a", [Task("t1", "x", depends_on=["t2"]),
                       Task("t2", "y", depends_on=["t1"])])
    assert any("cycle" in p for p in cycle.validate())


def test_plan_ready_frontier():
    p = Plan("a", [
        Task("t0", "init"),
        Task("t1", "a", depends_on=["t0"]),
        Task("t2", "b", depends_on=["t0"]),
        Task("t3", "test", depends_on=["t1", "t2"]),
    ])
    assert [t.id for t in p.ready_tasks(set())] == ["t0"]
    frontier = {t.id for t in p.ready_tasks({"t0"})}
    assert frontier == {"t1", "t2"}  # parallel frontier
    assert [t.id for t in p.ready_tasks({"t0", "t1", "t2"})] == ["t3"]


def test_choose_roster_prefers_small_and_moe():
    available = [
        {"id": "tiny", "downloaded": True, "fit": "yes", "size_gb": 3.0},
        {"id": "mid-a4b", "downloaded": True, "fit": "yes", "size_gb": 16.0},
        {"id": "big", "downloaded": True, "fit": "spill", "size_gb": 40.0},
        {"id": "absent", "downloaded": False, "fit": "yes", "size_gb": 5.0},
    ]
    r = choose_roster(available)
    assert r.small == "tiny"
    assert r.boss == "tiny"
    assert r.worker == "mid-a4b"   # MoE preferred for worker
    assert r.reviewer == "big"     # largest for judgment
    # trivial task routes to small, medium to worker
    assert r.for_complexity("trivial") == "tiny"
    assert r.for_complexity("medium") == "mid-a4b"


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
