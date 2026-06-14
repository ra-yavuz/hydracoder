"""Orchestrator: drives a goal to completion through plan -> schedule -> work
-> review, writing everything to the journal so a crash can resume.

Routing-first (per the feasibility spikes): each task runs on a right-sized
model started via hydra-llm. Independent tasks form the "ready frontier"; v1
runs them sequentially on a single GPU (real parallelism is a multi-GPU mode,
out of scope). The journal is the source of truth; reconstruct() lets a restart
skip already-done tasks.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

# hydra-llm as the model substrate.
from hydra_llm import api as hydra

from . import config as config_mod
from . import planner as planner_mod
from . import repair_scope as repair_scope_mod
from . import reviewer as reviewer_mod
from . import survey as survey_mod
from . import test_audit as test_audit_mod
from . import verifier as verifier_mod
from . import worker as worker_mod
from .journal import Journal
from .scheduler import choose_roster, Roster


class Orchestrator:
    def __init__(self, run_dir: Path, workspace: Path,
                 roster: Optional[Roster] = None,
                 prefer_models: Optional[dict] = None,
                 log: Optional[Callable[[str], None]] = None,
                 config: Optional[config_mod.Config] = None):
        self.journal = Journal(run_dir)
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        # Workspace config (hydracoder.toml). Raises on an invalid file: a
        # config the operator wrote must never be half-applied. On resume,
        # run() replaces this with the journaled effective config.
        self.config = config if config is not None \
            else config_mod.load_config(self.workspace)
        self._roster = roster
        self._prefer = prefer_models or {}
        self._started: set[str] = set()   # model ids we have started this run
        self._log = log or (lambda m: None)

    # --- model lifecycle (lazy: start a model only when a task needs it) -----

    def _ensure_roster(self) -> Roster:
        if self._roster is None:
            available = hydra.list_models()
            # Precedence: programmatic prefer_models > workspace config.
            prefer = {**self.config.roster, **self._prefer}
            if prefer:
                usable = {m["id"] for m in available
                          if m.get("downloaded") and m.get("fit") in ("yes", "spill")}
                bad = {role: mid for role, mid in prefer.items()
                       if mid not in usable}
                if bad:
                    msg = ("configured model(s) not usable (not downloaded or "
                           "does not fit this machine): " +
                           ", ".join(f"{role}={mid}" for role, mid in sorted(bad.items())))
                    self.journal.append("error", {"where": "roster-config",
                                                  "message": msg, "recovered": False})
                    raise RuntimeError(msg)
            self._roster = choose_roster(available, prefer=prefer)
            self.journal.append("decision", {
                "what": "roster",
                "why": f"small={self._roster.small} worker={self._roster.worker} "
                       f"reviewer={self._roster.reviewer} boss={self._roster.boss}",
            })
        return self._roster

    def _base_url(self, model_id: str) -> str:
        """Start the model if needed and return its OpenAI-compatible base URL."""
        st = hydra.model_status(model_id)
        if st and st.get("ready") and st.get("port"):
            return f"http://127.0.0.1:{st['port']}/v1"
        info = hydra.start(model_id, wait=True, wait_timeout=180)
        if not info.get("ready"):
            raise RuntimeError(f"model {model_id} did not become ready")
        self._started.add(model_id)
        return info["base_url"]

    def shutdown_started(self) -> None:
        """Stop only the models we started (leave pre-existing ones running)."""
        for mid in list(self._started):
            try:
                hydra.stop(mid)
            except Exception:
                # Best-effort cleanup: if stopping one model fails (already
                # gone, docker hiccup), we still try to stop the rest. A failed
                # stop is not worth aborting shutdown over; the idle reaper in
                # hydra-llm will eventually reclaim a stray container anyway.
                pass
        self._started.clear()

    # --- the run --------------------------------------------------------------

    def run(self, goal: str, max_task_tokens: Optional[int] = None,
            echo: bool = False) -> dict:
        """Plan and execute the goal. Idempotent-ish: if the journal already has
        a plan and some done tasks (a resume), it picks up from there.

        On resume the journaled effective config wins over the workspace file:
        the file lives in the model-writable workspace, so re-reading it midway
        would let a worker change its own permissions for the rest of the run
        (and an operator edit mid-run would silently fork run behavior)."""
        state = self.journal.reconstruct()
        # Goal-mismatch guard. A run-dir is bound to the goal it was started
        # with: resume means "finish THIS run", not "start a new one here". But
        # --run-dir defaults to a fixed path (./hydracoder-run) for both `run`
        # and `serve`, so the obvious re-invocation -- new goal, same default
        # run-dir -- used to skip planning entirely and silently no-op on the
        # old (often already-done) plan, reporting ok. That is a silent
        # wrong-work bug, not a feature. Refuse loudly instead. Iterating on an
        # existing build is a separate, not-yet-built capability; the right move
        # today is a fresh --run-dir (the planner then surveys the workspace).
        # Fires whether or not the prior run finished: a divergent goal against
        # a crashed run is the same mistake.
        prior_goal = state.get("goal")
        if state.get("plan") and prior_goal is not None and goal != prior_goal:
            status = "completed" if state.get("finished") else "in progress"
            msg = (f"run-dir is a {status} run for a different goal:\n"
                   f"  journaled: {prior_goal!r}\n"
                   f"  requested: {goal!r}\n"
                   "A run-dir resumes only its own goal. Use a new --run-dir to "
                   "start the new goal (the planner will survey the existing "
                   "workspace), or repeat the original goal to resume this run.")
            self.journal.append("error", {"where": "goal-mismatch",
                                          "message": msg, "recovered": False})
            raise RuntimeError(msg)
        if state.get("plan"):
            jc = state.get("config")
            self.config = (config_mod.from_normalized(jc) if jc
                           else config_mod.legacy_resume_config())
            self.journal.append("decision", {
                "what": "resume-config",
                "why": ("journaled effective config reused" if jc else
                        "journal predates config recording: restricted legacy "
                        "defaults applied")})
        if max_task_tokens is None:
            max_task_tokens = self.config.run.max_task_tokens
        roster = self._ensure_roster()

        # 1. Plan (skip if resuming a run that already planned).
        if not state.get("plan"):
            self.journal.append("run_started",
                                {"goal": goal, "models_dir": str(self.workspace),
                                 "config": self.config.normalized()})
            boss_url = self._base_url(roster.boss)
            # Brownfield awareness: when the workspace already has code, the
            # planner gets a bounded survey of it (file tree, tests, specs),
            # journaled so "what did the planner know" is auditable.
            ws_survey = survey_mod.survey_workspace(self.workspace)
            if ws_survey:
                self._log("existing code detected: surveying workspace for the planner")
                self.journal.append("decision", {"what": "workspace-survey",
                                                 "why": ws_survey[:800]})
            self._log("planning...")
            # Planning is one model call; a transient error (busy model, dropped
            # connection) must not kill the whole run, so retry a few times.
            plan, raw = None, ""
            for attempt in range(1, 4):
                try:
                    plan, raw = planner_mod.make_plan(boss_url, roster.boss, goal,
                                                      max_tokens=2500,
                                                      survey=ws_survey,
                                                      test_first=self.config.run.test_first)
                except Exception as e:
                    self.journal.append("error",
                                        {"where": f"planner attempt {attempt}",
                                         "message": str(e), "recovered": attempt < 3})
                    plan = None
                if plan is not None:
                    break
            if plan is None:
                self.journal.append("error",
                                    {"where": "planner", "message": "no parseable plan after retries",
                                     "recovered": False})
                self.journal.append("run_finished", {"ok": False, "summary": "planning failed"})
                return {"ok": False, "reason": "planning failed", "raw": raw[:500]}
            problems = plan.validate()
            if problems:
                self.journal.append("decision",
                                    {"what": "plan-warnings", "why": "; ".join(problems)})
            self.journal.append("plan_created", plan.to_dict())
        else:
            plan = self._plan_from_state(state)
            self._log("resuming from journal")

        # 2. Execute the ready frontier until all tasks are done or stuck.
        # `done` and `failed` are an in-memory working cache of task outcomes for
        # this run loop. They are NOT a second source of truth: `done` is seeded
        # from the journal (so a resumed run skips already-completed tasks), and
        # every change to either set is mirrored by a TASK_STATE event written to
        # the journal in _run_one. If they ever disagree, the journal wins; these
        # sets exist only so the scheduler does not have to re-read the journal on
        # every frontier check.
        done: set[str] = {tid for tid, t in state["tasks"].items()
                          if t.get("state") == "done"}
        failed: set[str] = set()
        while True:
            ready = [t for t in plan.ready_tasks(done)
                     if t.id not in failed]
            if not ready:
                break
            for task in ready:
                self._run_one(task, roster, done, failed,
                              max_task_tokens=max_task_tokens, echo=echo)

        all_ids = {t.id for t in plan.tasks}
        tasks_ok = done >= all_ids and not failed

        # Final deterministic gate: RUN the project's test suite once, now that
        # all files exist. This is the authoritative proof (real execution, not
        # a model's opinion). If it fails, run bounded repair iterations feeding
        # the actual traceback back to a worker until green or budget exhausted.
        verify = self._final_verification(roster, max_task_tokens, echo)

        ok = tasks_ok and (verify is None or verify["passed"])
        summary = (f"{len(done)}/{len(all_ids)} tasks done"
                   + (f", {len(failed)} failed" if failed else "")
                   + (f"; tests: {verify['summary']}" if verify else "; no tests"))
        self.journal.append("run_finished", {"ok": ok, "summary": summary})
        return {"ok": ok, "done": sorted(done), "failed": sorted(failed),
                "summary": summary, "tests": verify}

    def _final_verification(self, roster: Roster, max_task_tokens: int,
                            echo: bool,
                            max_fix_rounds: Optional[int] = None) -> Optional[dict]:
        """Run the whole project's test suite once. If it fails, hand the real
        traceback to a worker for a targeted fix and re-run, up to
        max_fix_rounds (config: [run] repair_rounds). Returns the final
        verifier result, or None if the project has no tests to run (nothing
        to verify deterministically)."""
        cfg = self.config.run
        if max_fix_rounds is None:
            max_fix_rounds = cfg.repair_rounds
        result = verifier_mod.run_tests(self.workspace, timeout=cfg.test_timeout,
                                        reject_vacuous=cfg.reject_vacuous_tests)
        if not result["ran"]:
            return None  # no test files: nothing to gate on deterministically

        # Repair-scope gate: snapshot before any repair touches the workspace,
        # so each round's edits can be judged against the failing tests'
        # implicated files. A run on an existing codebase is brownfield (the
        # gate is strictest there: do not edit a human suite the failures did
        # not flag).
        scope = None
        if cfg.check_repair_scope:
            scope = repair_scope_mod.RepairScopeTracker(self.workspace)
            if not scope.snapshot():
                scope = None  # git unavailable: degrade to no scope checks
        brownfield = survey_mod.has_existing_code(self.workspace)

        for rnd in range(1, max_fix_rounds + 1):
            self.journal.append("review", {"task_id": "(final)", "attempt": rnd,
                                           "passed": result["passed"],
                                           "notes": "final tests: " + result["summary"],
                                           "kind": "verifier"})
            if result["passed"]:
                self._log(f"final verification PASSED: {result['summary']}")
                return result
            self._log(f"final verification failed (round {rnd}): {result['summary']}")
            # Build a fix task from the real test output and run it on the worker.
            # The repair prompt must allow fixing a defective TEST, not only
            # defective code: an earlier version forbade touching tests, and a
            # run with correct code but buggy generated tests (off-by-one
            # expected positions, an unescaped regex in assertRaisesRegex)
            # burned all repair rounds without converging.
            fix = planner_mod.Task(
                id=f"fix-{rnd}", title="Fix failing tests",
                description=("The project's test suite fails. For EACH failure, "
                             "first DIAGNOSE by reading both the code under test "
                             "and the failing test, then decide which one is "
                             "actually defective. LOCALIZE precisely: grep for "
                             "the function named in the traceback and read only "
                             "that region (read_file with start_line); do not "
                             "read whole large files. Once you have seen the "
                             "defective region, apply the fix IMMEDIATELY with "
                             "edit_file; never re-read content you have already "
                             "seen. Change as FEW lines as possible: never "
                             "restructure, reformat, or 'improve' code that "
                             "the failures do not implicate. If the code is wrong, fix the "
                             "code. If the test itself is wrong (wrong expected "
                             "values, off-by-one positions, an unescaped regex "
                             "special character in assertRaisesRegex), correct "
                             "the test so it asserts the genuinely right "
                             "behavior. NEVER delete tests, skip them, or weaken "
                             "assertions merely to make them pass; a corrected "
                             "test must still meaningfully verify real behavior. "
                             "Test output:\n" + result["output"][-1500:]),
                acceptance="the full test suite passes")
            # Files the CURRENT failures implicate, captured before the worker
            # edits anything, so the scope gate can tell collateral from fix.
            implicated = (repair_scope_mod.implicated_files(result["output"],
                                                            self.workspace)
                          if scope else set())
            try:
                base = self._base_url(roster.worker)
                worker_mod.run_task(
                    fix, base, roster.worker, self.workspace, self.journal,
                    max_tokens=max_task_tokens, echo=echo,
                    tool_subset=self.config.tools_for_complexity(fix.complexity),
                    nudge_on_no_action=cfg.nudge_on_no_action,
                    max_nudges=cfg.max_nudges, bash_deny=cfg.bash_deny)
            except Exception as e:
                self.journal.append("error", {"where": f"final-fix round {rnd}",
                                              "message": str(e), "recovered": rnd < max_fix_rounds})
            if scope:
                self._check_repair_scope(scope, implicated, brownfield, rnd, cfg)
            result = verifier_mod.run_tests(self.workspace, timeout=cfg.test_timeout,
                                            reject_vacuous=cfg.reject_vacuous_tests)
        # Record the final state after exhausting fix rounds.
        note = "final tests" + ("" if result["passed"] else " (exhausted)")
        self.journal.append("review", {"task_id": "(final)", "attempt": max_fix_rounds,
                                       "passed": result["passed"],
                                       "notes": note + ": " + result["summary"],
                                       "kind": "verifier"})
        return result

    def _check_repair_scope(self, scope, implicated: set, brownfield: bool,
                            rnd: int, cfg) -> None:
        """Judge one repair round's edits against the failing tests' implicated
        files. Always journals a `repair_scope` event; when enforcing, reverts
        an out-of-scope round so the next round retries from a clean base. The
        gate is deterministic: it flags collateral the test suite cannot see
        (project-state caveats 9, 11), it does not judge correctness."""
        changed = scope.changed_files()
        verdict = repair_scope_mod.classify_scope(changed, implicated, brownfield)
        verdict["round"] = rnd
        verdict["implicated"] = sorted(implicated)
        self.journal.append("repair_scope", verdict)
        if not verdict["violation"]:
            scope.rebase_to_current()  # accept this round, judge the next alone
            return
        detail = []
        if verdict["out_of_scope"]:
            detail.append("edited unrelated file(s): " +
                          ", ".join(verdict["out_of_scope"]))
        if verdict["test_edits"]:
            detail.append("edited test file(s) the failures did not flag: " +
                          ", ".join(verdict["test_edits"]))
        msg = f"repair round {rnd} out of scope: " + "; ".join(detail)
        if cfg.enforce_repair_scope:
            reverted = scope.revert_to_snapshot()
            self._log(msg + (" -> reverted" if reverted else
                             " -> revert FAILED (left as-is)"))
            self.journal.append("decision",
                                {"what": "repair-scope-enforced",
                                 "why": msg + (" (reverted)" if reverted else
                                               " (revert failed)")})
            # Base stays at the snapshot so the next round starts clean.
        else:
            self._log(msg + " (flagged, not enforced)")
            scope.rebase_to_current()  # advisory: keep the edits, judge next alone

    def _run_one(self, task: "planner_mod.Task", roster: Roster,
                 done: set[str], failed: set[str],
                 max_task_tokens: int, echo: bool) -> None:
        """Run a single task to a terminal outcome. A task is DONE once its
        worker produced output without a tool/transport error. Correctness is
        NOT judged here by a model: measured on weak local models, a model
        reading code and voting pass/fail is unreliable (it rejects correct
        code and misses real bugs), so using it as a per-task GATE stalls the
        build on good work. Instead the model review is ADVISORY ONLY: logged to
        the journal for the human and the UI, never blocking. The authoritative
        correctness check is deterministic and happens once, over the whole
        project, in _final_verification() (it runs the tests and repairs against
        real tracebacks). Mutates `done`/`failed`. `task` is a planner.Task."""
        model_id = roster.for_complexity(task.complexity)
        self.journal.append("task_state", {"task_id": task.id, "state": "running",
                                           "model": model_id})
        try:
            base = self._base_url(model_id)
        except Exception as e:
            self.journal.append("error", {"where": f"start:{model_id}",
                                          "message": str(e), "recovered": False})
            self.journal.append("task_state", {"task_id": task.id, "state": "failed",
                                               "detail": "model start failed"})
            failed.add(task.id)
            return

        self._log(f"task {task.id} ({task.complexity}) -> {model_id}")
        cfg = self.config.run
        try:
            worker_mod.run_task(task, base, model_id, self.workspace, self.journal,
                                max_tokens=max_task_tokens, echo=echo,
                                tool_subset=self.config.tools_for_complexity(task.complexity),
                                nudge_on_no_action=cfg.nudge_on_no_action,
                                max_nudges=cfg.max_nudges, bash_deny=cfg.bash_deny)
        except Exception as e:
            self.journal.append("error", {"where": f"worker:{task.id}",
                                          "message": str(e), "recovered": False})
            self.journal.append("task_state", {"task_id": task.id, "state": "failed",
                                               "detail": "worker raised"})
            failed.add(task.id)
            return

        # Advisory review: log a model's opinion (best-effort, never blocks).
        # Config can disable it ([run] reviewer_enabled = false) to save the
        # reviewer model's start time and tokens; the deterministic final
        # verification is unaffected.
        if cfg.reviewer_enabled:
            self.journal.append("task_state", {"task_id": task.id, "state": "review"})
            try:
                rev_url = self._base_url(roster.reviewer)
                verdict = reviewer_mod.review_task(task, rev_url, roster.reviewer, self.workspace)
                self.journal.append("review", {"task_id": task.id,
                                               "passed": verdict["passed"],
                                               "notes": verdict["notes"],
                                               "kind": "model", "advisory": True})
            except Exception as e:
                self.journal.append("error", {"where": f"review:{task.id}",
                                              "message": str(e), "recovered": True})

        # Test-first: a test task's file is audited NOW (before any code that
        # depends on it runs). It must be non-vacuous and fail-first for a
        # missing-implementation reason; otherwise a bounded test-repair loop
        # corrects it. A test that cannot be made to fail-first is a failed
        # task (better to stop than to gate later code on a hollow test).
        if cfg.test_first and task.kind == "test":
            if not self._audit_test_task(task, roster, model_id,
                                         max_task_tokens, echo, cfg):
                self.journal.append("task_state", {"task_id": task.id,
                                                   "state": "failed",
                                                   "detail": "test audit failed"})
                failed.add(task.id)
                return

        # The task produced its files; correctness is decided by the final gate.
        self.journal.append("task_state", {"task_id": task.id, "state": "done"})
        done.add(task.id)

    def _audit_test_task(self, task, roster: Roster, model_id: str,
                         max_task_tokens: int, echo: bool, cfg) -> bool:
        """Audit a test task's file: it must be non-vacuous and fail-first for
        a missing-implementation reason. On failure, run a bounded test-repair
        loop. Returns True when the test passes the audit. Deterministic; the
        model never judges its own test's correctness here."""
        test_module = self._test_module_for(task)
        if not test_module:
            self.journal.append("test_audit", {"task_id": task.id, "ok": False,
                                               "reason": "no test_*.py produced by this test task"})
            return False
        for attempt in range(1, cfg.test_audit_rounds + 2):  # 1 audit + N repairs
            audit = test_audit_mod.audit_test_file(
                self.workspace, test_module, targets=task.targets,
                timeout=cfg.test_timeout)
            self.journal.append("test_audit", {"task_id": task.id,
                                               "attempt": attempt,
                                               "ok": audit["ok"],
                                               "reason": audit["reason"],
                                               "collected": audit["collected"],
                                               "fail_kind": audit["fail_kind"]})
            if audit["ok"]:
                self._log(f"test audit PASSED for {test_module}: {audit['reason']}")
                return True
            if attempt > cfg.test_audit_rounds:
                break
            self._log(f"test audit failed for {test_module} "
                      f"(round {attempt}): {audit['reason']}")
            fix = planner_mod.Task(
                id=f"{task.id}-audit-{attempt}",
                title="Fix the test file so it is a real fail-first test",
                description=(
                    f"The test file {test_module}.py does not yet pass the "
                    f"test-first audit. Problem: {audit['reason']}.\n"
                    "Requirements: it must define real unittest.TestCase test_* "
                    "methods with meaningful assertions covering the acceptance "
                    "criteria; it must import the module(s) it tests "
                    f"({', '.join(task.targets) or 'the planned interface'}); "
                    "and since the implementation does NOT exist yet, the tests "
                    "are EXPECTED to fail when run (ImportError/AttributeError/"
                    "AssertionError). Do NOT implement the module; only fix the "
                    "TEST file. Do not write a test that passes against absent "
                    "code (that asserts nothing). Audit output:\n"
                    + audit["output"][-1000:]),
                interface=task.interface, acceptance=task.acceptance,
                complexity=task.complexity, kind="test", targets=task.targets)
            try:
                base = self._base_url(model_id)
                worker_mod.run_task(
                    fix, base, model_id, self.workspace, self.journal,
                    max_tokens=max_task_tokens, echo=echo,
                    tool_subset=self.config.tools_for_complexity(task.complexity),
                    nudge_on_no_action=cfg.nudge_on_no_action,
                    max_nudges=cfg.max_nudges, bash_deny=cfg.bash_deny)
            except Exception as e:
                self.journal.append("error", {"where": f"test-audit-fix {task.id}",
                                              "message": str(e),
                                              "recovered": attempt <= cfg.test_audit_rounds})
        self._log(f"test audit exhausted for {test_module}")
        return False

    def _test_module_for(self, task) -> Optional[str]:
        """The test module name this test task is responsible for. Prefer a
        test_<target>.py matching a declared target; else the newest
        test_*.py in the workspace (the one the task just wrote)."""
        for t in task.targets:
            cand = self.workspace / f"test_{Path(t).stem}.py"
            if cand.is_file():
                return cand.stem
        tests = sorted(self.workspace.glob("test_*.py"),
                       key=lambda p: p.stat().st_mtime)
        return tests[-1].stem if tests else None

    def _plan_from_state(self, state: dict) -> "planner_mod.Plan":
        specs = [t["spec"] for t in state["tasks"].values() if t.get("spec")]
        tasks = [planner_mod.Task(
            id=s["id"], title=s.get("title", s["id"]),
            description=s.get("description", ""),
            depends_on=[str(x) for x in (s.get("depends_on") or [])],
            interface=s.get("interface", ""), acceptance=s.get("acceptance", ""),
            complexity=s.get("complexity", "small"),
            kind=s.get("kind", "code"),
            targets=[str(x) for x in (s.get("targets") or [])]) for s in specs]
        return planner_mod.Plan(
            architecture=(state.get("plan") or {}).get("architecture", ""),
            tasks=tasks)
