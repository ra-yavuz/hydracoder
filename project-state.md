# hydracoder: project state

Last verified: 2026-06-13. This file is the orientation doc for a developer picking
up hydracoder. It tells you where the project is, why it is built the way it is,
what is proven, what is not, and where to continue. Read it before changing code.

hydracoder is provided AS IS, WITHOUT WARRANTY OF ANY KIND. It drives local models
that read and write files and run shell commands in a workspace directory you point
it at. You accept all risk for what those models do on your machine.

---

## 1. What hydracoder is

hydracoder is a local-AI development orchestrator with a web UI. You give it a
project goal in plain language. It then, using **local models only** (no cloud):

1. plans the work into a task graph (planner),
2. picks which local model handles each role (scheduler),
3. drives a per-task coding agent that writes the files (worker, embeds lillycoder),
4. takes an advisory second-model opinion per task (reviewer),
5. runs the real generated tests as the final gate and repairs against real
   tracebacks (verifier + orchestrator repair loop),
6. records everything to an append-only journal so a crashed run can resume,
7. streams all of this to a dark web UI over a websocket, with a persistent chat
   box that routes to a "boss" model.

The motivating reason (the "why" behind the whole thing): keep being able to
develop locally when corporate AI (Claude, Codex, and similar) stops being cheap.
This is deliberately a doomsday-prep capability. It values reliability and
human-maintainability over raw speed, and right-sizing many small models over one
big one.

---

## 2. The three repos and how they relate

hydracoder is the orchestrator. It does **not** reimplement model management or the
coding agent loop. It imports two sibling repos as libraries. All three are
independent, releasable, standalone tools that synergize:

```
~/github-ra-yavuz/
  hydracoder/     this repo: planner + scheduler + worker driver + reviewer +
                  verifier + journal + web UI + boss chat
  lillycoder/     the per-task coding agent (read/write/edit/grep/bash + safety +
                  permissions). hydracoder embeds it as a library per task.
  hydra-llm/      the local model substrate (Docker llama-server containers,
                  download, hardware-fit, idle reaper, RAG). hydracoder asks it
                  what models exist, what fits, and to start/stop them.
```

### How the import works (this is the integration contract)

hydracoder imports the siblings by adding their `lib/` dirs to `PYTHONPATH`. There
is no pip dependency between them; they are resolved by path. The runner, CI, and
the e2e script all set:

```
export PYTHONPATH="hydracoder/lib:../lillycoder/lib:../hydra-llm/lib"
```

So a checkout for development is: clone all three side by side under one parent dir.

Concrete call sites (verified against the code, not assumed):

- `lib/hydracoder/orchestrator.py:16` does `from hydra_llm import api as hydra`.
  The orchestrator talks to hydra-llm only through that `api` module
  (`list_models`, `status`, `model_status`, `start`, `stop`, `hardware_snapshot`).
- `lib/hydracoder/worker.py:16-19` does `from lillycoder import agent`,
  `from lillycoder.endpoint import ModelInfo`, `from lillycoder.discovery import
  manual_endpoint`, `from lillycoder.sink import RecordingSink`. Each task is one
  lillycoder agent run pointed at a hydra-llm endpoint.
- `lib/hydracoder/scheduler.py` consumes `hydra-llm`'s `list_models()` output
  (dicts with `id`, `fit`) and never picks a model whose `fit` is not satisfiable
  on this machine.

### Why the changes to lillycoder and hydra-llm were needed

To embed lillycoder cleanly, lillycoder gained generic, reusable capabilities
(they are useful standalone too, which is why they shipped in lillycoder, not
here):

- `lillycoder/lib/lillycoder/sink.py`: a Sink protocol (ConsoleSink for the REPL,
  RecordingSink for headless/embedded) so the agent loop is decoupled from the
  terminal. hydracoder's worker subclasses RecordingSink to forward every token,
  tool call, and tool result into the journal and out to the web UI. This is the
  "see what the model is generating live" capability.
