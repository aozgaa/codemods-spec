"""Clone-based worktree provider (EXAMPLE_SPEC.md §9).

Worktrees live at <workdir>/worktrees/<codemod>-<slug>; per-subtask logs at
<workdir>/logs/<codemod>-<slug>.log.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import CodemodConfig


class WorktreeError(Exception):
    pass


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc.stdout


def worktree_path(config: CodemodConfig, slug: str) -> Path:
    return Path(config.workdir) / "worktrees" / f"{config.name}-{slug}"


def log_path(config: CodemodConfig, slug: str) -> Path:
    return Path(config.workdir) / "logs" / f"{config.name}-{slug}.log"


def prepare(config: CodemodConfig, slug: str) -> Path:
    """Produce a fresh checkout of base_branch with the subtask branch checked
    out. Any half-finished previous worktree is discarded (EXAMPLE_SPEC.md §5.2)."""
    path = worktree_path(config, slug)
    discard(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", "--branch", config.base_branch, config.repo, str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise WorktreeError(f"clone of {config.repo} failed: {proc.stderr.strip()}")
    _git(path, "switch", "-c", config.branch_for(slug))
    return path


def discard(path: Path | str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def has_changes(path: Path) -> bool:
    """Any non-ignored modification or untracked file (EXAMPLE_SPEC.md §4)."""
    return bool(_git(path, "status", "--porcelain").strip())


def commit_all(path: Path, message: str, amend: bool = False) -> str:
    """Commit (or amend) every non-ignored change; return the commit sha."""
    _git(path, "add", "-A")
    args = ["commit", "-q", "-m", message] + (["--amend", "--no-edit"] if amend else [])
    _git(path, *args)
    return _git(path, "rev-parse", "HEAD").strip()


def head_is_base(path: Path, base_branch: str) -> bool:
    """True until the subtask commit has been created."""
    head = _git(path, "rev-parse", "HEAD").strip()
    base = _git(path, "rev-parse", f"origin/{base_branch}").strip()
    return head == base


def push(path: Path, push_url: str, branch: str) -> None:
    _git(path, "push", "--force-with-lease", push_url, f"{branch}:{branch}")
