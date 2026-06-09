# hydracoder: implementation blueprint

**Status:** design, pre-implementation. Grounded in feasibility spikes run 2026-06-09 (see "Evidence" at the end).

**What this document is:** the build plan for hydracoder and the supporting patches to lillycoder and hydra-llm. It is meant to be reviewed and corrected before code is written. Read "The one-paragraph version" and "Decisions already made", then the milestones.

---

## The one-paragraph version

hydracoder is a local-AI development capability: a web UI where you enter a project goal and watch local models build it. A planner model turns the goal into a task graph; a scheduler routes each task to the right-sized local model (a small fast model for simple leaf tasks, a big model for planning and review); a reviewer model checks each task against an acceptance test; and a resilient context layer (per-task fresh contexts + an append-only journal + RAG-as-memory) keeps the system from crashing when contexts fill or a worker dies. It is built on two existing tools: **hydra-llm** runs and manages the models in Docker, and **lillycoder** is the agent loop that does the file/shell work. Its reason to exist: to keep your projects moving when hosted AI becomes too expensive to use.

---

## Why this design (the evidence that shaped it)

The spikes measured local-model behavior on the target hardware (AMD Strix Point, 83GB RAM, iGPU + 42GB GTT) and produced three conclusions that the architecture is built around:

1. **Right-size the model to the task; don't run many at once.** On a single GPU, running multiple models in parallel does not beat running one, because they time-slice the same compute. A tiny 3.5GB model did 5 leaf tasks in 28.7s; the big model took 72s; two models in parallel took 70.8s. All produced identical correct results. **Lever = routing by difficulty, not parallelism.** True parallelism is a multi-GPU feature, kept as an optional mode.

2. **Every model needs its own profile.** Thinking-mode control is per-model and partly broken upstream. Correct per-model flags took throughput from 4.8 to 33 tok/s (7x) with no hardware change. hydracoder must hold a per-model profile (reasoning control, sampling, tool quirks), provided by hydra-llm.

3. **Local models need scaffolding to act, not just to think.** A model will sometimes narrate ("I'll read the file first") and emit no tool call. The fix is imperative tool-use prompting + an auto-retry when a turn ends with talk-but-no-action and the task isn't done.

---

## Decisions already made (do not relitigate without reason)

