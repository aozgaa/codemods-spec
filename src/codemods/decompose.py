"""Evaluate decomposition blocks into unit lists (EXAMPLE_SPEC.md §3.2)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .codeowners import owners_to_files
from .config import CodemodConfig, Decomposition


class DecompositionError(Exception):
    pass


def decompose(config: CodemodConfig) -> list[str]:
    """Return the unit list for a codemod, validated for uniqueness."""
    repo_root = Path(config.repo)
    d = config.decomposition
    match d.type:
        case "literal":
            units = list(d.items)
        case "glob":
            units = _glob_units(d, repo_root)
        case "command":
            units = _command_units(d, repo_root)
        case "codeowners":
            units = sorted(owners_to_files(Path(d.path), repo_root))
        case _:  # unreachable after config validation
            raise DecompositionError(f"unknown decomposition type {d.type!r}")

    if not units:
        raise DecompositionError(f"decomposition of {config.name!r} produced no units")
    dupes = {u for u in units if units.count(u) > 1}
    if dupes:
        raise DecompositionError(f"decomposition of {config.name!r} produced duplicate units: {sorted(dupes)}")
    return units


def unit_files(config: CodemodConfig, unit: str) -> list[str] | None:
    """For codeowners decompositions, the files owned by `unit`; else None."""
    d = config.decomposition
    if d.type != "codeowners":
        return None
    return owners_to_files(Path(d.path), Path(config.repo)).get(unit, [])


def _hidden(p: Path, root: Path) -> bool:
    return any(part.startswith(".") for part in p.relative_to(root).parts)


def _glob_units(d: Decomposition, repo_root: Path) -> list[str]:
    matched: set[Path] = set()
    for pattern in d.include:
        # Shell semantics: globs don't match hidden entries (.git!) unless
        # the pattern itself names them.
        explicit_hidden = any(part.startswith(".") for part in Path(pattern).parts)
        matched.update(p for p in repo_root.glob(pattern)
                       if explicit_hidden or not _hidden(p, repo_root))
    for pattern in d.exclude:
        matched.difference_update(repo_root.glob(pattern))
    if d.kind == "directory":
        matched = {p for p in matched if p.is_dir()}
    elif d.kind == "file":
        matched = {p for p in matched if p.is_file()}
    return sorted(str(p.relative_to(repo_root)) for p in matched)


def _command_units(d: Decomposition, repo_root: Path) -> list[str]:
    proc = subprocess.run(
        d.command, shell=True, cwd=repo_root, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise DecompositionError(
            f"decomposition command exited {proc.returncode}: {proc.stderr.strip()}"
        )
    sep = "\0" if d.format == "nul" else "\n"
    return [u.strip() if d.format == "lines" else u
            for u in proc.stdout.split(sep) if u.strip()]
