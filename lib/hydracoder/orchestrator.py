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

from . import planner as planner_mod
from . import reviewer as reviewer_mod
from . import verifier as verifier_mod
from . import worker as worker_mod
from .journal import Journal
from .scheduler import choose_roster, Roster


class Orchestrator:
    def __init__(self, run_dir: Path, workspace: Path,
                 roster: Optional[Roster] = None,
                 prefer_models: Optional[dict] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.journal = Journal(run_dir)
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._roster = roster
        self._prefer = prefer_models or {}
        self._started: set[str] = set()   # model ids we have started this run
        self._log = log or (lambda m: None)

    # --- model lifecycle (lazy: start a model only when a task needs it) -----

    def _ensure_roster(self) -> Roster:
        if self._roster is None:
            self._roster = choose_roster(hydra.list_models(), prefer=self._prefer)
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

    def run(self, goal: str, max_task_tokens: int = 2500,
            echo: bool = False) -> dict:
        """Plan and execute the goal. Idempotent-ish: if the journal already has
        a plan and some done tasks (a resume), it picks up from there."""
        state = self.journal.reconstruct()
        roster = self._ensure_roster()

        # 1. Plan (skip if resuming a run that already planned).
        if not state.get("plan"):
            self.journal.append("run_started",
                                {"goal": goal, "models_dir": str(self.workspace)})
            boss_url = self._base_url(roster.boss)
            self._log("planning...")
            # Planning is one model call; a transient error (busy model, dropped
            # connection) must not kill the whole run, so retry a few times.
            plan, raw = None, ""
            for attempt in range(1, 4):
                try:
                    plan, raw = planner_mod.make_plan(boss_url, roster.boss, goal,
                                                      max_tokens=2500)
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
                            echo: bool, max_fix_rounds: int = 3) -> Optional[dict]:
        """Run the whole project's test suite once. If it fails, hand the real
        traceback to a worker for a targeted fix and re-run, up to
        max_fix_rounds. Returns the final verifier result, or None if the
        project has no tests to run (nothing to verify deterministically)."""
        result = verifier_mod.run_tests(self.workspace)
        if not result["ran"]:
            return None  # no test files: nothing to gate on deterministically
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
            fix = planner_mod.Task(
                id=f"fix-{rnd}", title="Fix failing tests",
                description=("The project's test suite fails. Read the relevant "
                             "files and fix the code so ALL tests pass. Do not "
                             "weaken or delete the tests to make them pass; fix "
                             "the real defect. Test output:\n" + result["output"][-1500:]),
                acceptance="the full test suite passes")
            try:
                base = self._base_url(roster.worker)
                worker_mod.run_task(fix, base, roster.worker, self.workspace,
                                    self.journal, max_tokens=max_task_tokens, echo=echo)
            except Exception as e:
                self.journal.append("error", {"where": f"final-fix round {rnd}",
                                              "message": str(e), "recovered": rnd < max_fix_rounds})
            result = verifier_mod.run_tests(self.workspace)
        # Record the final state after exhausting fix rounds.
        self.journal.append("review", {"task_id": "(final)", "attempt": max_fix_rounds,
                                       "passed": result["passed"],
                                       "notes": "final tests (exhausted): " + result["summary"],
                                       "kind": "verifier"})
        return result

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
        self.journal.append("task_state", {"task_id": task.id, "state": "running"})
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
        try:
            worker_mod.run_task(task, base, model_id, self.workspace, self.journal,
                                max_tokens=max_task_tokens, echo=echo)
        except Exception as e:
            self.journal.append("error", {"where": f"worker:{task.id}",
                                          "message": str(e), "recovered": False})
            self.journal.append("task_state", {"task_id": task.id, "state": "failed",
                                               "detail": "worker raised"})
            failed.add(task.id)
            return

        # Advisory review: log a model's opinion (best-effort, never blocks).
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

        # The task produced its files; correctness is decided by the final gate.
        self.journal.append("task_state", {"task_id": task.id, "state": "done"})
        done.add(task.id)

    def _plan_from_state(self, state: dict) -> "planner_mod.Plan":
        specs = [t["spec"] for t in state["tasks"].values() if t.get("spec")]
        tasks = [planner_mod.Task(
            id=s["id"], title=s.get("title", s["id"]),
            description=s.get("description", ""),
            depends_on=[str(x) for x in (s.get("depends_on") or [])],
            interface=s.get("interface", ""), acceptance=s.get("acceptance", ""),
            complexity=s.get("complexity", "small")) for s in specs]
        return planner_mod.Plan(
            architecture=(state.get("plan") or {}).get("architecture", ""),
            tasks=tasks)
