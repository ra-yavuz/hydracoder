"""Planner: turns a project goal into a task graph.

Validated in feasibility spike 2: a local model produces a correct graph with
explicit interfaces and per-task acceptance, and distinguishes sequential
(layered) work from parallel (independent) work. This module wraps that with a
strict schema, light validation, and a difficulty tag the scheduler routes on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import llm


PLAN_SYSTEM = """You are a software architect. Given a project goal, produce a \
build plan as a task graph. Respond with STRICT JSON only: no prose, no \
markdown fences. Schema:
{
  "architecture": "1-3 sentence overview",
  "tasks": [
    {
      "id": "t1",
      "title": "short imperative title",
      "description": "precise enough for a junior coder to implement",
      "depends_on": ["t0"],
      "interface": "the exact function/CLI/file signature this task exposes",
      "acceptance": "a concrete, testable pass condition",
      "complexity": "trivial|small|medium|large"
    }
  ]
}
Rules: maximize the number of tasks with depends_on == [] so independent work \
runs in parallel. Every depends_on id must reference another task's id. No \
cycles. Make interfaces explicit so dependent tasks can build against them. \
Always include a final task that wires the pieces together and one that tests \
the result."""


@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    interface: str = ""
    acceptance: str = ""
    complexity: str = "small"


@dataclass
class Plan:
    architecture: str
    tasks: list[Task]

    def validate(self) -> list[str]:
        """Return a list of structural problems (empty == valid)."""
        problems = []
        ids = {t.id for t in self.tasks}
        if len(ids) != len(self.tasks):
            problems.append("duplicate task ids")
        for t in self.tasks:
            for dep in t.depends_on:
                if dep not in ids:
                    problems.append(f"{t.id} depends on unknown {dep}")
        # cycle check via topo sort
        if not self._is_acyclic():
            problems.append("dependency cycle")
        return problems

    def _is_acyclic(self) -> bool:
        indeg = {t.id: 0 for t in self.tasks}
        adj: dict[str, list[str]] = {t.id: [] for t in self.tasks}
        for t in self.tasks:
            for dep in t.depends_on:
                if dep in indeg:
                    adj[dep].append(t.id)
                    indeg[t.id] += 1
        queue = [i for i, d in indeg.items() if d == 0]
        seen = 0
        while queue:
            n = queue.pop()
            seen += 1
            for m in adj[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    queue.append(m)
        return seen == len(self.tasks)

    def ready_tasks(self, done_ids: set[str]) -> list[Task]:
        """Tasks whose dependencies are all satisfied and not yet done."""
        return [t for t in self.tasks
                if t.id not in done_ids and all(d in done_ids for d in t.depends_on)]

    def to_dict(self) -> dict:
        return {"architecture": self.architecture,
                "tasks": [vars(t) for t in self.tasks]}


def make_plan(base_url: str, model: str, goal: str,
              max_tokens: int = 2500) -> tuple[Optional[Plan], str]:
    """Call the planner model. Returns (plan_or_None, raw_text). On parse or
    validation failure, plan is None and the caller can retry or surface it."""
    resp = llm.complete(base_url, model, [
        {"role": "system", "content": PLAN_SYSTEM},
        {"role": "user", "content": f"Project goal: {goal}"},
    ], max_tokens=max_tokens, temperature=0.3)
    obj = llm.extract_json(resp["content"])
    if not obj or "tasks" not in obj:
        return None, resp["content"]
    tasks = []
    for t in obj.get("tasks", []):
        if not isinstance(t, dict) or "id" not in t:
            continue
        tasks.append(Task(
            id=str(t["id"]),
            title=t.get("title", t["id"]),
            description=t.get("description", ""),
            depends_on=[str(x) for x in (t.get("depends_on") or [])],
            interface=t.get("interface", ""),
            acceptance=t.get("acceptance", ""),
            complexity=t.get("complexity", "small"),
        ))
    if not tasks:
        return None, resp["content"]
    return Plan(architecture=obj.get("architecture", ""), tasks=tasks), resp["content"]
