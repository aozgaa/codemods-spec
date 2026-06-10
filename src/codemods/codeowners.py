"""GitHub-format CODEOWNERS parsing (EXAMPLE_SPEC.md §3.2, type "codeowners").

Maps each repository file to its owners using last-match-wins semantics,
then inverts to {owner: [files]}.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def parse_rules(text: str) -> list[tuple[str, list[str]]]:
    """Return [(pattern, owners)] in file order; later rules take precedence."""
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern, owners = parts[0], [p for p in parts[1:] if not p.startswith("#")]
        rules.append((pattern, owners))
    return rules


def _pattern_to_regex(pattern: str) -> re.Pattern:
    """Translate a CODEOWNERS pattern to a regex over repo-relative paths."""
    anchored = pattern.startswith("/")
    pat = pattern.lstrip("/")
    dir_only = pat.endswith("/")
    pat = pat.rstrip("/")

    out = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i : i + 2] == "**":
                out.append(".*")
                i += 2
                if i < len(pat) and pat[i] == "/":
                    i += 1
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    body = "".join(out)

    prefix = "" if anchored else "(?:.*/)?"
    if dir_only:
        suffix = "/.*"  # "docs/" owns contents, not a file literally named docs
    elif "*" not in pat and "?" not in pat:
        suffix = "(?:/.*)?"  # bare "docs" owns the path itself or anything beneath
    else:
        suffix = ""
    return re.compile(f"^{prefix}{body}{suffix}$")


def owners_to_files(codeowners_path: Path, repo_root: Path) -> dict[str, list[str]]:
    """Invert CODEOWNERS over the repo's tracked files: {owner: sorted files}."""
    rules = parse_rules(codeowners_path.read_text())
    compiled = [(_pattern_to_regex(p), owners) for p, owners in rules]

    files = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo_root, check=True,
        capture_output=True, text=True,
    ).stdout.split("\0")

    result: dict[str, list[str]] = {}
    for f in files:
        if not f:
            continue
        owners: list[str] = []
        for regex, rule_owners in compiled:  # last match wins
            if regex.match(f):
                owners = rule_owners
        for o in owners:
            result.setdefault(o, []).append(f)
    return {o: sorted(fs) for o, fs in result.items()}