- `lillycoder/lib/lillycoder/headless.py` + `--prompt`/`--tools` flags: run one
  task non-interactively.
- `agent.run_turn` gained `nudge_on_no_action` (re-prompt when a weak model ends a
  turn with text but no tool call, about a 30% failure mode on small models) and
  `tool_subset` (restrict a task to a safe allowlist of tools).
- a `_in_workdir` contextmanager that chdirs into the task workspace so file tools
  resolve relative paths against the workspace, not the process CWD. This was a
  real bug that made the first spike fail.

hydra-llm gained the `api.py` module (a clean library surface over the CLI) and a
GTT-aware `fits_locally()` in `hardware.py` (so iGPU machines with shared memory
are not wrongly told a model will not fit).

Sibling changes added 2026-06-13, all driven by live experiment failures (each
is unit-tested in its own repo and useful standalone):

- lillycoder `tools/read.py`: read_file is line-windowed (200 lines / 8KB
  default, `start_line`/`max_lines` params) with an instructive truncation
  hint. The old 200KB default let a worker pull a 4,500-line file into a 16K
  context, killing the turn before it could edit.
- lillycoder `agent.py` `RepeatGuard`: identical read-only tool calls within a
  turn get a short "act on what you already read" stub instead of re-running.
  Mutating calls reset the guard (edit-then-re-read verification still works).
  Observed: a worker re-read the same located region three times and died of
  context bloat without editing.
- hydra-llm `docker_driver.py` `symlink_file_mounts()`: a GGUF that is a
  symlink to storage outside models_dir gets its resolved target bind-mounted
  into the container (the bare directory mount carried a dangling link;
  llama-server exited 1 with no logs because of `--log-disable`).
- lillycoder `agent.py`: `_gate_tool_call`/`run_turn` gained an optional
  `bash_deny` (regex list) policy layer so an embedder can forbid specific
  bash commands deterministically; default None leaves the standalone REPL
  unchanged. hydracoder uses it for the no-install policy (caveat 10).
- machine config, not repo: `qwen-3.6-27b-neo-code` now has a no-thinking
  profile in `~/.config/hydra-llm/catalog.yaml` (probe-verified: thinking
  collapses to an empty tag pair; native tool calling works).

Versions currently released (verified in the apt index 2026-06-12):
hydracoder 0.1.3-1, lillycoder 0.2.2-1, hydra-llm 0.2.12-1.

---

## 3. Module map (this repo)

All under `lib/hydracoder/`:

