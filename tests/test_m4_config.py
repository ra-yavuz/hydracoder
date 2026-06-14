"""M4 configurability proof: hydracoder.toml parsing (strict), tool-profile
resolution, and the orchestrator actually honoring the config: per-complexity
tool allowlists, reviewer toggle, token budget, roster validation, and the
resume rule (journaled effective config beats the workspace file, because the
file lives in the model-writable workspace).

Uses FAKE workers/planner/models like test_m3_resilience: no live model.

Run: PYTHONPATH=lib:<lillycoder>/lib:<hydra-llm>/lib python3 tests/test_m4_config.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT.parent / "lillycoder" / "lib"))
sys.path.insert(0, str(ROOT.parent / "hydra-llm" / "lib"))

from hydracoder import config as config_mod  # noqa: E402
from hydracoder import orchestrator as orch_mod  # noqa: E402
from hydracoder import planner as planner_mod  # noqa: E402
from hydracoder.orchestrator import Orchestrator  # noqa: E402
from hydracoder.scheduler import Roster  # noqa: E402
from hydracoder.worker import DEV_TOOLS, FILE_TOOLS  # noqa: E402


# --- config parsing ---------------------------------------------------------

def test_defaults_when_no_file():
    ws = Path(tempfile.mkdtemp())
    c = config_mod.load_config(ws)
    assert c.source is None
    assert c.tools_for_complexity("medium") == DEV_TOOLS
    assert c.run.repair_rounds == 3 and c.run.reviewer_enabled is True
    assert c.roster == {}


def test_valid_file_parsed():
    ws = Path(tempfile.mkdtemp())
    (ws / "hydracoder.toml").write_text(
        'version = 1\n'
        '[roster]\nworker = "m-big"\n'
        '[run]\nrepair_rounds = 5\nreviewer_enabled = false\n'
        'max_task_tokens = 777\n'
        '[tools]\ndefault = "dev"\ntrivial = "restricted"\n'
        'large = ["read_file", "write_file", "bash"]\n')
    c = config_mod.load_config(ws)
    assert c.source == str(ws / "hydracoder.toml")
    assert c.roster == {"worker": "m-big"}
    assert c.run.repair_rounds == 5 and c.run.reviewer_enabled is False
    assert c.run.max_task_tokens == 777
    assert c.tools_for_complexity("trivial") == FILE_TOOLS
    assert c.tools_for_complexity("small") == DEV_TOOLS      # falls to default
    assert c.tools_for_complexity("large") == ["read_file", "write_file", "bash"]


def test_profiles_dev_vs_all():
    # "dev" must include the shell but never the self-modification tools;
    # "all" is the explicit opt-in that exposes the full registry.
    everything = set(config_mod.registered_tool_names())
    assert "bash" in DEV_TOOLS and "rm" in DEV_TOOLS
    assert "pkg_install" not in DEV_TOOLS
    assert not any(t in DEV_TOOLS for t in everything
                   if "persona" in t), DEV_TOOLS
    assert set(config_mod._resolve_profile("all")) == everything
    assert "pkg_install" in everything  # i.e. "all" really does grant it


def test_strict_validation_collects_problems():
    ws = Path(tempfile.mkdtemp())
    (ws / "hydracoder.toml").write_text(
        '[runn]\nx = 1\n'                       # unknown section
        '[run]\nrepair_round = 2\n'             # typo'd key
        'max_task_tokens = true\n'              # wrong type
        '[tools]\ndefault = "everything"\n'     # unknown profile
        'small = ["read_file", "teleport"]\n')  # unknown tool name
    try:
        config_mod.load_config(ws)
        raise AssertionError("invalid config was accepted")
    except ValueError as e:
        msg = str(e)
    for needle in ("runn", "repair_round", "max_task_tokens",
                   "everything", "teleport"):
        assert needle in msg, f"missing {needle!r} in: {msg}"


def test_normalized_roundtrip_and_legacy():
    ws = Path(tempfile.mkdtemp())
    (ws / "hydracoder.toml").write_text(
        '[roster]\nboss = "b1"\n[run]\nmax_nudges = 0\n'
        '[tools]\ndefault = "restricted"\n')
    c = config_mod.load_config(ws)
    c2 = config_mod.from_normalized(c.normalized())
    assert c2.roster == c.roster and c2.run == c.run and c2.tools == c.tools
    legacy = config_mod.legacy_resume_config()
    assert legacy.tools_for_complexity("large") == FILE_TOOLS


# --- orchestrator wiring (fakes, no model) ----------------------------------

FAKE_PLAN = planner_mod.Plan("test arch", [
    planner_mod.Task("t1", "alpha", "make a", complexity="trivial"),
    planner_mod.Task("t2", "beta", "make b", depends_on=["t1"], complexity="small"),
])


def _patch(worker_fn, review_calls=None):
    orch_mod.planner_mod.make_plan = lambda *a, **k: (FAKE_PLAN, "raw")

    def fake_review(*a, **k):
        if review_calls is not None:
            review_calls.append(1)
        return {"passed": True, "notes": "ok"}
    orch_mod.reviewer_mod.review_task = fake_review
    Orchestrator._base_url = lambda self, mid: "http://127.0.0.1:1/v1"
    orch_mod.worker_mod.run_task = worker_fn


def test_run_honors_tools_tokens_and_journals_config():
    run_dir, ws = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    (ws / "hydracoder.toml").write_text(
        '[run]\nmax_task_tokens = 777\n'
        '[tools]\ndefault = "dev"\ntrivial = "restricted"\n')
    captured = {}

    def w(task, base, model, workdir, journal, **kw):
        captured[task.id] = kw
    _patch(w)
    o = Orchestrator(run_dir, ws, roster=Roster("m", "m", "m", "m"))
    res = o.run("goal")
    assert res["ok"], res
    assert captured["t1"]["tool_subset"] == FILE_TOOLS, captured["t1"]
    assert captured["t2"]["tool_subset"] == DEV_TOOLS, captured["t2"]
    assert captured["t1"]["max_tokens"] == 777
    started = [ev for ev in o.journal.replay() if ev.kind == "run_started"]
    assert started and started[0].data["config"]["tools"]["trivial"] == FILE_TOOLS
    assert started[0].data["config"]["run"]["max_task_tokens"] == 777


def test_reviewer_can_be_disabled():
    run_dir, ws = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    (ws / "hydracoder.toml").write_text('[run]\nreviewer_enabled = false\n')
    calls = []
    _patch(lambda *a, **k: None, review_calls=calls)
    o = Orchestrator(run_dir, ws, roster=Roster("m", "m", "m", "m"))
    res = o.run("goal")
    assert res["ok"], res
    assert calls == [], "reviewer ran despite reviewer_enabled = false"
    states = [ev.data["state"] for ev in o.journal.replay()
              if ev.kind == "task_state"]
    assert "review" not in states, states


def test_resume_uses_journaled_config_not_edited_file():
    # The workspace file is model-writable; a resume must run with what the
    # run STARTED with, not with whatever the file says now.
    run_dir, ws = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    (ws / "hydracoder.toml").write_text('[tools]\ndefault = "restricted"\n')
    crashed = {"done": False}

    def crashing(task, base, model, workdir, journal, **kw):
        if task.id == "t2" and not crashed["done"]:
            crashed["done"] = True
            raise KeyboardInterrupt("simulated crash")
    _patch(crashing)
    o1 = Orchestrator(run_dir, ws, roster=Roster("m", "m", "m", "m"))
    try:
        o1.run("goal")
    except KeyboardInterrupt:
        pass

    # A worker (or anyone) widens the file between crash and resume.
    (ws / "hydracoder.toml").write_text('[tools]\ndefault = "dev"\n')
    captured = {}

    def w(task, base, model, workdir, journal, **kw):
        captured[task.id] = kw
    _patch(w)
    o2 = Orchestrator(run_dir, ws, roster=Roster("m", "m", "m", "m"))
    res = o2.run("goal")
    assert res["ok"], res
    assert captured["t2"]["tool_subset"] == FILE_TOOLS, \
        f"resume used the edited file, not the journal: {captured['t2']}"
    decisions = [ev.data for ev in o2.journal.replay()
                 if ev.kind == "decision" and ev.data.get("what") == "resume-config"]
    assert decisions, "resume-config decision not journaled"


def test_roster_config_is_validated_against_usable_models():
    run_dir, ws = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    _patch(lambda *a, **k: None)
    orch_mod.hydra.list_models = lambda: [
        {"id": "m1", "downloaded": True, "fit": "yes", "size_gb": 1.0},
        {"id": "m-gone", "downloaded": False, "fit": "yes", "size_gb": 1.0},
    ]
    cfg = config_mod.from_dict({"roster": {"worker": "m-gone"}})
    o = Orchestrator(run_dir, ws, config=cfg)
    try:
        o.run("goal")
        raise AssertionError("undownloaded roster model was accepted")
    except RuntimeError as e:
        assert "m-gone" in str(e), e
    cfg2 = config_mod.from_dict({"roster": {"worker": "m1"}})
    o2 = Orchestrator(Path(tempfile.mkdtemp()), ws, config=cfg2)
    res = o2.run("goal")
    assert res["ok"], res
    assert o2._roster.worker == "m1"


def test_bash_deny_default_and_override():
    import re
    # default config carries the install/network deny patterns, and they catch
    # the indirect sidestep (make venv) without catching benign test commands.
    c = config_mod.Config()
    assert c.run.bash_deny, "default bash_deny should be non-empty"
    assert any(re.search(p, "make venv") for p in c.run.bash_deny)
    assert not any(re.search(p, "python3 -m unittest t") for p in c.run.bash_deny)
    # explicit override is honored, and validated as a list of strings
    c2 = config_mod.from_dict({"run": {"bash_deny": [r"\brm\b"]}})
    assert c2.run.bash_deny == [r"\brm\b"], c2.run.bash_deny
    # empty list = allow everything the safety layer permits
    c3 = config_mod.from_dict({"run": {"bash_deny": []}})
    assert c3.run.bash_deny == [], c3.run.bash_deny
    # wrong type is refused
    try:
        config_mod.from_dict({"run": {"bash_deny": "rm"}})
        raise AssertionError("non-list accepted")
    except ValueError as e:
        assert "bash_deny" in str(e), e


def test_run_passes_bash_deny_to_worker():
    run_dir, ws = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    (ws / "hydracoder.toml").write_text('[run]\nbash_deny = ["\\\\bmake\\\\b"]\n')
    captured = {}

    def w(task, base, model, workdir, journal, **kw):
        captured[task.id] = kw
    _patch(w)
    o = Orchestrator(run_dir, ws, roster=Roster("m", "m", "m", "m"))
    res = o.run("goal")
    assert res["ok"], res
    assert captured["t1"]["bash_deny"] == [r"\bmake\b"], captured["t1"]


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
