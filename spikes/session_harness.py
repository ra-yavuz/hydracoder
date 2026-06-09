#!/usr/bin/env python3
"""Spike 5: long-session context survival. Drives lillycoder's REAL multi-turn
machinery (agent.run_turn + ContextTracker + ContextTracker.compact, the same
code the REPL uses) across many turns with a deliberately SMALL window so
compaction is forced. Verifies: (1) no crash at/over the window, (2) compaction
actually fires and shrinks the estimate, (3) the model still does correct work
AFTER compaction using a fact established BEFORE it.

Usage: session_harness.py --api URL --model ID --workdir DIR --window N
"""
from __future__ import annotations
import argparse, io, json, sys, time
from pathlib import Path

LILLY_LIB = "/home/yavuz/github-ra-yavuz/lillycoder/lib"
if LILLY_LIB not in sys.path:
    sys.path.insert(0, LILLY_LIB)

import httpx
from rich.console import Console

from lillycoder import discovery, agent
from lillycoder.endpoint import ModelInfo
from lillycoder.config import load_persona
from lillycoder.context import ContextTracker
from lillycoder.tools import registry  # noqa: F401


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--window", type=int, default=3000,
                    help="small artificial context window to force compaction")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve(); workdir.mkdir(parents=True, exist_ok=True)
    ep = discovery.manual_endpoint(args.api)
    info = ModelInfo(alias=args.model, endpoint=ep, context_window=args.window)
    client = httpx.Client(base_url=ep.base_url, timeout=None)

    system_prompt = load_persona("default")
    messages = [{"role": "system", "content": system_prompt}]
    ctx = ContextTracker(model_window=args.window)

    # A sequence of turns. The SECRET is established early; later we compact;
    # then we ask the model to use the secret, testing post-compaction recall.
    turns = [
        "Remember this project fact: the magic project code is ZEPHYR-7731. Just acknowledge it, do not write any file yet.",
        "Create a file notes.txt containing the single line: hello world. Use write_file now.",
        "Append a second line 'second line' to notes.txt (read it, then write it back with both lines). Call tools now.",
        "List the files in the current directory with list_dir. Call the tool now.",
        "Read notes.txt back and tell me how many lines it has. Call read_file now.",
        # filler turns to grow context toward the window
        "Explain in 3 short sentences what a CLI is.",
        "Explain in 3 short sentences what a unit test is.",
        # POST-COMPACTION RECALL TEST: use the secret from turn 1
        "Create a file code.txt whose contents are exactly the magic project code I gave you at the very start. Use write_file now. Do not ask me to repeat it.",
    ]

    log = {"window": args.window, "turns": [], "compactions": 0,
           "crashed": False, "error": None}

    rec = Console(file=io.StringIO(), force_terminal=False, no_color=True)

    try:
        for i, user_text in enumerate(turns):
            messages.append({"role": "user", "content": user_text})
            ctx.refresh(messages)
            pct_before = ctx.percent()

            # The REPL's auto-compact rule: if >=90% full, compact BEFORE the turn.
            compacted = False
            if pct_before >= 90.0:
                try:
                    ctx.compact(messages, system_prompt, client, info, keep_last_pairs=3)
                    log["compactions"] += 1
                    compacted = True
                except Exception as e:
                    log["turns"].append({"i": i, "compact_error": str(e)})

            t0 = time.monotonic()
            agent.run_turn(client, info, messages, rec,
                           bypass_perms=True, workdir=workdir,
                           show_thoughts=False, max_tokens=600)
            dt = round(time.monotonic() - t0, 1)
            ctx.refresh(messages)

            final = ""
            for m in reversed(messages):
                if m.get("role") == "assistant" and m.get("content"):
                    final = m["content"]; break
            log["turns"].append({
                "i": i, "pct_before": round(pct_before, 1),
                "pct_after": round(ctx.percent(), 1),
                "compacted_before_turn": compacted,
                "msgs": len(messages), "secs": dt,
                "final": (final or "")[:140],
            })
    except Exception as e:
        import traceback
        log["crashed"] = True
        log["error"] = "".join(traceback.format_exception(type(e), e, e.__traceback__))[-1500:]
    finally:
        client.close()

    print("===== SESSION REPORT =====")
    print(json.dumps(log, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
