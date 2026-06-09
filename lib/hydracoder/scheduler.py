"""Scheduler: routing-first model selection.

The feasibility spikes showed that on a single GPU, running many models in
parallel does not beat one; the lever is RIGHT-SIZING the model to the task. So
the scheduler routes each task to a model by difficulty: a small fast model for
trivial/small leaf tasks, a larger model for medium/large or review work. It
respects hydra-llm's hardware fit so it never picks a model that will not run.

Concurrency mode is a policy knob (routing | swarm | multi_gpu); v1 defaults to
routing. Multi-GPU true parallelism is out of scope for v1 (it is wasted on a
single GPU per the spikes) but the seam is here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Difficulty -> preferred role. The orchestrator maps roles to concrete model
# ids from what is available + fits.
COMPLEXITY_TO_ROLE = {
    "trivial": "small",
    "small": "small",
    "medium": "worker",
    "large": "worker",
}


@dataclass
class Roster:
    """Concrete model ids chosen for each role on this machine."""
    small: str      # fast, fits fully in VRAM: leaf tasks
    worker: str     # capable workhorse: medium/large tasks
    reviewer: str   # smarter: reviews acceptance
    boss: str       # resident: chat + planning + control

    def for_complexity(self, complexity: str) -> str:
        role = COMPLEXITY_TO_ROLE.get(complexity, "worker")
        return getattr(self, role)


def choose_roster(available: list[dict],
                  prefer: Optional[dict] = None) -> Roster:
    """Pick a roster from hydra-llm's list_models() output (dicts with id, fit,
    downloaded, size_gb). `prefer` can pin any role by id.

    Heuristic, deliberately simple (per the doctrine: no premature config):
      - small  = smallest downloaded model that fits cleanly ("yes")
      - worker = a mid/large downloaded model that fits (prefers a4b/MoE names)
      - reviewer = largest downloaded fitting model (best judgment)
      - boss = the small model (resident, cheap, always responsive)
    """
    prefer = prefer or {}
    usable = [m for m in available if m.get("downloaded") and m.get("fit") in ("yes", "spill")]
    if not usable:
        raise RuntimeError("no downloaded model fits this machine; download one first")

    by_size = sorted(usable, key=lambda m: (m.get("size_gb") or 0))
    smallest = by_size[0]["id"]
    largest = by_size[-1]["id"]

    # worker: prefer a MoE / a4b model (fast for its size) in the middle,
    # else the largest that still reads "yes" (clean fit), else largest.
    yes_fit = [m for m in by_size if m.get("fit") == "yes"]
    moe = next((m for m in by_size if "a4b" in (m.get("id") or "")), None)
    worker = (moe or (yes_fit[-1] if yes_fit else by_size[-1]))["id"]

    roster = Roster(
        small=prefer.get("small", smallest),
        worker=prefer.get("worker", worker),
        reviewer=prefer.get("reviewer", largest),
        boss=prefer.get("boss", smallest),
    )
    return roster