- **North-star:** a fallback dev capability for when corporate AI prices out. Quality bar = "good enough to keep shipping with supervision", not "matches a frontier hosted model".
- **Composition:** new repo `hydracoder` imports hydra-llm (model substrate) and lillycoder (worker agent loop) as libraries. Both stay standalone-first.
- **Default behavior:** routing-first (small model for leaf tasks, big for plan/review). Real multi-model parallelism is an opt-in "multi-GPU mode", not the default.
- **Concurrency modes** are runtime-switchable by talking to the boss model (control tools), with UI buttons as the equivalent surface.
- **Decomposition:** conservative, auto-escalate to a parallel frontier only when tasks are genuinely independent.
- **Autonomy:** workers act freely inside the project workspace dir; anything outside it, plus network installs and destructive commands, is gated (reuses lillycoder's existing safety + permission layer).
- **Portability:** auto-selects models by the user's hardware tier; one-command install; zero-config first run; ships as a .deb with a Pages page and apt entry; liability disclaimer on every surface.
- **RAG** (hydra-llm's existing LanceDB) is the memory layer: long-term project memory + code-aware retrieval into worker contexts. Not rebuilt.
- **UI vision:** each running model shown as a small live terminal streaming its own tool calls/output; in-UI model download via hydra-llm.
- **Releases** of lillycoder and hydra-llm are batched to the end, done properly per the CLAUDE.md publishing pipeline.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  WEB UI  (dark, modern; FastAPI static + WebSocket)                 │
│   • project-goal input + models-folder config                       │
│   • per-model live terminals (stream each worker's tool calls/output)│
│   • task-graph view (lanes: queued / running / review / done)        │
│   • persistent chat box  ── routed to the BOSS model                 │
└───────────────┬─────────────────────────────────────────────────────┘
                │ WebSocket (journal events: task state, diffs, tokens, logs)
┌───────────────▼─────────────────────────────────────────────────────┐
│  ORCHESTRATOR  (hydracoder daemon, Python + FastAPI + asyncio)       │
│   • Boss/router model (small, resident): chat box, delegation,       │
│     control tools (set mode, set model-for-role, pause, download)    │
│   • Planner: goal → architecture → task graph (deps + acceptance)    │
│   • Scheduler: routes each task to the right-sized model by          │
│     difficulty + hardware fit; runs the independent frontier;        │
│     swaps the heavy slot; (multi-GPU mode: true concurrency)         │
│   • Worker driver: calls lillycoder's agent loop per task, in an     │
│     isolated fresh context, with a task-scoped tool subset           │
│   • Reviewer: a smarter model checks each task vs its acceptance test│
│   • Journal: append-only event log (plan, task state, tool calls,    │
│     diffs, decisions), the source of truth; enables resume/recovery │
│   • Context service: per-task fresh contexts; compaction by the boss;│
│     RAG retrieval for code/memory; OOM/overflow recovery             │
└───────┬───────────────────────────────────────────┬─────────────────┘
        │ imports                                     │ imports
┌───────▼──────────────────┐                ┌─────────▼────────────────┐
│  hydra-llm (substrate)    │                │  lillycoder (worker)     │
│  • start/stop/download     │                │  • agent loop (run_turn) │
│  • per-model profiles      │                │  • tools: read/write/    │
│  • hardware fit (GTT-aware) │                │    edit/bash/grep/...    │
│  • idle reaper (free VRAM)  │                │  • safety + permissions  │
│  • RAG / LanceDB memory     │                │  • embeddable output sink│
└────────────────────────────┘                └──────────────────────────┘
```

### Component responsibilities and interfaces

**hydra-llm (patched, standalone-first).** Gives hydracoder a programmatic API (today it is CLI-only). Needed surface:
- `list_models()` / `model_status()` → what's downloaded, running, on what port, fit/spill.
- `start(alias)` / `stop(alias)` → returns the OpenAI-compatible base URL + port.
- `download(alias)` with progress events (for in-UI download).
- per-model **profile** resolution (reasoning control, sampling, `--jinja`, ctx-size) via the existing `extra_args` / `reasoning_format` / `chat_template_kwargs` mechanism, curated per catalog entry.
- hardware fit that counts GTT + RAM on unified-memory tiers (fixes the pessimistic "spill" label).
- RAG: `index(path)`, `query(text)` (already exists).

**lillycoder (patched, standalone-first).** The worker engine. Needed surface:
- `run_turn(...)` already exists; make its output go through a generic **sink** (not hardcoded `rich.Console`) so the orchestrator can stream it over WebSocket.
- a **headless one-shot mode** (`--prompt`) so it is drivable programmatically and scriptable.
- **task-scoped tool subset**: let a caller restrict which tools are active for a task (research says ≤5-7 for small models).
- **grammar-constrained tool output** (GBNF) option for models that misfire on free-form tool JSON.
- **lossless context**: persist tool-call traces in sessions; strip reasoning traces from persisted history (keep only conclusions).
- **path-resolution fix** (DONE this session): tools resolve against the agent workdir, not process CWD.
- **auto-retry/nudge** when a turn ends with content-but-no-tool-call and the task isn't done.

**hydracoder (new).** Everything else: UI, orchestrator daemon, planner, scheduler, reviewer, journal, context service, boss chat + control tools.

### The journal (resilience core)

An append-only JSONL event log per project. Every state change is written **before** it is acted on. Event kinds: `plan_created`, `task_state_changed`, `tool_call`, `file_diff`, `decision`, `compaction`, `error`. Consequences:
- **Crash/OOM recovery:** on restart, replay the journal to reconstruct exact state and resume.
- **The UI is a pure function of the journal** (each live terminal replays/streams its task's events).
- **Context never silently lost:** if a worker overflows, the journal has everything; the boss compacts and the task resumes from a fresh context seeded by the journal + RAG.

---

## Build sequence (milestones, bottom-up)

Each milestone is independently verifiable. Foundation (lillycoder + hydra-llm) first, because hydracoder depends on it and the spikes proved it is needed.

**M0: Foundation patches (lillycoder + hydra-llm), the proven-needed fixes**
- lillycoder: path-fix (done), embeddable output sink, headless `--prompt` mode, task-scoped tool subset, persist tool traces, strip reasoning from history, auto-retry-on-no-action.
- hydra-llm: per-model profile curation (the spike's reasoning flags), GTT-aware fit, a thin Python API module the orchestrator imports.
- Verify: drive a single coding task end-to-end via the headless lillycoder against a hydra-llm-started model, with output captured through the sink. (This is essentially the spike harness, promoted to a real feature.)

**M1: Walking skeleton (hydracoder)**
- FastAPI app: goal input + chat box + WebSocket; journal writer/reader.
- Orchestrator: take a goal → one planner call → run ONE worker task via lillycoder → one reviewer check → stream events to the UI.
- Verify: enter a one-task goal in the browser, watch the worker terminal stream, see the file written and the review pass, kill the process mid-run and confirm journal replay resumes.

**M2: Planner + scheduler (routing-first)**
- Planner produces the full task graph (deps + acceptance + interfaces), as validated in the spike.
- Scheduler: route each task to the right-sized model by difficulty + hardware fit; run the independent frontier; swap the heavy slot via hydra-llm; reviewer per task.
- Verify: run the mathutils goal (5 independent tasks) and the notes-CLI goal (sequential); confirm correct graphs, correct routing, acceptance tests pass.

**M3: Resilient context layer**
- Per-task fresh contexts; boss-model compaction; RAG memory (index the workspace, retrieve into worker contexts); OOM/overflow detection → downscale + retry from journal.
- Verify: force a context overflow and an OOM; confirm recovery without losing work.

**M4: Boss chat + control tools**
- Chat box routed to the resident boss model; control tools (set mode, set model-for-role, set models-folder, pause/reprioritize, download model) applied live; UI buttons mirror them.
- Verify: mid-run, type "use the tiny model for everything" and "pause task t3"; confirm the scheduler obeys.

**M5: Multi-GPU mode (optional)**
- True concurrent workers when separate compute exists; otherwise no-op to routing-first.
- Verify: only meaningful on multi-GPU hardware; document that single-GPU gains nothing (per spike).

**M6: Packaging + portability + publish**
- Zero-config first run (detect hardware → pick/download default models). One-command .deb. Pages page, apt entry, hub card, profile README, website cards (per CLAUDE.md). Liability disclaimer on every surface.
- Batched release of lillycoder + hydra-llm patches via the publishing pipeline.
- Verify: `release-doctor.sh`, fresh-machine install test.

---

## Risks and open questions (named honestly)

Two assumptions flagged unproven in the first draft have now been tested and PROVEN (Spikes 4 and 5 below); the remaining risks are engineering-quality, not feasibility.

- **Workers narrate instead of acting (PROVEN risk, mitigation PROVEN).** On harder tasks a worker will sometimes end a turn with talk and zero tool calls (~30% of the time in the cross-task spike), silently doing nothing. Mitigation, verified to work: imperative tool-use prompting + auto-retry when a turn ends with no tool call and the task is not done. This is a HARD M0 requirement, not polish.
- **Decomposition quality is the whole ballgame.** Bad task graphs (false independence) stall the frontier. Mitigation: conservative-by-default, explicit interfaces per task, reviewer gate. (Spike 2 showed the planner distinguishes parallel from sequential correctly, but adversarial cases are untested.)
- **Compaction quality** depends on the model. Spike 5 proved the session survives 6 compactions without crash and preserves a load-bearing fact, but a subtle fact buried in noise might not survive a summary. Mitigation: journal + RAG are the lossless backstops; compaction is the convenience layer, not the source of truth.
- **Per-model profiles are maintenance.** New models need a profile. Mitigation: a sane default profile (reasoning routed-out + budget-capped) plus per-model overrides only where needed.
- **lillycoder scope creep** (the "local coding-agent" Tier-B features: hooks, /goal, settings) is a separate parallel track and must not block hydracoder.

---

## Evidence (spikes, 2026-06-09)

All runs used hydra-llm-started models with `--jinja`, driven through lillycoder's real `agent.run_turn`, with hidden acceptance oracles.

- **Spike 1 (one model, real task):** gemma-4-26b-a4b-it wrote fizzbuzz (8/8 hidden cases) in 19.9s and extended a CLI (lines+words, no regression) in 26.9s, 0 tool errors. Found and fixed a lillycoder path bug. Per-model reasoning config took throughput 4.8 → 33 tok/s.
- **Spike 2 (decomposition):** correct task graphs; distinguished sequential (notes-CLI) from parallel (mathutils, 5-wide independent frontier); explicit interfaces + acceptance per task.
- **Spike 3 (many small vs one big):** all approaches 28/28 correct. Big solo 72.0s; sequential swarm 72.8s; 2-models-parallel 70.8s; **tiny model alone 28.7s (fastest)**. Conclusion: right-size, don't parallelize, on a single GPU.
- **Spike 4 (coupled multi-file):** one-turn 3-file banking lib (BankAccount used by Bank used by a CLI) passed 11/11 cross-file integration checks. The harder cross-task variant (3 separate contexts, each worker reading its dependency to discover the interface) failed 4/11 on first attempt (a worker narrated and made zero tool calls), then passed 11/11 once given imperative "call the tools now" prompting. Proves coupled work is feasible and that auto-retry-on-no-action is required.
- **Spike 5 (long-session survival):** drove lillycoder's real ContextTracker.compact + run_turn across 8 turns with a 900-token window. No crash despite repeatedly hitting 100% context; compaction fired 6 times; a fact set in turn 0 (a project code) was still correctly recalled and written to a file in turn 7, after 6 compactions. Proves the session does not crash on overflow and stays coherent across compactions.

Hardware: AMD Strix Point (Radeon 880M/890M iGPU, 8GB VRAM carve-out + 42GB GTT), 83GB RAM, llama.cpp build b9064 via hydra-llm Docker images.
