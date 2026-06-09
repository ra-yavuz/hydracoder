"""Minimal OpenAI-compatible client for the planner / reviewer / boss roles.

Workers do not use this: they run through lillycoder's agent loop. This is for
the non-tool, single-shot model calls (planning, reviewing, chat) where we just
want a completion. Uses urllib (stdlib) to avoid adding a dependency; the
worker path already pulls in httpx via lillycoder.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Optional


def complete(base_url: str, model: str, messages: list[dict],
             max_tokens: int = 2000, temperature: float = 0.3,
             timeout: float = 600.0) -> dict:
    """One non-streaming chat completion. Returns {content, reasoning, finish}.
    Raises on transport error or non-200."""
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    choice = d["choices"][0]
    msg = choice.get("message", {})
    return {
        "content": msg.get("content") or "",
        "reasoning": msg.get("reasoning_content") or "",
        "finish": choice.get("finish_reason"),
        "usage": d.get("usage", {}),
    }


def extract_json(text: str) -> Optional[dict]:
    """Pull a JSON object out of a model reply, tolerating markdown fences and
    leading/trailing prose. Returns None if no parseable object is found."""
    t = text.strip()
    if t.startswith("```"):
        # drop the first fence line and any closing fence
        t = t.split("```", 2)[-1] if t.count("```") >= 2 else t[3:]
        t = t.lstrip("json").lstrip("\n")
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    # find the outermost object
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None