| File | Role |
|---|---|
| `journal.py` | Append-only event log (fsync per append). Source of truth. `replay` tolerates a truncated tail; `reconstruct` rebuilds run state for resume; `subscribe` feeds the live UI. `run_started` now carries the normalized effective config. |
| `config.py` | Per-workspace `hydracoder.toml`: roster pins, run knobs (repair rounds, token budget, reviewer toggle, nudges, vacuous-test gate, test timeout, repair-scope checks, test-first, `bash_deny` policy), per-task-type tool allowlists with profiles (`restricted`/`dev`/`all`). Strict parsing: unknown keys/tools/types refuse the run. Resume reuses the journaled config, never the file. |
| `planner.py` | `Plan`/`Task` (now carry `kind` code/test + `targets`), `make_plan` (asks a model for a task graph; `test_first=True` emits test tasks before code tasks), `validate`, `ready_tasks`. |
| `scheduler.py` | `Roster` (small/worker/reviewer/boss), `choose_roster` picks real model ids from hydra-llm's fit-aware list. Prefers a small model for leaf tasks and a MoE/a4b model for the worker role. |
| `survey.py` | Deterministic bounded snapshot of an existing workspace for the planner (brownfield awareness): file tree, test layout, spec/doc inventory, README excerpt. Empty for greenfield. |
| `worker.py` | `run_task`: one lillycoder agent run per task. Default tool profile is `dev` (file tools + bash/rm/mv); a `kind="test"` task is told to write ONLY the test file. Streams to the journal via `JournalSink`. |
| `reviewer.py` | `review_task`: a second model gives a verdict. ADVISORY ONLY; it never blocks a task (see section 5). |
| `verifier.py` | `run_tests`: runs the real test suite (root `test_*.py` and `tests/`-package layouts) in the workspace, counts collected tests (vacuous gate), returns structured pass/fail. Deterministic. |
| `test_audit.py` | Test-first auditor: a test file must be non-vacuous AND fail-first for a missing-implementation reason (Import/Attribute/AssertionError, not SyntaxError or pass) AND import its target. Deterministic; proves a test EXERCISES the code before any code exists. |
| `repair_scope.py` | Repair-scope gate: git-snapshots before repairs, then per round flags edits to files the failing tests never implicated (the silent-collateral detector). Deterministic; advisory by default, can enforce (revert). |
| `orchestrator.py` | `run`: plan (brownfield survey + optional test-first), execute the ready frontier (test tasks audited inline in test-first mode), then `_final_verification` (run tests, repair up to N rounds against the real traceback, with per-round scope checks). The verifier, not any model opinion, is the final arbiter. |
| `boss.py` | The chat-box "boss" model that can answer and delegate mid-run. |
| `server.py` | stdlib `http.server` for static UI + a websocket that replays the journal and streams live events. On connect it also sends the hydra-llm model inventory + current roster (for the roster panel); control results are broadcast as notices. |
| `llm.py` | thin client helpers for talking to hydra-llm endpoints. |
| `web/` | BUILT static UI (committed output of `webapp/`, fonts bundled, no CDNs). Do not edit by hand. |
| `__main__.py` | CLI entry; `bin/hydracoder` wraps it. |

Outside `lib/`: `webapp/` is the UI source, a Vite + vanilla TypeScript app
(no framework, devDependencies only). `npm run build` type-checks and emits
into `lib/hydracoder/web/`. The UI was rebuilt 2026-06-12 from the old 3-file
vanilla app with the same websocket protocol plus: per-task terminal windows
with focus/maximize, model-id badges (task_state events now carry `model`), a
synthetic "(final) verification" window for the deterministic gate, a roster
drawer that pins models to roles mid-run, and a run banner. Verified by
headless-Chromium screenshot against a seeded journal, and test_m2 against the
built output.

---

## 4. How to run it

### The fast proof (unit + integration tests, no models needed)

```
cd ~/github-ra-yavuz/hydracoder
export PYTHONPATH="lib:../lillycoder/lib:../hydra-llm/lib"
python3 tests/test_m1_core.py        # 8/8  journal, plan, scheduler
python3 tests/test_m3_resilience.py  # 3/3  crash+resume, token budget, worker error
python3 tests/test_m3_verifier.py    # 8/8  verifier incl. state-isolation + vacuous gate
python3 tests/test_m4_config.py      # 9/9  hydracoder.toml, tool routing, resume config
python3 tests/test_m5_brownfield.py  # 10/10 survey, planner survey prompt, test/ layouts
python3 tests/test_m6_repair_scope.py # 14/14 repair-scope gate (collateral detector)
python3 tests/test_m7_test_first.py  # 13/13 test-first audit (fail-first proof)
python3 tests/test_m2_server.py      # 2/2  http serves UI, websocket replays journal
```

These pass today (verified 2026-06-12, all exit 0). They are pure-Python and need
no running models. CI runs all but test_m2 (it binds a port, so it is run
locally). Note the tests are plain `def test_*()` functions run as scripts, NOT
unittest TestCase classes, so `python3 -m unittest discover` finds 0 of them. Run
the files directly, as CI does.

### The full end-to-end proof (needs local models running)

`tests/e2e_test_project.sh` is the real demonstration: it asks hydracoder to build
a small stdlib-only todo web app (store.py, server.py, index.html, test_store.py)
with local models only, then runs the generated `test_store.py` and exits non-zero
if it fails. Requires a hydra-llm worker model available/running.

