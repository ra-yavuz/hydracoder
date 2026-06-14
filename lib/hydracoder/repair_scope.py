"""Repair-scope gate: keep a repair round from changing code the failing
tests never implicated.

The failure this exists for (observed live on a real repo): a worker asked to
fix a planted off-by-one ALSO refactored an adjacent function, silently
changing an HTTP 416 response to 200. The test suite stayed green because it
did not cover that path, so the deterministic gate, the thing we trust,
could not see the regression. The lesson (project-state caveats 9, 11): the
gate is only as strong as the suite, so when the suite is weak we must
constrain the EDIT instead of trusting the test result alone.

This module is deterministic (git + traceback parsing, no model in the loop).
It snapshots the workspace before repairs and, after each round, answers one
question: did this round edit any file that the failing tests did not point
at? It does not decide correctness; it flags collateral.

Design notes:
- We use a throwaway git repo INSIDE the workspace (.git is created only if
  absent, and the workspace is the model's sandbox anyway). A pre-existing
  repo is used as-is and never committed to: we stash-free snapshot via a
  temporary index so the user's real git state is untouched.
- "Implicated files" come from the file paths in the failing-test traceback,
  restricted to files that actually live in the workspace. A test file is
  implicated by its own failures, so during brownfield repair, editing a
  test that was not itself failing is also a violation.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional


def _git(workspace: Path, *args: str, check: bool = True,
         env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(workspace),
                          capture_output=True, text=True, check=check,
                          env=env)


def available() -> bool:
    """True if git is on PATH (the gate degrades to a no-op without it)."""
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


class RepairScopeTracker:
    """Snapshots the workspace, then reports per-round changed files.

    Snapshotting never mutates a pre-existing repo's HEAD, branch, or index:
    it writes a tree object from a private temporary index and remembers that
    tree's id. Diffs are computed against that tree. For a non-repo workspace
    it initialises a throwaway repo (the workspace is a disposable sandbox).
    """

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self._base_tree: Optional[str] = None
        self._index_file: Optional[str] = None
        self._own_repo = False
        self._ok = False

    def _env_with_index(self) -> dict:
        env = dict(os.environ)
        # Private index so we never touch the user's staging area.
        env["GIT_INDEX_FILE"] = self._index_file  # type: ignore[assignment]
        # Deterministic identity so commits/trees never depend on user config.
        env.setdefault("GIT_AUTHOR_NAME", "hydracoder")
        env.setdefault("GIT_AUTHOR_EMAIL", "hydracoder@localhost")
        env.setdefault("GIT_COMMITTER_NAME", "hydracoder")
        env.setdefault("GIT_COMMITTER_EMAIL", "hydracoder@localhost")
        return env

    def snapshot(self) -> bool:
        """Record the pre-repair tree. Returns False (gate disabled) if git is
        unavailable or snapshotting fails for any reason; the caller then skips
        scope checks rather than blocking a run on an infrastructure issue."""
        if not available():
            return False
        try:
            is_repo = _git(self.workspace, "rev-parse", "--is-inside-work-tree",
                           check=False).returncode == 0
            if not is_repo:
                _git(self.workspace, "init", "-q")
                self._own_repo = True
            self._index_file = str(self.workspace / ".git" / "hydracoder-scope.index")
            env = self._env_with_index()
            # Stage everything into the private index, then write a tree.
            _git(self.workspace, "add", "-A", env=env)
            tree = _git(self.workspace, "write-tree", env=env).stdout.strip()
            self._base_tree = tree or None
            self._ok = bool(tree)
            return self._ok
        except (OSError, subprocess.CalledProcessError):
            return False

    def changed_files(self) -> list[str]:
        """Workspace-relative paths changed since snapshot(). Empty if the gate
        is disabled or nothing changed."""
        if not self._ok or self._base_tree is None:
            return []
        try:
            env = self._env_with_index()
            _git(self.workspace, "add", "-A", env=env)
            cur = _git(self.workspace, "write-tree", env=env).stdout.strip()
            out = _git(self.workspace, "diff", "--name-only",
                       self._base_tree, cur, env=env).stdout
            return [line for line in out.splitlines() if line.strip()]
        except (OSError, subprocess.CalledProcessError):
            return []

    def rebase_to_current(self) -> None:
        """Advance the baseline to the current state, so the NEXT round's
        diff reflects only that next round's edits (each round is judged on
        its own, not cumulatively)."""
        if not self._ok:
            return
        try:
            env = self._env_with_index()
            _git(self.workspace, "add", "-A", env=env)
            self._base_tree = _git(self.workspace, "write-tree",
                                   env=env).stdout.strip() or self._base_tree
        except (OSError, subprocess.CalledProcessError):
            pass

    def revert_to_snapshot(self) -> bool:
        """Restore the workspace to the snapshot tree (used when enforcing).
        Returns True on success. Only files tracked at snapshot time are
        restored; brand-new files the round created are removed."""
        if not self._ok or self._base_tree is None:
            return False
        try:
            env = self._env_with_index()
            # Reset the private index to the base tree, then check it out over
            # the working tree, and remove anything not in the base tree.
            _git(self.workspace, "read-tree", self._base_tree, env=env)
            _git(self.workspace, "checkout-index", "-a", "-f", env=env)
            _git(self.workspace, "clean", "-fdq", env=env)
            return True
        except (OSError, subprocess.CalledProcessError):
            return False


# --- pure helpers (no git, unit-testable in isolation) ----------------------

# A traceback line: '  File "/abs/or/rel/path.py", line 42, in func'
_TB_FILE = re.compile(r'File "([^"]+\.py)"')
# `import x` / `from x import ...` (top-level module name only).
_IMPORT = re.compile(r'^\s*(?:from|import)\s+([a-zA-Z_][\w]*)', re.MULTILINE)


def implicated_files(test_output: str, workspace: Path) -> set[str]:
    """Workspace-relative .py files a repair may legitimately edit.

    Two sources, because a plain assertion failure (assertEqual on a wrong
    return value) names ONLY the test file in its traceback, not the module
    under test:
      1. .py files named in failing-test traceback frames (the test modules
         and any code that raised on the way to the failure).
      2. workspace modules IMPORTED by those failing test files (the code the
         test exercises). Without this, fixing the module under test would be
         wrongly flagged as out of scope.
    Editing anything outside this set is collateral."""
    ws = Path(workspace).resolve()
    out: set[str] = set()
    for m in _TB_FILE.finditer(test_output):
        raw = m.group(1)
        p = Path(raw)
        try:
            rp = (p if p.is_absolute() else ws / p).resolve()
            rel = rp.relative_to(ws)
        except (ValueError, OSError):
            continue
        # Ignore stdlib / site-packages frames that happen to live elsewhere.
        out.add(str(rel))

    # Add workspace modules imported by the failing test files.
    for rel in list(out):
        if not is_test_file(rel):
            continue
        try:
            text = (ws / rel).read_text(errors="replace")
        except OSError:
            continue
        for im in _IMPORT.finditer(text):
            mod = im.group(1)
            cand = f"{mod}.py"
            if (ws / cand).is_file():
                out.add(cand)
    return out


def is_test_file(rel_path: str) -> bool:
    name = Path(rel_path).name
    return name.startswith("test_") and name.endswith(".py")


def classify_scope(changed: list[str], implicated: set[str],
                   brownfield: bool) -> dict:
    """Judge a round's changed files against the implicated set.

    Returns {"in_scope": [...], "out_of_scope": [...], "test_edits": [...],
             "violation": bool}.

    out_of_scope = a changed .py file the failing tests did not implicate.
    test_edits   = changed test files that were NOT themselves failing
                   (in brownfield this is always a violation: do not touch a
                   human suite the failures did not flag).
    Non-.py changes are ignored (data fixtures, etc. are not behavior the
    suite gates and this module does not police them)."""
    in_scope, out_of_scope, test_edits = [], [], []
    failing_tests = {f for f in implicated if is_test_file(f)}
    for f in changed:
        if not f.endswith(".py"):
            continue
        if is_test_file(f) and f not in failing_tests:
            test_edits.append(f)
            continue
        if f in implicated:
            in_scope.append(f)
        else:
            out_of_scope.append(f)
    # A new module the repair created (not implicated, not a test) is
    # out_of_scope too; that is intentional, repairs should not invent files.
    violation = bool(out_of_scope) or (brownfield and bool(test_edits))
    return {
        "in_scope": sorted(in_scope),
        "out_of_scope": sorted(out_of_scope),
        "test_edits": sorted(test_edits),
        "violation": violation,
    }
