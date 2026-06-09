"""Append-only journal: the source of truth for a hydracoder run.

Every state change is written here BEFORE it is acted on, so a crash, OOM, or
restart can replay the log and resume exactly. The web UI is a pure function of
this log (it streams/replays events). Nothing else in hydracoder holds
authoritative state; in-memory objects are derived from the journal.

Format: one JSON object per line (JSONL) at <run_dir>/journal.jsonl. Writes are
flushed and fsync'd so a power loss cannot lose an acknowledged event.

Event kinds and their data payloads are defined by the EVENT_KINDS table below
(not just prose) so the contract is explicit and callers can reference the
constants instead of bare strings. Keep this table and the constants in sync
when adding an event.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


# --- the event contract, as named constants (use these, not bare strings) ----
# Each value documents the expected `data` dict for that kind.
RUN_STARTED = "run_started"      # {goal, models_dir}
PLAN_CREATED = "plan_created"    # {architecture, tasks:[task-spec dicts]}
TASK_STATE = "task_state"        # {task_id, state, detail?}  state in TASK_STATES
WORKER_EVENT = "worker_event"    # {task_id, event}  event = a lillycoder sink event
REVIEW = "review"                # {task_id, passed, notes, attempt?}
COMPACTION = "compaction"        # {task_id, before_pct, after_pct}
DECISION = "decision"            # {what, why}
ERROR = "error"                  # {where, message, recovered}
CONTROL = "control"              # {action, args}  boss-model control-tool call
RUN_FINISHED = "run_finished"    # {ok, summary}

EVENT_KINDS = {RUN_STARTED, PLAN_CREATED, TASK_STATE, WORKER_EVENT, REVIEW,
               COMPACTION, DECISION, ERROR, CONTROL, RUN_FINISHED}

# The states a task moves through (the `state` field of a TASK_STATE event).
TASK_STATES = ("queued", "running", "review", "done", "failed", "blocked")


@dataclass
class Event:
    seq: int
    ts: float
    kind: str
    data: dict

    def to_line(self) -> str:
        return json.dumps({"seq": self.seq, "ts": self.ts,
                           "kind": self.kind, "data": self.data},
                          ensure_ascii=False)

    @staticmethod
    def from_line(line: str) -> "Event":
        d = json.loads(line)
        return Event(seq=d["seq"], ts=d["ts"], kind=d["kind"], data=d["data"])


class Journal:
    """Append-only event log with durable writes and a subscriber fan-out so
    the web UI can stream events live without polling the file."""

    def __init__(self, run_dir: Path, clock: Optional[Callable[[], float]] = None):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "journal.jsonl"
        self._seq = 0
        self._subscribers: list[Callable[[Event], None]] = []
        # clock is injectable so tests are deterministic (the workflow/runtime
        # forbids Date.now-style nondeterminism; here we just allow override).
        self._clock = clock or time.time
        # If resuming, advance _seq past the existing tail.
        if self.path.exists():
            for ev in self.replay():
                self._seq = max(self._seq, ev.seq)

    def append(self, kind: str, data: dict) -> Event:
        self._seq += 1
        ev = Event(seq=self._seq, ts=self._clock(), kind=kind, data=data)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(ev.to_line() + "\n")
            f.flush()
            os.fsync(f.fileno())
        for sub in list(self._subscribers):
            try:
                sub(ev)
            except Exception:
                # Intentionally swallowed: a subscriber is a live UI feed, not
                # a critic of the write. Journalling (the durable record) has
                # already succeeded above; one flaky subscriber must never break
                # it or stop the other subscribers. Subscriber-side failures are
                # surfaced in the UI's own connection state, not here.
                pass
        return ev

    def replay(self) -> Iterable[Event]:
        """Yield every event in order. Tolerates a truncated last line (a crash
        mid-write): the partial line is skipped, earlier events are intact.

        Note: this is a generator, so a bare `return` here means "yield nothing"
        (an empty sequence), NOT "return a value". When the journal file does not
        exist yet, callers iterating this simply see no events."""
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield Event.from_line(line)
                except json.JSONDecodeError:
                    # Truncated tail from a crash mid-append: stop here.
                    break

    def subscribe(self, fn: Callable[[Event], None]) -> Callable[[], None]:
        """Register a live subscriber. Returns an unsubscribe callable."""
        self._subscribers.append(fn)
        return lambda: self._subscribers.remove(fn) if fn in self._subscribers else None

    # --- derived state (a pure function of the log) ---------------------------

    def reconstruct(self) -> dict:
        """Fold the event log into current run state: goal, plan, per-task state.
        This is how a restart resumes: read the journal, rebuild this dict, and
        continue from whatever was not yet done."""
        state: dict[str, Any] = {
            "goal": None, "models_dir": None, "plan": None,
            "tasks": {}, "finished": None,
        }
        for ev in self.replay():
            d = ev.data
            if ev.kind == RUN_STARTED:
                state["goal"] = d.get("goal")
                state["models_dir"] = d.get("models_dir")
            elif ev.kind == PLAN_CREATED:
                state["plan"] = d
                for t in d.get("tasks", []):
                    state["tasks"].setdefault(t["id"], {})
                    state["tasks"][t["id"]].update({"spec": t, "state": "queued"})
            elif ev.kind == TASK_STATE:
                tid = d["task_id"]
                state["tasks"].setdefault(tid, {})
                state["tasks"][tid]["state"] = d["state"]
                if d.get("detail"):
                    state["tasks"][tid]["detail"] = d["detail"]
            elif ev.kind == REVIEW:
                tid = d["task_id"]
                state["tasks"].setdefault(tid, {})
                state["tasks"][tid]["review"] = {"passed": d.get("passed"), "notes": d.get("notes")}
            elif ev.kind == RUN_FINISHED:
                state["finished"] = d
        return state