```
# bring up a worker model first via hydra-llm, then:
tests/e2e_test_project.sh gemma-4-26b-a4b-it-ud     # greenfield todo app
tests/e2e_interpreter.sh gemma-4-26b-a4b-it-ud      # complex greenfield (interpreter)
tests/e2e_existing_repo.sh <workspace> "<goal>" [worker]   # brownfield, any repo
```

All three e2e scripts gate hard: real test suites, per-module test depth,
behavioral smoke checks (interpreter), and for brownfield a sha256 fingerprint
proving test files were not touched.

### The actual product (web UI)

```
hydracoder            # or: python3 -m hydracoder
# open the printed http://127.0.0.1:<port>/ , enter a goal, watch the models work
```

---

## 5. Design decisions and why (the non-obvious ones)

- **The deterministic verifier is the final gate, model reviews are advisory.**
  Earlier iterations let a model reviewer block tasks. It repeatedly rejected
  correct code and, worse, once hallucinated "truncated" and missed a real bug.
  Running the actual tests is the only trustworthy signal. `reviewer.py` is kept
  for its second-opinion value but `orchestrator._run_one` treats a task as done
  once the worker produced files without error; `_final_verification` is the sole
  arbiter and repairs against real tracebacks.
- **Per-task review must never run the whole suite.** An earlier bug ran the full
  test suite after every task, failing correct early tasks because later tasks'
  tests did not exist yet. The whole-suite run happens once, at the end.
- **Worker tools are config-driven; the default changed in 0.2 (operator
  decision).** Through v0.1.3 workers had file tools only (no bash). The default
  is now the `dev` profile (file tools + bash/rm/mv) per the operator's
  default-allow choice; the `restricted` profile is one config line away
  (`[tools] default = "restricted"`). Never granted implicitly, even by `dev`:
  `pkg_install` (the documented runaway failure) and lillycoder's persona tools
  (agent self-modification). lillycoder's always-on hard-deny safety layer
  (sudo, rm -rf on home/root, mkfs, device writes, out-of-workspace file-tool
  writes) applies regardless of profile, even with bypass_perms. Note that
  bash commands are gated only by that deny list, NOT confined to the
  workspace; this is the accepted residual risk of default-allow.
- **Vacuous tests fail the gate.** `verifier.run_tests` counts collected tests
  per module with unittest's own loader; any `test_*.py` collecting zero tests
  fails verification by name, and the repair loop feeds that to a worker. This
  closed the original e2e hole (an empty `test_server.py` passing silently).
- **The gate is only as strong as the suite, so the suite is hardened two ways.**
  (a) The repair-scope gate (`repair_scope.py`) flags repair edits to code the
  failing tests never implicated, catching silent collateral the suite cannot
  see. (b) Test-first mode (`test_audit.py`) authors tests before code and
  proves each one FAILS against the absent implementation, so a test that
  asserts nothing cannot slip through. Both are deterministic. Test-first's
  honest limit: it proves a test exercises the code, not that it asserts the
  correct answer (the oracle problem); semantic correctness stays advisory.
  Grounded against CODEX, which independently reached the same A+constrained-B
  recommendation and named "oracle laundering" as why model-authored tests
  cannot self-certify.
- **Resume trusts the journal's config, not the file.** `hydracoder.toml` lives
  in the model-writable workspace, so a resumed run reuses the journaled
  effective config; re-reading the file midway would let a worker widen its own
  allowlist for the rest of the run. Journals predating config recording resume
  with the old restricted defaults.
- **Context is capped (ctx-size 16384) and thinking is disabled per-model.** A 256K
  context crushed throughput; leaving "thinking" on made weak models burn their
  whole token budget producing empty content. Per-model worker profiles in
  `~/.config/hydra-llm/catalog.yaml` set
  `--jinja --reasoning off --reasoning-budget 0 --reasoning-format deepseek`.
  Thinking-mode handling is per-model and partly broken upstream in llama.cpp;
  every model wants its own flags. This is fragile (see caveats).
- **Journal is the source of truth; in-memory done/failed sets are a derived
  cache.** Resume reconstructs from the journal, which is why a crash mid-run does
  not lose completed tasks.
