#!/usr/bin/env python3
"""Spike 2: can a local model decompose a multi-part goal into an INDEPENDENT
task graph with explicit interfaces and per-task acceptance criteria?

We send a planning prompt asking for strict JSON, parse it, and mechanically
check: valid JSON? tasks have ids/deps/acceptance? are there genuinely
independent tasks (empty deps) that could run in parallel? do deps reference
real task ids (no cycles, no dangling)?

Usage: plan_probe.py --api URL --model ID --goal "..." [--reasoning on|off]
"""
from __future__ import annotations
import argparse, json, sys, time
import httpx

PLAN_SYSTEM = """You are a software architect. Given a project goal, produce a \
build plan as a task graph. Respond with STRICT JSON only, no prose, no \
markdown fences. Schema:
{
  "architecture": "1-3 sentence overview",
  "tasks": [
    {
      "id": "t1",
      "title": "short imperative title",
      "description": "what to build, precise enough for a junior coder",
      "depends_on": ["t0", ...],        // task ids that must finish first; [] if independent
      "interface": "the exact function/CLI/file signature this task exposes to others",
      "acceptance": "a concrete, testable pass condition",
      "complexity": "trivial|small|medium|large"
    }
  ]
}
Rules: maximize the number of tasks with depends_on == [] so they can run in \
parallel. Every depends_on id must reference another task's id. No cycles. \
Make interfaces explicit so dependent tasks can be built against them."""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--goal", required=True)
    ap.add_argument("--max-tokens", type=int, default=2000)
    args = ap.parse_args()

    payload = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": PLAN_SYSTEM},
            {"role": "user", "content": f"Project goal: {args.goal}"},
        ],
        "max_tokens": args.max_tokens,
        "temperature": 0.3,
        "stream": False,
    }
    t0 = time.monotonic()
    r = httpx.post(args.api.rstrip("/") + "/chat/completions", json=payload, timeout=None)
    elapsed = time.monotonic() - t0
    d = r.json()
    msg = d["choices"][0]["message"]
    raw = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""

    # Strip accidental markdown fences.
    txt = raw.strip()
    if txt.startswith("```"):
        txt = txt.split("```", 2)[1] if "```" in txt[3:] else txt
        txt = txt.lstrip("json").strip()
        if txt.endswith("```"):
            txt = txt[:-3].strip()

    report = {"elapsed_s": round(elapsed, 1), "reasoning_tokens_approx": len(reasoning)//4,
              "raw_len": len(raw), "finish": d["choices"][0].get("finish_reason")}
    try:
        plan = json.loads(txt)
    except Exception as e:
        report["json_valid"] = False
        report["parse_error"] = str(e)
        report["raw_head"] = raw[:600]
        print(json.dumps(report, indent=2)); return 1

    report["json_valid"] = True
    tasks = plan.get("tasks", [])
    ids = [t.get("id") for t in tasks]
    idset = set(ids)
    independent = [t["id"] for t in tasks if not t.get("depends_on")]
    dangling = [(t["id"], dep) for t in tasks for dep in (t.get("depends_on") or []) if dep not in idset]
    have_iface = sum(1 for t in tasks if t.get("interface"))
    have_accept = sum(1 for t in tasks if t.get("acceptance"))

    report.update({
        "architecture": plan.get("architecture", "")[:300],
        "task_count": len(tasks),
        "task_ids": ids,
        "independent_tasks": independent,
        "independent_count": len(independent),
        "dangling_deps": dangling,
        "tasks_with_interface": have_iface,
        "tasks_with_acceptance": have_accept,
        "tasks": [{k: t.get(k) for k in ("id","title","depends_on","interface","acceptance","complexity")} for t in tasks],
    })
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
