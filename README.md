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
- [The chat box: steering a run in plain language](#the-chat-box)
- [Extending hydracoder](#extending-hydracoder)
- [Troubleshooting](#troubleshooting)
- [Install](#install)
- [Disclaimer](#disclaimer)

---

## What it does

You give hydracoder a project goal and a folder of local models. It then:

1. **Plans.** A planner model decomposes the goal into a task graph: a list of tasks, each with explicit dependencies, an interface it must expose, and a concrete acceptance condition.
2. **Routes.** A scheduler sends each task to a model sized for its difficulty: a small fast model for simple leaf tasks, a larger model for harder work and for review.
3. **Builds.** Each task runs through lillycoder's agent loop in its own isolated context. Every tool call and token streams to a live terminal in the UI.
4. **Reviews.** A reviewer model checks each finished task against its acceptance condition. A failed review is retried, with the reviewer's notes fed back to the worker so it fixes the specific problem.
5. **Recovers.** Everything is written to an append-only journal before it happens. If the process is killed, the machine reboots, or a context window fills, you restart and the run resumes exactly where it stopped, with completed tasks skipped.

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
goal ─▶ planner ─▶ task graph ─▶ scheduler ─┬▶ worker (lillycoder) ─▶ reviewer ─▶ done
                                            │         ▲                   │
                                            │         └── retry with ─────┘ (on failed review)
                                            │             reviewer notes
                                            └▶ (next ready task, dependencies satisfied)
```

The scheduler runs the **ready frontier**: every task whose dependencies are already done. On a single GPU these run one after another (see [Design decisions](#design-decisions)); the UI still shows them as separate live terminals so you can follow each one.

Each worker gets a **fresh, small context**: only its own task spec plus the files it reads. This is deliberate. It keeps individual contexts from filling, which is the main reason long local-model sessions normally fall apart.

---

## Architecture

Eleven small, single-purpose Python modules under `lib/hydracoder/`. Each file has a module docstring explaining its job. Nothing is larger than about 200 lines.

| Module | Responsibility |
|---|---|
| `journal.py` | Append-only event log. The source of truth. Replay + reconstruct + live subscribe. |
| `planner.py` | Goal to task graph. `Plan`/`Task` types, cycle/dangling validation, ready-frontier. |
| `scheduler.py` | Picks a model per task by difficulty (`Roster` + `choose_roster`). |
| `worker.py` | Runs one task through lillycoder's agent loop; forwards events to the journal. |
| `reviewer.py` | A model judges a finished task against its acceptance condition. |
| `orchestrator.py` | Ties it together: plan, route, work, review, retry, resume. |
| `llm.py` | Tiny OpenAI-compatible client for planner/reviewer/boss calls (stdlib only). |
| `boss.py` | The chat box's brain and the conversational control surface (control tools). |
| `server.py` | Web server: stdlib HTTP for the UI, websockets for the live event stream. |
| `web/` | The dark UI (index.html + style.css + app.js, no build step, no CDNs). |
| `__main__.py` | CLI entry: `serve` and `run` subcommands. |

The web UI is a **pure view over the journal**: it subscribes to the event stream and renders it. It holds no authoritative state of its own, which is why a browser that connects late still sees the full history (the server replays the journal on connect).

---

## The journal

The journal (`<run-dir>/journal.jsonl`) is the heart of hydracoder's resilience. Every state change is written, flushed, and fsync'd **before** it is acted on. The event kinds are defined as named constants in `journal.py` (`RUN_STARTED`, `PLAN_CREATED`, `TASK_STATE`, `WORKER_EVENT`, `REVIEW`, `DECISION`, `ERROR`, `CONTROL`, `RUN_FINISHED`).

Two properties fall out of this:

- **Resume.** `Journal.reconstruct()` folds the log back into current state (goal, plan, per-task status). A restarted orchestrator skips tasks already marked done and continues from the frontier. A run killed mid-task picks up cleanly.
- **Crash tolerance.** `replay()` skips a truncated final line (a crash mid-write), so the rest of the log is always readable.

The in-memory `done`/`failed` sets the scheduler uses are a working cache derived from the journal, never a competing source of truth. If they ever disagree, the journal wins.

---

## Design decisions

These were established by running feasibility experiments on real local models **before** the code was written, so they are measured, not assumed.

**Right-size the model to the task; do not parallelize on a single GPU.** Running several models at once does not beat running one on a single GPU, because they share the same compute and just time-slice. In testing, a small 3.5 GB model finished a set of leaf tasks faster than a large model and faster than two models running "in parallel". So the scheduler's lever is matching model size to task difficulty, not concurrency. True multi-GPU parallelism is a separate, optional mode, not the default.

**Every model needs its own profile.** Reasoning/thinking control in llama.cpp is per-model and partly broken upstream for some families. Without the right flags, a model spends its whole token budget on hidden chain-of-thought and returns nothing usable. hydra-llm holds a per-model profile (for example `--reasoning off --reasoning-budget 0 --reasoning-format deepseek`) so workers stay on task. Getting this right took measured throughput from about 5 to about 33 tokens/second on the same hardware.

**Local models narrate instead of acting** roughly a third of the time on harder tasks: they say "I will read the file first" and then call no tool. hydracoder counters this with imperative, tool-first prompting and an automatic retry when a turn ends with text but no tool call. The worker also gets only the file tools it needs (no shell by default), which keeps a model from wandering off to build a virtualenv instead of writing code.

**Reviews catch what tests miss, and retries fix it.** The reviewer is a second line of defense before the authoritative acceptance tests. When it rejects a task, its specific reason is fed back to the worker for a corrective retry rather than the task simply failing.

---

## Configuration

`hydracoder serve` and `hydracoder run` accept:

| Flag | Meaning |
|---|---|
| `--workspace DIR` | where the built project files go (and the worker's working directory) |
| `--run-dir DIR` | where the journal for this run lives (point at an existing one to resume) |
| `--host` / `--http-port` / `--ws-port` | UI bind settings (default `127.0.0.1:8765` / ws `8766`) |

Model selection is automatic: hydracoder asks hydra-llm which models are downloaded and fit your hardware, then assigns roles (small / worker / reviewer / boss). You can override any role from the chat box (see below) or by passing a `Roster` if you embed hydracoder as a library.

Per-model behavior (reasoning control, context size, sampling) lives in **hydra-llm's** catalog, not here, so the same profile benefits hydra-llm's other users too.

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
- **A new worker tool set:** pass `tool_subset` to `worker.run_task`, or change `WORKER_TOOLS`.
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