- **Right-size, do not fan out on one GPU.** "Parallel small models" on a single
  GPU is an illusion (they serialize on the device). The scheduler instead matches
  model size to task difficulty: a tiny model for leaf tasks, a MoE worker for the
  rest. Genuine parallelism would need multiple endpoints/devices.

---

## 6. What is proven

- The orchestrator core: journal append/replay/resume/truncation-tolerance, plan
  validation, scheduler roster selection. (test_m1, 8/8)
- Resilience: crash-midway then resume completes without losing finished tasks; a
  tiny token budget does not crash the run; a worker exception is recorded as a
  failure and the run continues. (test_m3_resilience, 3/3)
- The verifier: detects passing, failing, no-tests, a deliberately planted test
  state-isolation bug, AND vacuous test files (0 collected tests fail the gate,
  import errors are correctly NOT classified as vacuous). (test_m3_verifier, 8/8)
- Configurability: strict hydracoder.toml parsing, per-task-type tool routing
  reaching the worker, reviewer toggle, token budget from config, roster
  validation against usable models, and resume preferring the journaled config
  over an edited workspace file. (test_m4_config, 9/9)
- Brownfield machinery: workspace survey reaching the planner, package-dir
  test layouts discovered/counted/vacuous-gated, non-importable test dirs
  warned explicitly. (test_m5_brownfield, 10/10)
- A genuinely complex greenfield task (2026-06-13, live models): the
  expression-language interpreter e2e (lexer -> parser -> evaluator -> REPL,
  precedence/associativity/error contracts) passed with 15 real tests and a
  behavioral REPL check (42 / 512 / -9 / div-by-zero). The run also proved
  the repair loop can fix DEFECTIVE GENERATED TESTS: rounds 1-2 failing,
  round 3 green (the prior prompt forbade touching tests and burned all
  rounds at a constant failure count). `tests/e2e_interpreter.sh`.
- Brownfield on a real OSS repo (2026-06-13, live models): bottle (4,583-line
  module, 377-test suite) with a planted off-by-one in parse_range_header.
  With the gemma worker, hydracoder converged to the EXACT one-line fix
  (diff vs pristine empty), full suite green, test files cryptographically
  untouched. `tests/e2e_existing_repo.sh`. This needed the windowed-read and
  repeat-guard fixes above; the journal of each failed round is what located
  every defect.
- Repair-scope and test-first machinery, deterministic. (test_m6, 14/14;
  test_m7, 13/13)
- The `bash_deny` policy, live: a bottle brownfield re-run journaled the
  worker's `make` command DENIED by the policy gate, then converged to the
  exact one-line fix with tests untouched (suite green). Proves the no-install
  rule is now enforced deterministically, not just prompted. (test_m4_config
  for the config/wiring; lillycoder test_m0a for the gate.)
