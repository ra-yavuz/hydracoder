#!/usr/bin/env python3
"""Spike 1 harness: drive lillycoder's REAL agent loop (agent.run_turn)
against a running local model, with NO REPL and NO terminal. This tests the
load-bearing question for the whole hydracoder project:

  can a local model + lillycoder's tools + the agent loop complete a real,
  bounded coding task?

It is also a proof-of-concept for the "embeddable output sink" lillycoder
patch: we pass a recording rich.Console instead of the interactive one, and
drive the loop programmatically. Nothing here is a workaround; it exercises
exactly the code hydracoder will embed.

Usage:
  harness.py --api http://127.0.0.1:18100/v1 --model <id> --workdir <dir> \
             --task "..." [--max-iters N] [--show-thoughts]

Prints a JSON report at the end (between BEGIN_REPORT / END_REPORT markers).
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

# Make the lillycoder package importable from its repo without installing.
LILLY_LIB = "/home/yavuz/github-ra-yavuz/lillycoder/lib"
if LILLY_LIB not in sys.path:
    sys.path.insert(0, LILLY_LIB)

import httpx  # noqa: E402
from rich.console import Console  # noqa: E402

from lillycoder import discovery  # noqa: E402
from lillycoder.endpoint import ModelInfo  # noqa: E402
from lillycoder.config import load_persona  # noqa: E402
from lillycoder.context import ContextTracker  # noqa: E402
from lillycoder.tools import registry  # noqa: F401,E402  (force tool registration)
from lillycoder import agent  # noqa: E402


def count_tool_calls(messages: list[dict]) -> dict:
    """Tally tool calls and tool results from the message history."""
    calls = []
    results_ok = 0
    results_err = 0
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                calls.append(tc.get("function", {}).get("name", "?"))
        if m.get("role") == "tool":
            try:
                payload = json.loads(m.get("content", "{}"))
                if isinstance(payload, dict) and payload.get("ok") is False:
                    results_err += 1
                else:
                    results_ok += 1
            except Exception:
                results_ok += 1
    return {
        "tool_calls_made": len(calls),
        "tool_call_names": calls,
        "tool_results_ok": results_ok,
        "tool_results_err": results_err,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--persona", default="default")
    ap.add_argument("--max-iters", type=int, default=12)
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--reasoning-effort", default=None)
    ap.add_argument("--show-thoughts", action="store_true")
    args = ap.parse_args()

    # Let us override the hard iteration cap to see real behavior.
    agent.MAX_TOOL_ITERATIONS = args.max_iters

    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    ep = discovery.manual_endpoint(args.api)
    model_id = args.model or (ep.models[0] if ep.models else "default")
    info = ModelInfo(alias=model_id, endpoint=ep,
                     context_window=ep.context_for(model_id) or 8192)

    system_prompt = load_persona(args.persona)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": args.task},
    ]

    ctx = ContextTracker(model_window=info.context_window or 8192)
    ctx.refresh(messages)
    pct_before = ctx.percent()

    # Live + recording console. Tee everything the agent loop prints to BOTH
    # stdout (so we can watch the run unfold in real time and catch a stuck or
    # looping model immediately) AND a buffer (for the final report). This is
    # the streaming-visibility the spike needs; it mirrors hydracoder's own
    # requirement that every model's output be observable live.
    buf = io.StringIO()

    class _Tee(io.TextIOBase):
        def write(self, s):
            sys.stdout.write(s)
            sys.stdout.flush()
            buf.write(s)
            return len(s)

    rec = Console(file=_Tee(), force_terminal=False, width=100, no_color=True)

    client = httpx.Client(base_url=ep.base_url, timeout=None)
    t0 = time.monotonic()
    err = None
    try:
        agent.run_turn(
            client, info, messages, rec,
            bypass_perms=True,          # spike: workspace is throwaway
            workdir=workdir,
            show_thoughts=args.show_thoughts,
            max_tokens=args.max_tokens,
            reasoning_effort=args.reasoning_effort,
        )
    except Exception as e:  # noqa: BLE001  (spike: we want to see any failure)
        import traceback
        err = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    finally:
        client.close()
    elapsed = time.monotonic() - t0

    ctx.refresh(messages)
    tally = count_tool_calls(messages)

    # Final visible assistant text (the model's closing reply).
    final_text = ""
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("content"):
            final_text = m["content"]
            break

    report = {
        "model": model_id,
        "task": args.task,
        "workdir": str(workdir),
        "elapsed_seconds": round(elapsed, 1),
        "error": err,
        "messages_count": len(messages),
        "context_window": info.context_window,
        "context_pct_before": round(pct_before, 1),
        "context_pct_after": round(ctx.percent(), 1),
        **tally,
        "final_assistant_text": final_text[:1500],
    }

    sys.stdout.write("\n===== AGENT CONSOLE OUTPUT =====\n")
    sys.stdout.write(buf.getvalue())
    sys.stdout.write("\n===== BEGIN_REPORT =====\n")
    sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False))
    sys.stdout.write("\n===== END_REPORT =====\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
