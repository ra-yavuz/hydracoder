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


# Restricted profile: FILE tools only, NO bash. This was the hardcoded default
# through v0.1.3 (a bash-equipped worker once spent its whole budget building a
# venv and pip-installing). It remains selectable per task type via the
# workspace config ([tools] section in hydracoder.toml).
FILE_TOOLS = ["read_file", "write_file", "edit_file", "list_dir",
              "grep", "find", "mkdir"]

# Backward-compatible alias; external docs reference WORKER_TOOLS.
WORKER_TOOLS = FILE_TOOLS

# Default profile: every developer tool, including the shell. This is the
# operator's decision (default-allow): lillycoder's always-on hard-deny
# safety layer (sudo, rm -rf on home/root, mkfs, device writes, fork bombs,
# out-of-workspace file writes) still applies even with bypass_perms=True.
# Deliberately NOT included even here: pkg_install (the documented runaway
# failure mode, and its apt path is hard-refused anyway) and the persona
# tools (they mutate the agent's own configuration outside the workspace;
# an autonomous worker must not self-modify). Either can still be granted
# per task type by naming it explicitly in the [tools] config.
DEV_TOOLS = FILE_TOOLS + ["bash", "rm", "mv"]


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


def _task_prompt(task, review_feedback: Optional[str] = None,
                 tools: Optional[list[str]] = None) -> str:
    """Imperative, tool-first prompt. The 'call the tools now' framing is the
    proven Spike-4 mitigation for the narrate-instead-of-act failure. If
    review_feedback is given (a retry after a failed review), it is surfaced so
    the worker fixes the specific defect rather than starting blind. `tools`
    is the task's actual allowlist, so the prompt never advertises a tool the
    model cannot call (it used to hardcode bash even when bash was excluded)."""
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
    test_first = ""
    if getattr(task, "kind", "code") == "test":
        tgt = ", ".join(task.targets) if task.targets else "the planned module"
        test_first = (
            "\nThis is a TEST-FIRST task: write ONLY the test file for "
            f"{tgt}. The implementation does NOT exist yet, so your tests are "
            "EXPECTED to fail when run (ImportError or AssertionError). That "
            "is correct. Import the module under test, call its planned "
            "interface, and assert the genuinely correct results. Do NOT "
            "implement the module, and do NOT write tests that pass against "
            "absent code (a passing test here asserts nothing).\n")
    tool_names = "/".join(tools) if tools else "read_file/write_file/edit_file"
    return (
        f"TASK: {task.title}\n\n{task.description}\n\n"
        f"Interface this task must expose: {task.interface}\n"
        f"Acceptance: {task.acceptance}\n{deps}{fix}{test_first}\n"
        f"Do this now by CALLING TOOLS ({tool_names}). "
        "Do not just describe what you would do. When the task is fully done "
        "and the acceptance condition is met, say: DONE."
    )


def run_task(task, base_url: str, model: str, workdir: Path, journal,
             max_tokens: int = 2500, echo: bool = False,
             tool_subset: Optional[list[str]] = None,
             review_feedback: Optional[str] = None,
             nudge_on_no_action: bool = True, max_nudges: int = 2,
             bash_deny: Optional[list[str]] = None) -> JournalSink:
    """Run one task to completion through lillycoder. Returns the sink (with the
    recorded events). Raises only on transport-level failure; tool errors are
    captured as events and left for the reviewer to judge. `review_feedback`,
    when set, makes this a corrective retry of a review-rejected task.

    `tool_subset=None` means the DEV_TOOLS default profile (this changed in
    0.2: it used to mean the restricted no-bash set). The orchestrator always
    passes an explicit list resolved from the workspace config; the default
    here only matters for direct callers."""
    subset = tool_subset if tool_subset is not None else list(DEV_TOOLS)
    ep = manual_endpoint(base_url)
    info = ModelInfo(alias=model, endpoint=ep,
                     context_window=ep.context_for(model) or 16384)
    messages = [
        {"role": "system",
         "content": ("You are a precise coding worker. You complete one bounded "
                     "task by calling tools. You keep changes minimal and you "
                     "honor existing interfaces in the workspace. In large "
                     "files, LOCALIZE before you read: use grep to find the "
                     "relevant function, then read_file with start_line to "
                     "view just that region; never try to read a large file "
                     "whole. Rules: never "
                     "install packages, create virtualenvs, or fetch anything "
                     "from the network, not even indirectly through make "
                     "targets, build scripts, or package managers (run tests "
                     "directly with python3 -m unittest, not via make); "
                     "everything you need is already in the "
                     "workspace. Any test file you write must contain real "
                     "unittest.TestCase test_* methods with meaningful "
                     "assertions on the module under test; an empty or "
                     "assertion-free test file is a FAILED task. Derive "
                     "expected values carefully (count string positions "
                     "character by character). In assertRaisesRegex the "
                     "pattern is a REGEX: escape special characters, or "
                     "assert on str(exception) instead.")},
        {"role": "user", "content": _task_prompt(task, review_feedback,
                                                 tools=subset)},
    ]
    sink = JournalSink(journal, task.id, echo=echo)
    client = httpx.Client(base_url=ep.base_url, timeout=None)
    try:
        agent.run_turn(
            client, info, messages, sink,
            bypass_perms=True, workdir=Path(workdir),
            show_thoughts=False, max_tokens=max_tokens,
            tool_subset=subset,
            nudge_on_no_action=nudge_on_no_action, max_nudges=max_nudges,
            bash_deny=bash_deny,
        )
    finally:
        client.close()
    return sink