- Test-first END TO END with live models (2026-06-13): a Stack module built
  test-first. The journal shows the order that matters: the test task wrote
  test_stack.py FIRST, the audit PASSED ("good fail-first: 1 real test fails
  because the implementation is absent", i.e. proven red against no stack.py),
  THEN the code task implemented Stack, and final verification passed 6 real
  tests. `tests/e2e_test_first.sh`. Two bugs were found and fixed live in the
  process: the planner naming the test file as its own target (prompt + audit
  normalization), and an orphaned model container from an over-broad pkill
  (infra, not code).
- The server: serves the UI and the websocket replays the journal. (test_m2, 2/2)
- The end-to-end claim: local models, driven by hydracoder, built a todo app
  with REAL tests for every module: `test_server.py` (5 methods: GET /, GET
  and POST /api/todos, invalid-JSON 400, missing-text 400) and `test_store.py`
  (4 methods on add/list/complete/remove), 9 tests run by the deterministic
  gate, OK, exit 0 (re-proven 2026-06-12 with the hardened gate and the `dev`
  tool default). This is the authoritative "local models wrote working,
  self-tested code" evidence.

---

## 7. Honest caveats and unproven things

Read this section before you trust hydracoder with anything important.

1. **[CLOSED 2026-06-12] Vacuous tests no longer pass the gate.** The verifier
   now counts collected tests per module and fails verification when any
   `test_*.py` collects zero (unit-tested in `test_m3_verifier.py`; the e2e
   script independently re-checks every generated test file). The worker and
   planner prompts also demand real assertions for every module. The e2e was
   RE-RUN with these changes the same day (live models, exit 0): 5/5 tasks
   done, and this time the models produced `test_server.py` with 5 REAL test
   methods and `test_store.py` with 4 (9 tests ran, OK), so the historical
   vacuous-test hole did not recur; the run also exercised the new `dev` tool
   default (worker used bash productively, no runaway). Residual limitation:
   a test with weak assertions (e.g. `assertTrue(True)`) still passes; only
   emptiness is detected, not assertion quality.
2. **One worker model proven, not "many small models beat one big one".** The
   thesis that a swarm of small models rivals a big one is plausible and the
   architecture supports it, but it is not demonstrated. The proven path is one
   right-sized worker model. Single-GPU parallelism does not actually run models
   concurrently.
3. **Per-model thinking/flag tuning is fragile and machine-specific.** The catalog
   profiles were tuned for specific gemma models on one machine (AMD Strix Point,
   iGPU + GTT). A different model or box may reason when it should not, or refuse
   tool calls. There is no automatic per-model probe yet; new models need manual
   profiling. Upstream llama.cpp thinking-mode bugs (issues referenced in the
   memory notes) mean some models cannot fully disable thinking.
4. **Boss/chat mid-run delegation is lightly exercised.** The boss model and chat
   routing exist and the websocket carries them, but there is no automated test
   that proves a mid-run chat instruction changes the run. Treat it as functional
   but unproven under load.
5. **The repair loop is bounded at 3 rounds.** If real tests still fail after 3
   repair rounds, the run ends failed. This is intentional (no infinite loops) but
   means hard tasks can be left unfinished rather than silently "passed".
6. **Hardware fit is heuristic.** `fits_locally()` is GTT-aware now, but it is an
   estimate. A model reported as fitting can still OOM under a large context on a
   constrained machine.
7. **No cross-machine / multi-endpoint orchestration.** Everything assumes one
   hydra-llm host. Real parallelism across machines is not built.
8. **Logo is a placeholder-quality geometric mark**, not a hand-drawn hydra like
   the rest of the family. Cosmetic, listed for completeness.
9. **The gate is only as strong as the test suite.** Demonstrated live
   (2026-06-13): on the bottle task, the qwen worker restored the planted line
   but ALSO refactored static_file's range handling, silently changing
   unsatisfiable-Range behavior from HTTP 416 to a 200 full-body response.
   The suite stayed green because bottle's own tests do not cover that path.
   Repair prompts now demand minimal diffs, but prompt discipline is soft;
   a deterministic diff-size/diff-location check per repair round is the
   structural fix (see section 8).
10. **[CLOSED 2026-06-13] The no-install rule can be sidestepped indirectly.**
   A worker ran bottle's own `make venv`, building a virtualenv despite the
   prompt forbidding installs. Fixed structurally by the `bash_deny` policy
   (item 1d): a re-run of the same bottle brownfield task is journaled
   DENYING the worker's `make` command ("policy: command matches a denied
   pattern '\\bmake\\b'"), after which it completed the exact one-line fix
   anyway. Enforcement is now deterministic in the tool gate, not prompt-only.
11. **Model temperament differs at equal capability.** Head-to-head on the
   same planted bug: gemma-4-26b-a4b converged in 3 repair rounds to the
   exact minimal fix; qwen-3.6-neo-code converged in 2 rounds but edited
   beyond the implicated lines (the 416 regression above). Faster is not
   better when the suite cannot see the collateral.

---

## 8. Where to continue (ranked by value)

1. **[DONE 2026-06-12] Reject vacuous tests at the gate.** Implemented in
   `verifier.run_tests` (loader-based count, `reject_vacuous` knob), the worker
   system prompt, the planner prompt, and an independent assertion in
   `tests/e2e_test_project.sh`. The live e2e was re-run the same day and
   passed (exit 0) with real tests for every module; see caveat 1.
1b. **[DONE 2026-06-13] Per-repair-round scope gate.** `repair_scope.py`:
   snapshots the workspace (throwaway git, never touching a real repo's HEAD/
   index), then per repair round flags edits to files the failing tests never
   implicated ("implicated" = traceback files + modules the failing test
   imports, because a plain assertEqual failure names only the test file).
   Config `[run] check_repair_scope` (default on, journals a `repair_scope`
   event) and `enforce_repair_scope` (default off, reverts the round). 14
   tests in `test_m6_repair_scope.py`. Directly addresses caveats 9 and 11.
1c. **[DONE 2026-06-13] Test-first mode.** `test_audit.py` + planner
   `kind`/`targets` + orchestrator inline audit. `[run] test_first` (or
   `--test-first`, or the boss `start_feature` control action): the planner
   emits a test task per module before the code task; each test file is
   audited (non-vacuous AND fails-first for a missing-implementation reason
   AND imports its target) before any dependent code runs, with a bounded
   test-repair loop. Strengthens the suite at its root, with the honest limit
   that it proves tests EXERCISE the code, not that they encode the right
   spec (the oracle problem; semantic correctness stays advisory). 12 tests
   in `test_m7_test_first.py`, live e2e `tests/e2e_test_first.sh`.
1d. **[DONE 2026-06-13] Config-driven bash command policy.** `[run] bash_deny`
   (regex list, default forbids pip/venv/make/npm/curl/git-clone) enforced
   deterministically in lillycoder's tool gate (`_gate_tool_call` gained a
   `bash_deny` param; `run_turn` threads it; `worker.run_task` and all three
   orchestrator call sites pass `cfg.bash_deny`). lillycoder standalone is
   unaffected (default None = no policy denial). Tests: lillycoder
   `test_m0a_patches` (gate behavior) + hydracoder `test_m4_config` (default,
   override, validation, wiring). Closes caveat 10.
