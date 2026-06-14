"""Per-workspace configuration: hydracoder.toml in the workspace root.

Everything has a working default; the file is optional. When present it is
parsed strictly: unknown sections, unknown keys, wrong types, unknown tool
names, and unknown profile names are ERRORS, not warnings. A typo silently
changing run behavior is worse than a refused run.

Schema (all sections optional):

    version = 1                      # optional, only 1 is accepted

    [roster]                         # pin model ids per role; validated at run
    small = "gemma-4-e2b-it"         # start against models that are actually
    worker = "gemma-4-26b-a4b-it-ud" # downloaded AND fit this machine
    # reviewer = "..."
    # boss = "..."

    [run]
    repair_rounds = 3                # final-verification repair loop bound
    max_task_tokens = 2500           # per-task token budget
    reviewer_enabled = true          # advisory model review per task
    nudge_on_no_action = true        # re-prompt when a model narrates instead
    max_nudges = 2                   #   of acting (weak-model failure mode)
    reject_vacuous_tests = true      # a test file with 0 tests fails the gate
    test_timeout = 120.0             # seconds for the final test run

    [tools]                          # per task type; profile name or name list
    default = "dev"                  # "restricted" | "dev" | "all" | [names]
    # trivial = "restricted"         # task types are the planner's complexity
    # small = "restricted"           #   levels; unset levels use `default`
    # medium = "dev"
    # large = ["read_file", "write_file", "edit_file", "bash"]

Profiles: "restricted" is file tools only (no shell; hydracoder's hardcoded
default through v0.1.3). "dev" (the default) is every developer tool including
bash/rm/mv; lillycoder's always-on hard-deny safety layer still applies.
"all" additionally exposes pkg_install and the persona tools; those can mutate
state outside the workspace and are never granted implicitly.

Trust model: hydracoder.toml lives in the workspace, which worker models can
write to. The orchestrator therefore journals the effective config at run
start and a RESUMED run reuses the journaled config, never re-reading the
file. A fresh run does read the file: review a workspace config you did not
write yourself before running against it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # only an error if a config file actually exists

from .worker import DEV_TOOLS, FILE_TOOLS

CONFIG_NAME = "hydracoder.toml"

ROLES = ("small", "worker", "reviewer", "boss")
COMPLEXITIES = ("trivial", "small", "medium", "large")

# Bash commands an orchestrated worker may not run, as regex patterns. These
# close the indirect sidestep of the no-install rule: a worker running the
# repo's own `make venv` / `pip install` / network fetch despite a prompt that
# forbids installs. Enforced deterministically in lillycoder's tool gate.
# Patterns match anywhere in the command string (word-boundaried where it
# matters) so `&&`-chained and subshell forms are caught too.
DEFAULT_BASH_DENY = [
    r"\bpip[0-9.]*\s+install\b",
    r"\bpython[0-9.]*\s+-m\s+pip\b",
    r"\b(python[0-9.]*\s+-m\s+venv|virtualenv)\b",
    r"\bmake\b",                       # repo Makefiles often install/build
    r"\b(npm|pnpm|yarn)\s+(install|i|add|ci)\b",
    r"\b(pipenv|poetry|conda|uv|cargo|go)\s+(install|add|get|sync)\b",
    r"\b(curl|wget)\b",                # network fetch
    r"\bgit\s+clone\b",
]


def registered_tool_names() -> list[str]:
    """Every tool name lillycoder's registry knows. Imported lazily so this
    module stays importable in contexts that only need parsing."""
    from lillycoder.tools import registry
    return sorted(t.name for t in registry.all_tools())


def _resolve_profile(name: str) -> list[str]:
    if name == "restricted":
        return list(FILE_TOOLS)
    if name == "dev":
        return list(DEV_TOOLS)
    if name == "all":
        return registered_tool_names()
    raise ValueError(f"unknown tool profile {name!r} "
                     f"(expected restricted, dev, or all)")


@dataclass
class RunConfig:
    repair_rounds: int = 3
    max_task_tokens: int = 2500
    reviewer_enabled: bool = True
    nudge_on_no_action: bool = True
    max_nudges: int = 2
    reject_vacuous_tests: bool = True
    test_timeout: float = 120.0
    # Repair-scope gate: after each repair round, compare what the worker
    # actually edited against the files the FAILING tests implicate. A round
    # that edits unrelated code (the silent-collateral failure: a worker
    # "improving" an adjacent function while fixing a bug, changing behavior
    # the suite does not cover) is a scope violation.
    #   check_repair_scope:   journal a scope violation when detected (always
    #                         safe; advisory by default).
    #   enforce_repair_scope: also REVERT that round and re-prompt with the
    #                         violation made explicit. Off by default because
    #                         legitimate fixes can be genuinely cross-file;
    #                         the journal flag is the safe default signal.
    check_repair_scope: bool = True
    enforce_repair_scope: bool = False
    # Test-first mode: the planner emits a test task per module that authors
    # test_<module>.py BEFORE the implementation, and each test file is audited
    # (must be non-vacuous AND fail-first for a missing-implementation reason)
    # before the code task that depends on it runs. This strengthens the suite
    # at its root: a test proven to fail when the code is absent demonstrably
    # exercises that code. Off by default (greenfield code-first is unchanged).
    test_first: bool = False
    # Bounded repair rounds for a test file that fails its audit (e.g. it was
    # vacuous or had a syntax error). Separate from code repair_rounds.
    test_audit_rounds: int = 2
    # Regex patterns for bash commands a worker may NOT run, enforced in the
    # tool gate (not merely prompted). Default forbids the network/install/
    # build-system paths that sidestep the no-install rule indirectly (a
    # worker once ran `make venv`, building a virtualenv via the repo's
    # Makefile despite the prompt forbidding installs). Set to [] to allow
    # everything the always-on safety layer permits.
    bash_deny: list = field(default_factory=lambda: list(DEFAULT_BASH_DENY))


@dataclass
class Config:
    roster: dict[str, str] = field(default_factory=dict)
    run: RunConfig = field(default_factory=RunConfig)
    # complexity (or "default") -> concrete tool-name list. "default" is
    # always present after load; profile names are resolved at load time so
    # downstream code only ever sees explicit lists.
    tools: dict[str, list[str]] = field(
        default_factory=lambda: {"default": list(DEV_TOOLS)})
    source: Optional[str] = None  # file path loaded from, None = defaults

    def tools_for_complexity(self, complexity: str) -> list[str]:
        return self.tools.get(complexity, self.tools["default"])

    def normalized(self) -> dict:
        """JSON-safe effective config, journaled at run start so a resume can
        reuse exactly what the run started with."""
        return {"roster": dict(self.roster), "run": asdict(self.run),
                "tools": {k: list(v) for k, v in self.tools.items()},
                "source": self.source}


def _check_unknown(given: dict, allowed: tuple, where: str,
                   problems: list[str]) -> None:
    for k in given:
        if k not in allowed:
            problems.append(f"unknown key {k!r} in {where} "
                            f"(allowed: {', '.join(allowed)})")


def from_dict(raw: dict, source: Optional[str] = None) -> Config:
    """Build a validated Config from parsed TOML. Raises ValueError listing
    every problem found (not just the first)."""
    problems: list[str] = []
    _check_unknown(raw, ("version", "roster", "run", "tools"),
                   CONFIG_NAME, problems)

    version = raw.get("version", 1)
    if version != 1:
        problems.append(f"unsupported version {version!r} (this hydracoder "
                        f"understands version 1)")

    # [roster]
    roster: dict[str, str] = {}
    r = raw.get("roster", {})
    if not isinstance(r, dict):
        problems.append("[roster] must be a table of role = \"model-id\"")
    else:
        _check_unknown(r, ROLES, "[roster]", problems)
        for role, mid in r.items():
            if role in ROLES:
                if isinstance(mid, str) and mid.strip():
                    roster[role] = mid.strip()
                else:
                    problems.append(f"[roster] {role} must be a non-empty "
                                    f"model-id string")

    # [run]
    run = RunConfig()
    rn = raw.get("run", {})
    if not isinstance(rn, dict):
        problems.append("[run] must be a table")
    else:
        known = {f.name: f.type for f in fields(RunConfig)}
        _check_unknown(rn, tuple(known), "[run]", problems)
        for key, val in rn.items():
            if key not in known:
                continue
            current = getattr(run, key)
            if isinstance(current, list):
                # list-typed key (bash_deny): a list of strings
                if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                    problems.append(f"[run] {key} must be a list of strings")
                    continue
            elif isinstance(current, bool):
                if not isinstance(val, bool):
                    problems.append(f"[run] {key} must be true or false")
                    continue
            elif not isinstance(val, (int, float)) or isinstance(val, bool):
                problems.append(f"[run] {key} must be a number")
                continue
            setattr(run, key, val)
        if run.repair_rounds < 0:
            problems.append("[run] repair_rounds must be >= 0")
        if run.max_task_tokens <= 0:
            problems.append("[run] max_task_tokens must be > 0")
        if run.max_nudges < 0:
            problems.append("[run] max_nudges must be >= 0")
        if run.test_timeout <= 0:
            problems.append("[run] test_timeout must be > 0")

    # [tools]
    tools: dict[str, list[str]] = {}
    tl = raw.get("tools", {})
    if not isinstance(tl, dict):
        problems.append("[tools] must be a table")
    else:
        allowed = ("default",) + COMPLEXITIES
        _check_unknown(tl, allowed, "[tools]", problems)
        for key, val in tl.items():
            if key not in allowed:
                continue
            try:
                tools[key] = _parse_tool_value(val, key)
            except ValueError as e:
                problems.append(str(e))
    tools.setdefault("default", list(DEV_TOOLS))

    if problems:
        where = source or CONFIG_NAME
        raise ValueError(f"invalid {where}:\n  - " + "\n  - ".join(problems))
    return Config(roster=roster, run=run, tools=tools, source=source)


def _parse_tool_value(val: Any, key: str) -> list[str]:
    if isinstance(val, str):
        return _resolve_profile(val)
    if isinstance(val, list) and all(isinstance(x, str) for x in val):
        known = set(registered_tool_names())
        unknown = sorted(set(val) - known)
        if unknown:
            # lillycoder's schemas_for_model silently falls back to ALL tools
            # when a subset matches nothing; refusing typos here is what keeps
            # that fallback from ever silently widening a task's allowlist.
            raise ValueError(f"[tools] {key} names unknown tools: "
                             f"{', '.join(unknown)} "
                             f"(known: {', '.join(sorted(known))})")
        if not val:
            raise ValueError(f"[tools] {key} must not be an empty list")
        return list(val)
    raise ValueError(f"[tools] {key} must be a profile name "
                     f"(restricted/dev/all) or a list of tool names")


def load_config(workspace: Path) -> Config:
    """Load <workspace>/hydracoder.toml. Missing file = defaults. A file that
    exists but cannot be parsed or validated raises ValueError: a config the
    operator wrote must never be half-applied."""
    path = Path(workspace) / CONFIG_NAME
    if not path.is_file():
        return Config()
    if tomllib is None:
        raise ValueError(f"{path} exists but no TOML parser is available "
                         f"(Python >= 3.11 or the tomli package is required)")
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"invalid TOML in {path}: {e}") from None
    return from_dict(raw, source=str(path))


def from_normalized(data: dict) -> Config:
    """Rebuild a Config from the journaled normalized() form, for resume.
    Tolerates unknown [run] keys (a journal written by a newer hydracoder)
    by ignoring them; everything it does read was validated at original load."""
    known = {f.name for f in fields(RunConfig)}
    run_kwargs = {k: v for k, v in (data.get("run") or {}).items() if k in known}
    tools = {k: list(v) for k, v in (data.get("tools") or {}).items()}
    tools.setdefault("default", list(DEV_TOOLS))
    return Config(roster=dict(data.get("roster") or {}),
                  run=RunConfig(**run_kwargs), tools=tools,
                  source=data.get("source"))


def legacy_resume_config() -> Config:
    """Effective config for resuming a journal that predates config recording.
    Those runs executed with the restricted file-tool profile and the old
    hardcoded knobs, so the resumed half behaves like the recorded half."""
    return Config(tools={"default": list(FILE_TOOLS)}, source="(legacy-resume)")
