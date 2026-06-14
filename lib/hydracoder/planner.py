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
the result. Test tasks must demand real unittest.TestCase test_* methods with \
meaningful assertions for EVERY module the plan produces; a test file with no \
test methods fails verification. \
If a WORKSPACE SURVEY is provided, the project ALREADY EXISTS: plan tasks \
that MODIFY and EXTEND the existing files, referencing real paths from the \
survey. Do not plan recreating files that already exist. Tasks must read the \
listed spec/doc files relevant to what they change and respect the existing \
interfaces and conventions."""


TEST_FIRST_PLAN_SYSTEM = """You are a software architect using TEST-FIRST \
development. Given a project goal, produce a build plan as a task graph where \
the TESTS for each module are written and proven to fail BEFORE the module is \
implemented. Respond with STRICT JSON only: no prose, no markdown fences. \
Schema:
{
  "architecture": "1-3 sentence overview",
  "tasks": [
    {
      "id": "t1",
      "title": "short imperative title",
      "description": "precise enough for a junior coder to implement",
      "depends_on": ["t0"],
      "interface": "the exact function/class/CLI signature this exposes",
      "acceptance": "a concrete, testable pass condition",
      "complexity": "trivial|small|medium|large",
      "kind": "test|code",
      "targets": ["store.py"]
    }
  ]
}
Rules for TEST-FIRST planning:
- For EVERY module the project needs, emit TWO tasks: a kind="test" task that \
writes test_<module>.py against the module's planned interface, and a \
kind="code" task that implements the module. The code task MUST depend on its \
test task (depends_on includes the test task id). The test task's "targets" \
field lists the MODULE FILE(S) UNDER TEST that the test imports, NOT the test \
file itself. Example: a test task that writes test_store.py has \
"targets": ["store.py"] (because the test does `from store import ...`). \
Never put a test_*.py name in "targets".
- A kind="test" task writes ONLY the test file. It must define real \
unittest.TestCase test_* methods with meaningful assertions covering every \
acceptance condition, importing the planned interface. It must NOT implement \
the module; the module does not exist yet, so the tests are expected to fail \
when first run (that is the point: the audit proves they fail for the right \
reason).
- Give every module an explicit interface so the test task can write against \
it precisely. No cycles. Every depends_on id must reference another task. \
Include a final kind="code" task that wires the pieces together."""


@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    interface: str = ""
    acceptance: str = ""
    complexity: str = "small"
    # "code" (default) or "test". In test-first mode a "test" task authors a
    # test_*.py against a planned interface BEFORE the implementation exists,
    # and is audited (must fail-first for the right reason) before any code
    # task that depends on it runs. Greenfield default ("code") is unchanged.
    kind: str = "code"
    # In test-first mode, the module path a "test" task targets (e.g.
    # "store.py"), so the audit can check the test imports the right module.
    targets: list[str] = field(default_factory=list)


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
              max_tokens: int = 2500,
              survey: str = "",
              test_first: bool = False) -> tuple[Optional[Plan], str]:
    """Call the planner model. Returns (plan_or_None, raw_text). On parse or
    validation failure, plan is None and the caller can retry or surface it.
    `survey` (from survey.survey_workspace) makes planning brownfield-aware.
    `test_first` asks for test tasks (kind="test") ordered BEFORE the
    implementation tasks that depend on them, each naming the module it
    targets, so the audit can prove the tests fail-first before any code."""
    system = TEST_FIRST_PLAN_SYSTEM if test_first else PLAN_SYSTEM
    user = f"Project goal: {goal}"
    if survey:
        user += ("\n\nWORKSPACE SURVEY (the project already exists; plan to "
                 "modify and extend it):\n" + survey)
    resp = llm.complete(base_url, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], max_tokens=max_tokens, temperature=0.3)
    obj = llm.extract_json(resp["content"])
    if not obj or "tasks" not in obj:
        return None, resp["content"]
    tasks = []
    for t in obj.get("tasks", []):
        if not isinstance(t, dict) or "id" not in t:
            continue
        kind = t.get("kind", "code")
        kind = kind if kind in ("code", "test") else "code"
        tasks.append(Task(
            id=str(t["id"]),
            title=t.get("title", t["id"]),
            description=t.get("description", ""),
            depends_on=[str(x) for x in (t.get("depends_on") or [])],
            interface=t.get("interface", ""),
            acceptance=t.get("acceptance", ""),
            complexity=t.get("complexity", "small"),
            kind=kind,
            targets=[str(x) for x in (t.get("targets") or [])],
        ))
    if not tasks:
        return None, resp["content"]
    return Plan(architecture=obj.get("architecture", ""), tasks=tasks), resp["content"]