2. **Prove or disprove the multi-small-model thesis** with a second endpoint (two
   hydra-llm containers, or a second device) so the scheduler can actually run two
   workers at once, then measure against one large model on the same goal.
3. **Automatic per-model profiling** in hydra-llm: a probe that detects whether a
   model honors `--reasoning off` and tool calls, and writes a catalog profile, so
   adding a model does not require hand-tuning. Lives in hydra-llm, not here.
4. **A boss-delegation test**: scripted mid-run chat that demonstrably redirects
   the run, recorded in the journal.
5. **lillycoder "local Claude Code" UX** (hooks, /goal, settings) was explicitly
   deferred as a parallel track. It is lillycoder work, not hydracoder work, but it
   is the long-term direction for the standalone agent.
6. **Replace the placeholder logo** with a drawn hydra matching the family (hub
   `logo-hydracoder.{webp,png}` and website `logo-hydracoder.webp`).

---

## 9. Release and finalization state

All three are released and live on the public surfaces required by the repo
conventions (verified 2026-06-12):

- apt index: hydracoder 0.1.3-1, lillycoder 0.2.2-1, hydra-llm 0.2.12-1
- GitHub Pages for hydracoder resolves (HTTP 200) and is linked from the hub
- the trilingual website project cards and profile README list hydracoder
- the liability disclaimer is on README, docs/index.html, the CLI help, and
  debian/control

CI (`.github/workflows/ci.yml`) lints, runs the four script-style integration
tests with the siblings cloned and the runtime deps installed, builds the .deb, and
on a `v*` tag publishes a GitHub Release and dispatches the apt-repo rebuild. CI was
iterated to a clean-venv-verified pass; reproduce the CI environment locally before
tagging a new release.

For the full per-repo conventions (logos, disclaimer placement, website cards,
packaging), see `~/github-ra-yavuz/CLAUDE.md`.
