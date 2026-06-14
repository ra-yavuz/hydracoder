# hydracoder

**A local-AI development orchestrator.** Enter a project goal in a dark web UI and watch local models plan it, build it, and review it, with no hosted AI in the loop. hydracoder exists so you can keep shipping on your own hardware when hosted AI becomes too expensive to use.

It is the coordination layer on top of two companion tools:

- **[hydra-llm](https://github.com/ra-yavuz/hydra-llm)** runs and manages local models in Docker (start, stop, download, hardware-fit detection, per-model profiles).
- **[lillycoder](https://github.com/ra-yavuz/lillycoder)** is the per-task agent loop that reads, writes, and edits files and runs tools.

hydracoder adds the planner, the scheduler, the reviewer, the resilient journal, and the web UI that ties them together.

---

## Table of contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [How a run works](#how-a-run-works)
- [Architecture](#architecture)
- [The journal: why a run never loses work](#the-journal)
- [Design decisions (measured, not assumed)](#design-decisions)
- [Configuration](#configuration)
- [Working on an existing codebase](#working-on-an-existing-codebase)
- [Trusting the result: test-first and the scope gate](#trusting-the-result-the-gate-is-only-as-strong-as-the-suite)
- [Limits and failure behavior](#limits-and-failure-behavior)
- [The chat box: steering a run in plain language](#the-chat-box)
- [Extending hydracoder](#extending-hydracoder)
- [Troubleshooting](#troubleshooting)
- [Install](#install)
- [Disclaimer](#disclaimer)

---

## What it does

You give hydracoder a project goal and a folder of local models. It then:

1. **Plans.** A planner model decomposes the goal into a task graph: a list of tasks, each with explicit dependencies, an interface it must expose, and a concrete acceptance condition. If the workspace already contains code, the planner first receives a deterministic survey of it (see [Working on an existing codebase](#working-on-an-existing-codebase)).
2. **Routes.** A scheduler sends each task to a model sized for its difficulty: a small fast model for simple leaf tasks, a larger model for harder work and for review.
3. **Builds.** Each task runs through lillycoder's agent loop in its own isolated context. Every tool call and token streams to a live terminal in the UI.
4. **Reviews (advisory).** A reviewer model gives a second opinion on each finished task. It never blocks the build: measured on weak local models, a gating reviewer rejected correct work and missed real bugs, so its verdicts are journalled for you and the UI, nothing more.
5. **Verifies and repairs.** Once all tasks are done, hydracoder RUNS the project's real test suite. This is the only gate. If it fails, the actual traceback is handed to a worker for a bounded number of repair rounds; a test file that collects zero tests fails the gate too.
6. **Recovers.** Everything is written to an append-only journal before it happens. If the process is killed or the machine reboots, you restart and the run resumes exactly where it stopped, with completed tasks skipped.

---

## Quick start

```
# 1. Have at least one local model available via hydra-llm:
hydra-llm list                 # see what is downloaded / fits your hardware
hydra-llm download gemma-4-e2b-it   # a small fast model, if you have none

# 2. Start hydracoder:
hydracoder serve               # opens the UI at http://127.0.0.1:8765/

# 3. In the browser, type a goal and press Build. Watch the model terminals.
```

No browser? Run a goal headless and get the result on stdout:

```
hydracoder run "build a JSON-backed todo CLI with tests" --workspace ./todo
```

---

## How a run works

```
goal ─▶ planner ─▶ task graph ─▶ scheduler ─┬▶ worker (lillycoder) ─▶ advisory review
                                            └▶ (next ready task, deps satisfied)

all tasks done ─▶ RUN the real test suite ─▶ green ─▶ done
                          ▲                    │
                          └── repair worker ◀──┘ failed: real traceback,
                              (bounded rounds)         fix code OR a defective test
```

The scheduler runs the **ready frontier**: every task whose dependencies are already done. On a single GPU these run one after another (see [Design decisions](#design-decisions)); the UI still shows them as separate live terminals so you can follow each one.

Each worker gets a **fresh, small context**: only its own task spec plus the files it reads. This is deliberate. It keeps individual contexts from filling, which is the main reason long local-model sessions normally fall apart.

---

## Architecture

Small, single-purpose Python modules under `lib/hydracoder/`. Each file has a module docstring explaining its job; nothing is larger than about 250 lines.

| Module | Responsibility |
|---|---|
| `journal.py` | Append-only event log. The source of truth. Replay + reconstruct + live subscribe. |
| `planner.py` | Goal to task graph. `Plan`/`Task` types, cycle/dangling validation, ready-frontier. |
| `scheduler.py` | Picks a model per task by difficulty (`Roster` + `choose_roster`). |
| `survey.py` | Deterministic, bounded snapshot of an existing workspace for the planner. |
| `worker.py` | Runs one task through lillycoder's agent loop; forwards events to the journal. |
| `reviewer.py` | A model judges a finished task against its acceptance condition (advisory). |
| `verifier.py` | The gate: runs the real test suite (root and `tests/` package layouts), counts collected tests, rejects vacuous test files. |
| `test_audit.py` | Test-first: proves a test file fails-first for a missing-implementation reason before any code exists. |
| `repair_scope.py` | Flags (or reverts) a repair round that edits code the failing tests never implicated. |
| `orchestrator.py` | Ties it together: plan, route, work, review, verify, repair, resume. |
| `llm.py` | Tiny OpenAI-compatible client for planner/reviewer/boss calls (stdlib only). |
| `boss.py` | The chat box's brain and the conversational control surface (control tools). |
| `config.py` | Per-workspace `hydracoder.toml`: roster pins, run knobs, tool allowlists. |
| `server.py` | Web server: stdlib HTTP for the UI, websockets for the live event stream. |
| `web/` | The built UI (static files, committed; no CDNs, fonts bundled). Source lives in `webapp/`. |
| `__main__.py` | CLI entry: `serve` and `run` subcommands. |

The web UI is a **pure view over the journal**: it subscribes to the event stream and renders it. It holds no authoritative state of its own, which is why a browser that connects late still sees the full history (the server replays the journal on connect).

### Web UI development

The UI source is a Vite + vanilla TypeScript app in `webapp/` (no framework, no runtime dependencies). `lib/hydracoder/web/` is its committed build output, so the installed package and the .deb stay self-contained static files and node is needed only when changing the UI:

```
cd webapp
npm install
npm run build      # type-checks, then emits into ../lib/hydracoder/web/
npm run dev        # live-reload dev server (point it at a running hydracoder)
```

Each task runs in its own terminal window on the stage: live tokens, tool calls, and review lines stream in, the final deterministic verification gets its own marked window, and clicking a window header maximizes it. The roster button opens a drawer that pins any downloaded model to any role (small / worker / reviewer / boss) mid-run, using the same journalled control path as the boss chat.

---

## The journal

The journal (`<run-dir>/journal.jsonl`) is the heart of hydracoder's resilience. Every state change is written, flushed, and fsync'd **before** it is acted on. The event kinds are defined as named constants in `journal.py` (`RUN_STARTED`, `PLAN_CREATED`, `TASK_STATE`, `WORKER_EVENT`, `REVIEW`, `DECISION`, `ERROR`, `CONTROL`, `REPAIR_SCOPE`, `TEST_AUDIT`, `RUN_FINISHED`).

Two properties fall out of this:

- **Resume.** `Journal.reconstruct()` folds the log back into current state (goal, plan, per-task status). A restarted orchestrator skips tasks already marked done and continues from the frontier. A run killed mid-task picks up cleanly.
- **Crash tolerance.** `replay()` skips a truncated final line (a crash mid-write), so the rest of the log is always readable.

The in-memory `done`/`failed` sets the scheduler uses are a working cache derived from the journal, never a competing source of truth. If they ever disagree, the journal wins.

---

## Design decisions

These were established by running feasibility experiments on real local models **before** the code was written, so they are measured, not assumed.

**Right-size the model to the task; do not parallelize on a single GPU.** Running several models at once does not beat running one on a single GPU, because they share the same compute and just time-slice. In testing, a small 3.5 GB model finished a set of leaf tasks faster than a large model and faster than two models running "in parallel". So the scheduler's lever is matching model size to task difficulty, not concurrency. True multi-GPU parallelism is a separate, optional mode, not the default.

**Every model needs its own profile.** Reasoning/thinking control in llama.cpp is per-model and partly broken upstream for some families. Without the right flags, a model spends its whole token budget on hidden chain-of-thought and returns nothing usable. hydra-llm holds a per-model profile (for example `--reasoning off --reasoning-budget 0 --reasoning-format deepseek`) so workers stay on task. Getting this right took measured throughput from about 5 to about 33 tokens/second on the same hardware.

**Local models narrate instead of acting** roughly a third of the time on harder tasks: they say "I will read the file first" and then call no tool. hydracoder counters this with imperative, tool-first prompting and an automatic retry when a turn ends with text but no tool call. The worker's tool set is configurable per task type (see `hydracoder.toml` below); the default `dev` profile includes the shell but never the package installer or the agent's own persona tools. Package installs, virtualenvs, and network fetches are forbidden two ways: the prompt says so, and a deterministic **`bash_deny`** policy in the tool gate refuses matching commands outright (the prompt alone was not enough; a weak model once ran a repo's own `make venv`, building a virtualenv indirectly). The default deny list covers pip/venv/make/npm/curl/git-clone and is overridable per workspace. lillycoder's always-on safety layer (no sudo, no rm -rf on home, no writes outside the workspace by file tools) applies regardless.

**A test file that tests nothing fails the gate.** The final verification counts the tests each generated `test_*.py` actually collects (with unittest's own loader); a file that collects zero is reported as vacuous and fails the run, and the repair loop tells the worker exactly which module still needs real tests. Without this, an empty test file exits 0 and silently proves nothing.

**The test run is the gate; model reviews are advisory.** Earlier iterations let a reviewer model block tasks. Measured on local models it rejected correct code and once hallucinated a defect while missing a real one, so the deterministic verifier (run the actual suite, repair against the actual traceback) became the sole arbiter. The reviewer remains for its second-opinion value, journalled and visible in the UI, never blocking.

**Repair may fix the code or a defective test, never weaken one.** A real run produced correct code plus generated tests with wrong expectations (off-by-one positions, an unescaped regex in `assertRaisesRegex`); a repair prompt that forbade touching tests burned every round without converging. The repair worker is now told to diagnose which side is wrong and fix that, with an explicit prohibition on deleting tests or weakening assertions.

---

## Configuration

`hydracoder serve` and `hydracoder run` accept:

| Flag | Meaning |
|---|---|
| `--workspace DIR` | where the built project files go (and the worker's working directory) |
| `--run-dir DIR` | where the journal for this run lives (point at an existing one to resume) |
| `--host` / `--http-port` / `--ws-port` | UI bind settings (default `127.0.0.1:8765` / ws `8766`) |

Model selection is automatic: hydracoder asks hydra-llm which models are downloaded and fit your hardware, then assigns roles (small / worker / reviewer / boss). You can override any role from the chat box (see below), by passing a `Roster` if you embed hydracoder as a library, or persistently per workspace via `hydracoder.toml` (below).

Per-model behavior (reasoning control, context size, sampling) lives in **hydra-llm's** catalog, not here, so the same profile benefits hydra-llm's other users too.

### hydracoder.toml (per-workspace)

Optional file in the workspace root; every key has a working default and the file is parsed strictly (unknown keys, unknown tool names, and wrong types refuse the run rather than silently changing behavior):

```toml
version = 1

[roster]                           # pin model ids per role; checked against
worker = "gemma-4-26b-a4b-it-ud"   # models that are downloaded AND fit
# small / reviewer / boss likewise

[run]
repair_rounds = 3                  # bound on the final repair loop
max_task_tokens = 2500             # per-task token budget
reviewer_enabled = true            # advisory second-model review per task
nudge_on_no_action = true          # re-prompt a model that narrated, max_nudges times
max_nudges = 2
reject_vacuous_tests = true        # a test file collecting 0 tests fails the gate
test_timeout = 120.0               # seconds for the final test run
check_repair_scope = true          # flag repair edits to unimplicated files
enforce_repair_scope = false       # also revert an out-of-scope repair round
test_first = false                 # author + audit tests before implementing
test_audit_rounds = 2              # repair rounds for a failed test audit
# bash_deny = [...]                # regex patterns of bash commands workers
                                   # may not run (default forbids pip/venv/
                                   # make/npm/curl/clone; [] allows all)

[tools]                            # per task type: profile name or explicit list
default = "dev"                    # "restricted" | "dev" | "all" | ["read_file", ...]
trivial = "restricted"             # task types are the planner's complexity levels;
# small / medium / large            # unset levels use `default`
```

Tool profiles: `restricted` is file tools only (no shell; the hardcoded default through v0.1.3), `dev` (default) adds `bash`, `rm`, `mv`, and `all` additionally exposes `pkg_install` and lillycoder's persona tools, which can mutate state outside the workspace and are never granted implicitly.

Two trust properties worth knowing: the effective config is journalled at run start and a **resumed run reuses the journalled config**, never re-reading the file (the file lives in the model-writable workspace); and a workspace config you did not write yourself deserves a read before you run against it, for the same reason.

---

## Working on an existing codebase

Pointing hydracoder at a workspace that already contains code changes the run in three concrete ways:

1. **The planner sees a survey, not a blank page.** Before planning, hydracoder builds a deterministic, size-capped snapshot of the workspace: the file tree, where the tests live, which spec/doc files exist (README, docs/, spec/, ARCHITECTURE, CONTRIBUTING, and similar), and the opening of the README. The planner is instructed to plan modifications to the real files it was shown, to not recreate what exists, and to have tasks read the listed spec documents relevant to what they change. The survey is journalled, so "what did the planner know" is auditable for every run.
2. **Your existing test suite becomes the gate.** The verifier runs both supported layouts: `test_*.py` in the workspace root, and a `tests/` or `test/` package directory (the common real-repo layout; it must contain `__init__.py`, and a test directory without one is reported as an explicit warning rather than silently skipped). Vacuous-test detection and the repair loop apply to existing tests exactly as to generated ones.
3. **Workers read before they write.** Task prompts carry the interfaces of dependency tasks, and workers have `read_file`/`grep`/`find` to learn real signatures; the system prompt requires honoring existing interfaces.

Honest current limits: spec files are surfaced to the planner and assigned as reading to tasks, but nothing yet *semantically* extracts requirements from them; very large repositories need retrieval (hydra-llm already ships a full RAG stack: indexing the workspace and attaching per-task retrieved context is the next planned integration); and only unittest layouts run today, not pytest-only suites.

---

## Trusting the result: the gate is only as strong as the suite

The deterministic test run is the only thing that gates a build, which makes the test suite the single point that decides whether "done" means anything. Two mechanisms harden it, because in practice a worker can produce code that passes a weak suite while quietly breaking something the suite never checked (observed: a repair fixed one bug and silently changed an HTTP 416 response to 200, with the suite none the wiser).

**Test-first mode** writes the tests before the code and proves they are real. Enable it with `[run] test_first = true`, the `--test-first` flag, or by asking the boss to "implement <feature> test-first" (it has a `start_feature` action). The planner then emits a test task per module *before* the implementation task that depends on it. Each test file is **audited deterministically** before any code is written: it must collect real tests (non-vacuous), it must **fail when run against the absent implementation** for a missing-implementation reason (`ImportError`/`AttributeError`/`AssertionError`, not a `SyntaxError` and not a pass), and it must import the module it targets. A test that passes against absent code asserts nothing and is rejected; a bounded test-repair loop fixes a failing audit. The honest limit, the same one any test-first process has: this proves the tests *exercise* the code, not that they encode the *correct* spec. Deciding the right answer needs ground truth (your interface description, examples) that the machine does not invent; a model's opinion on semantic correctness stays advisory, never a gate.

**The repair-scope gate** stops a repair from changing code the failing tests never pointed at. Before repairs begin, hydracoder snapshots the workspace (a throwaway git tree that never touches a real repo's branch or index). After each repair round it compares what was edited against the files the failing tests *implicate*, the traceback frames plus the modules the failing tests import. A round that edits anything else, or that touches a human test file the failures did not flag, is journalled as a `repair_scope` violation. By default this is advisory (`[run] check_repair_scope`, on); set `[run] enforce_repair_scope = true` to revert an out-of-scope round and retry. It is deliberately off-by-default for enforcement because legitimate fixes can be genuinely cross-file; the journalled flag is the safe signal.

---

## Limits and failure behavior

What actually happens at the edges, verified in code and tests rather than hoped:

**Context.** Every task starts a fresh conversation; nothing accumulates across tasks. Model context is capped by the per-model profile (16384 in the tuned profiles; a 256K window measurably crushed throughput). lillycoder estimates prompt size with a chars/4 heuristic and budgets `max_tokens` to fit the remaining window with a 15% margin (floor 512, ceiling 4096). If a task still overflows, the model server errors, the worker raises, the task is journalled failed, and the run continues; nothing corrupts. lillycoder's interactive REPL additionally auto-compacts at 90% context (summarize older turns, keep recent ones verbatim); the embedded per-task path does not compact yet: the journal defines a `COMPACTION` event for it, but with fresh-per-task contexts it has not been needed in practice.

**Out of memory.** Prevention: hydra-llm's hardware-fit check (GTT-aware on unified-memory machines) keeps the scheduler from choosing a model that cannot fit; it is a heuristic and a large context can still exhaust a constrained machine. Detection: hydra-llm inspects the container's death signals (`OOMKilled`, exit code) and reports them. Consequence: an OOM-killed model surfaces as a start failure or a mid-task transport error; the task is journalled failed and the run continues. If the host kills hydracoder itself, the journal resume restores all completed work. There is no automatic retry with a smaller context or model yet.

**The repair loop is bounded.** If the real tests still fail after the configured repair rounds (default 3), the run ends failed rather than looping forever or passing silently. The repair worker may fix the code or a genuinely defective test, and is forbidden from deleting tests or weakening assertions.

**Failure is always recorded.** Every error path writes an `error` event with where/what/recovered to the journal before the run moves on. A run is never "mysteriously stuck without a trace": the journal (and the UI streaming it) is the trace.

---

## The chat box

The chat box at the bottom of the UI is wired to a resident **boss model**. Type a question and it answers; ask it to change something and it calls a control tool. There are no commands to memorize:

- "use the tiny model for everything" sets the worker role
- "pause" / "resume" stops and restarts scheduling new tasks
- "status" reports how many tasks are queued / running / done / failed

The same actions are available as buttons in the top bar. Every control action is journalled.

---

## Extending hydracoder

Because each module has one job, common changes are localized:

- **A different planning style:** edit the prompt in `planner.py` (`PLAN_SYSTEM`). The schema and validation stay the same.
- **A different routing policy:** edit `scheduler.py` (`COMPLEXITY_TO_ROLE` and `choose_roster`).
- **A new worker tool set:** configure `[tools]` in `hydracoder.toml` (per task type), or pass `tool_subset` to `worker.run_task` when embedding.
- **A new control action:** add it to `CONTROL_TOOLS` and handle it in `boss.apply_control`.
- **A new event kind:** add a constant to `journal.py`'s event table and handle it in `reconstruct()` and the UI's `handle()`.

Run the model-free tests after any change:

```
PYTHONPATH=lib python3 tests/test_m1_core.py
PYTHONPATH=lib python3 tests/test_m3_resilience.py
```

---

## Troubleshooting

- **"no downloaded model fits this machine":** run `hydra-llm list` and `hydra-llm download <id>`. On unified-memory machines (an iGPU with shared RAM), hydra-llm counts GTT toward the fit budget, so models larger than the VRAM carve-out can still be "yes".
- **A worker returns text but writes nothing:** that is the narrate-instead-of-act behavior; hydracoder auto-retries with a stronger prompt. If it persists, the model is likely too small for that task; let the planner route harder tasks to a larger model.
- **A run seems stuck:** open the task's terminal in the UI to see its live tool calls. Local 20B-plus models are simply slow; throughput is the limit, not a hang.
- **Resume a killed run:** point `--run-dir` at the same journal directory; completed tasks are skipped.

---

## Install

hydracoder ships as a Debian package via the signed apt repository. It depends on `hydra-llm` and `lillycoder` from the same repository.

```
curl -fsSL https://ra-yavuz.github.io/apt/KEY.gpg | sudo gpg --dearmor -o /usr/share/keyrings/ra-yavuz.gpg
echo "deb [signed-by=/usr/share/keyrings/ra-yavuz.gpg] https://ra-yavuz.github.io/apt stable main" | sudo tee /etc/apt/sources.list.d/ra-yavuz.list
sudo apt update && sudo apt install hydracoder
```

From source (for development), the `hydracoder` launcher resolves the sibling `lillycoder` and `hydra-llm` repos automatically when they sit next to this one.

---

## Disclaimer

hydracoder is provided AS IS, WITHOUT WARRANTY OF ANY KIND. It runs local models that read, write, and delete files in a workspace and run shell commands on your machine. You alone are responsible for any damage to your data, hardware, or system. By running it you accept all risk. See the LICENSE file for the full terms.

## Author

Ramazan Yavuz. One of a set of public, open-source local-AI tools. Personal site: https://ramazan-yavuz.tr
