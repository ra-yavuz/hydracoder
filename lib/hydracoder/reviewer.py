"""Reviewer: a model checks whether a finished task meets its acceptance.

Reviewing is a judgment task where a more capable model helps, so the
orchestrator routes this to the reviewer role. The reviewer is shown the task's
acceptance condition and the files that exist, and must return a strict verdict.
It is one line of defense; the e2e acceptance tests are the other (and the
authoritative one). A reviewer pass does not override a failing test.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import llm


REVIEW_SYSTEM = """You are a strict code reviewer. You are given a task's \
acceptance condition and the relevant files that were produced. Decide whether \
the acceptance condition is genuinely met. Respond with STRICT JSON only:
{"passed": true|false, "notes": "one or two sentences: what is right or what is missing"}
Be skeptical. If a required function, file, or behavior is missing or wrong, \
passed must be false."""


def _gather_files(workdir: Path, names: Optional[list[str]] = None,
                  max_bytes: int = 8000) -> str:
    """Read the workspace files (or a named subset) into a single review blob,
    capped so the reviewer context stays small."""
    wd = Path(workdir)
    files = []
    if names:
        cand = [wd / n for n in names]
    else:
        cand = sorted([p for p in wd.rglob("*")
                       if p.is_file() and p.suffix in (".py", ".js", ".html", ".css", ".txt", ".md", ".json")])
    blob = []
    budget = max_bytes
    for p in cand:
        if not p.exists() or budget <= 0:
            continue
        try:
            text = p.read_text(encoding="utf-8")[:budget]
        except Exception:
            continue
        rel = p.relative_to(wd)
        blob.append(f"=== {rel} ===\n{text}")
        budget -= len(text)
        files.append(str(rel))
    return "\n\n".join(blob) if blob else "(no readable files were produced)"


def review_task(task, base_url: str, model: str, workdir: Path,
                max_tokens: int = 600) -> dict:
    """Return {"passed": bool, "notes": str}. On a model/parse failure, returns
    passed=False with the reason so a broken review never silently passes."""
    blob = _gather_files(Path(workdir))
    user = (f"Task: {task.title}\nAcceptance condition: {task.acceptance}\n\n"
            f"Files produced:\n{blob}")
    try:
        resp = llm.complete(base_url, model, [
            {"role": "system", "content": REVIEW_SYSTEM},
            {"role": "user", "content": user},
        ], max_tokens=max_tokens, temperature=0.1)
    except Exception as e:
        return {"passed": False, "notes": f"reviewer call failed: {e}"}
    obj = llm.extract_json(resp["content"])
    if not obj or "passed" not in obj:
        return {"passed": False, "notes": "reviewer returned no parseable verdict"}
    return {"passed": bool(obj["passed"]), "notes": str(obj.get("notes", ""))[:300]}
