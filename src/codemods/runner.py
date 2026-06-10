"""Execute run/postmod scripts under the script contract (EXAMPLE_SPEC.md §4)."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import worktree as wt
from .config import CodemodConfig
from .decompose import unit_files


@dataclass
class ScriptResult:
    ok: bool
    returncode: int
    log_path: str


def _env_for(config: CodemodConfig, unit: str, worktree: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        CODEMODS_NAME=config.name,
        CODEMODS_UNIT=unit,
        CODEMODS_WORKTREE=str(worktree),
        CODEMODS_BASE_BRANCH=config.base_branch,
    )
    files = unit_files(config, unit)
    if files is not None:
        f = tempfile.NamedTemporaryFile(
            mode="w", prefix=f"codemods-{config.name}-files-", delete=False)
        f.write("\0".join(files))
        f.close()
        env["CODEMODS_UNIT_FILES"] = f.name
    return env


def run_script(config: CodemodConfig, script: str, unit: str, slug: str,
               worktree: Path, phase: str) -> ScriptResult:
    """Run `script` with argv[1]=unit in the worktree, appending stdout+stderr
    to the subtask log."""
    log = wt.log_path(config, slug)
    log.parent.mkdir(parents=True, exist_ok=True)
    env = _env_for(config, unit, worktree)
    with open(log, "a") as out:
        out.write(f"\n===== {phase}: {script} {unit!r} =====\n")
        out.flush()
        proc = subprocess.run(
            [script, unit], cwd=worktree, env=env, stdout=out, stderr=subprocess.STDOUT,
        )
        out.write(f"===== {phase} exited {proc.returncode} =====\n")
    if files := env.get("CODEMODS_UNIT_FILES"):
        os.unlink(files)
    return ScriptResult(ok=proc.returncode == 0, returncode=proc.returncode,
                        log_path=str(log))
