"""Workspace survey: a deterministic, bounded text snapshot of an existing
codebase for the planner.

Without this the planner is blind: make_plan() sees only the goal text, so
pointing hydracoder at an existing project produced a greenfield plan that
ignored (and would overwrite) what is already there. The survey gives the
planner the file tree, where the tests live, which spec/doc files exist, and
the opening of the primary README, all hard-capped in size so it cannot crowd
a 16K context. It is deterministic (no model in the loop) and journaled, so
"what did the planner know" is auditable per run.
"""
from __future__ import annotations

from pathlib import Path

# Directories that never inform planning (caches, VCS, build output).
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist",
             "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
             ".eggs", "hydracoder-run"}

# Spec/doc artifacts the planner must know exist (matched case-insensitively
# on the stem). Directories named docs/spec are listed too.
SPEC_STEMS = {"readme", "agents", "claude", "contributing", "architecture",
              "design", "spec", "specs", "roadmap", "changelog", "todo"}
SPEC_DIRS = {"docs", "doc", "spec", "specs", "rfcs"}

MAX_TREE_ENTRIES = 150
MAX_README_CHARS = 1500


def _iter_files(ws: Path):
    """All files under ws, skipping SKIP_DIRS, sorted for determinism."""
    stack = [ws]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS:
                    stack.append(p)
            elif p.is_file():
                yield p


def has_existing_code(workspace: Path) -> bool:
    """True when the workspace already contains files worth planning around
    (anything beyond hydracoder's own config)."""
    ws = Path(workspace)
    if not ws.is_dir():
        return False
    for p in _iter_files(ws):
        if p.name != "hydracoder.toml":
            return True
    return False


def survey_workspace(workspace: Path, max_chars: int = 4000) -> str:
    """A bounded plain-text survey of the workspace, or "" when the workspace
    is (effectively) empty so greenfield planning stays exactly as before."""
    ws = Path(workspace)
    if not has_existing_code(ws):
        return ""

    files = list(_iter_files(ws))
    rels = [p.relative_to(ws) for p in files]

    # size + language stats
    total_kb = sum((p.stat().st_size for p in files), 0) // 1024
    by_ext: dict[str, int] = {}
    for r in rels:
        ext = r.suffix.lower() or "(none)"
        by_ext[ext] = by_ext.get(ext, 0) + 1
    top_ext = sorted(by_ext.items(), key=lambda kv: -kv[1])[:6]

    # tests
    root_tests = sorted(str(r) for r in rels
                        if r.parent == Path(".") and r.name.startswith("test_")
                        and r.suffix == ".py")
    test_dirs = sorted({r.parts[0] for r in rels
                        if len(r.parts) > 1 and r.parts[0] in ("test", "tests")})

    # specs / docs
    spec_files = sorted(str(r) for r in rels
                        if r.stem.lower() in SPEC_STEMS
                        or (len(r.parts) > 1 and r.parts[0].lower() in SPEC_DIRS
                            and r.suffix.lower() in (".md", ".rst", ".txt")))

    # primary README excerpt
    readme_excerpt = ""
    for cand in ("README.md", "README.rst", "README.txt", "README"):
        p = ws / cand
        if p.is_file():
            try:
                readme_excerpt = p.read_text(errors="replace")[:MAX_README_CHARS]
            except OSError:
                pass
            break

    tree_lines = [str(r) for r in sorted(rels, key=str)[:MAX_TREE_ENTRIES]]
    more = len(rels) - len(tree_lines)

    parts = [
        f"{len(rels)} files, {total_kb} KB. "
        f"By type: {', '.join(f'{e}={n}' for e, n in top_ext)}.",
        "FILE TREE:\n" + "\n".join("  " + line for line in tree_lines)
        + (f"\n  ... and {more} more files" if more > 0 else ""),
    ]
    if root_tests or test_dirs:
        t = []
        if root_tests:
            t.append("root test files: " + ", ".join(root_tests))
        for d in test_dirs:
            init = "importable package" if (ws / d / "__init__.py").is_file() \
                else "NO __init__.py (unittest cannot import it)"
            t.append(f"{d}/ directory ({init})")
        parts.append("TESTS: " + "; ".join(t))
    else:
        parts.append("TESTS: none found")
    if spec_files:
        parts.append("SPEC/DOC FILES (read the relevant ones before changing "
                     "behavior they describe): " + ", ".join(spec_files[:25]))
    if readme_excerpt:
        parts.append("README (start):\n" + readme_excerpt)

    return ("\n\n".join(parts))[:max_chars]
