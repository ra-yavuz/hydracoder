"""Worker driver: runs ONE task through lillycoder's agent loop in an isolated
context, forwarding every tool call / token to the journal via a sink.

This is the payoff of the lillycoder M0a sink work: hydracoder embeds the real
agent loop (not a reimplementation) and observes it through structured events,
exactly as designed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import httpx

# lillycoder is imported as a library (added to sys.path by the runner).
from lillycoder import agent
from lillycoder.endpoint import ModelInfo
from lillycoder.discovery import manual_endpoint
from lillycoder.sink import RecordingSink
from lillycoder.tools import registry  # noqa: F401  (register tools)


# Tool subset workers get by default: FILE tools only, deliberately NO bash.
# A code-writing worker writes files; it does not need a shell, and giving it
# one invites runaway behavior (a worker once spent the whole time budget
# building a venv and pip-installing). Verification and test-running are the
# orchestrator/harness's job, not the worker's. A task that genuinely needs a
# shell can be opted in explicitly via the tool_subset argument. This matches
# the autonomy decision: act freely on files in the workspace, network installs
# and environment setup are out of scope for an autonomous worker.
WORKER_TOOLS = ["read_file", "write_file", "edit_file", "list_dir",
                "grep", "find", "mkdir"]


class JournalSink(RecordingSink):
    """A RecordingSink that also forwards each structured event to the journal
    under a given task id, so the run is observable live and replayable."""

    def __init__(self, journal, task_id: str, echo: bool = False):
        super().__init__(echo=echo)
        self._journal = journal
        self._task_id = task_id

    def _forward(self, event: dict) -> None:
        self._journal.append("worker_event", {"task_id": self._task_id, "event": event})

    def on_token(self, text, is_thought=False):
        super().on_token(text, is_thought)
        # Only forward visible tokens to keep the journal lean.
        if not is_thought:
            self._forward({"kind": "token", "text": text})

    def on_tool_call(self, name, args):
        super().on_tool_call(name, args)
        self._forward({"kind": "tool_call", "name": name, "args": dict(args)})

    def on_tool_result(self, name, ok, result):
        super().on_tool_result(name, ok, result)
        # Truncate big results in the journal; the file system holds the truth.
        r = result
        if isinstance(r, dict) and isinstance(r.get("diff"), str) and len(r["diff"]) > 500:
            r = {**r, "diff": r["diff"][:500] + "...(truncated)"}
        self._forward({"kind": "tool_result", "name": name, "ok": ok, "result": r})

    def on_turn_end(self, *, stopped_reason=None):
        super().on_turn_end(stopped_reason=stopped_reason)
        self._forward({"kind": "turn_end", "stopped_reason": stopped_reason})


def _task_prompt(task, review_feedback: Optional[str] = None) -> str:
    """Imperative, tool-first prompt. The 'call the tools now' framing is the
    proven Spike-4 mitigation for the narrate-instead-of-act failure. If
    review_feedback is given (a retry after a failed review), it is surfaced so
    the worker fixes the specific defect rather than starting blind."""
    deps = ""
    if task.depends_on:
        deps = ("\nThis task depends on: " + ", ".join(task.depends_on) +
                ". Their code already exists in the workspace; read the files "
                "you need with read_file to learn their real interfaces before "
                "using them. Do NOT redefine those functions; IMPORT and call "
                "them.\n")
    fix = ""
    if review_feedback:
        fix = ("\nA previous attempt was REJECTED by review with this reason:\n"
               f"  {review_feedback}\n"
               "Read the current files, fix exactly this problem, and write the "
               "corrected version.\n")
    return (
        f"TASK: {task.title}\n\n{task.description}\n\n"
        f"Interface this task must expose: {task.interface}\n"
        f"Acceptance: {task.acceptance}\n{deps}{fix}\n"
        "Do this now by CALLING TOOLS (read_file/write_file/edit_file/bash). "
        "Do not just describe what you would do. When the task is fully done "
        "and the acceptance condition is met, say: DONE."
    )


def run_task(task, base_url: str, model: str, workdir: Path, journal,
             max_tokens: int = 2500, echo: bool = False,
             tool_subset: Optional[list[str]] = None,
             review_feedback: Optional[str] = None) -> JournalSink:
    """Run one task to completion through lillycoder. Returns the sink (with the
    recorded events). Raises only on transport-level failure; tool errors are
    captured as events and left for the reviewer to judge. `review_feedback`,
    when set, makes this a corrective retry of a review-rejected task."""
    ep = manual_endpoint(base_url)
    info = ModelInfo(alias=model, endpoint=ep,
                     context_window=ep.context_for(model) or 16384)
    messages = [
        {"role": "system",
         "content": ("You are a precise coding worker. You complete one bounded "
                     "task by calling tools. You keep changes minimal and you "
                     "honor existing interfaces in the workspace.")},
        {"role": "user", "content": _task_prompt(task, review_feedback)},
    ]
    sink = JournalSink(journal, task.id, echo=echo)
    client = httpx.Client(base_url=ep.base_url, timeout=None)
    try:
        agent.run_turn(
            client, info, messages, sink,
            bypass_perms=True, workdir=Path(workdir),
            show_thoughts=False, max_tokens=max_tokens,
            tool_subset=tool_subset or WORKER_TOOLS,
            nudge_on_no_action=True, max_nudges=2,
        )
    finally:
        client.close()
    return sink
