"""GitHub review driver using the `gh` CLI (EXAMPLE_SPEC.md §9, §11).

Requires a logged-in `gh` (run `gh auth login` once).
"""

from __future__ import annotations

import json
import subprocess
from typing import Literal

from ..config import ReviewConfig
from .base import ReviewError


def _gh(*args: str) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise ReviewError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


class GithubReviewDriver:
    def open(self, cfg: ReviewConfig, branch: str, base_branch: str,
             title: str, body: str) -> str:
        existing = json.loads(_gh(
            "pr", "list", "--repo", cfg.repo, "--head", branch,
            "--state", "open", "--json", "url"))
        if existing:
            return existing[0]["url"]
        args = ["pr", "create", "--repo", cfg.repo, "--head", branch,
                "--base", base_branch, "--title", title, "--body", body]
        if cfg.draft:
            args.append("--draft")
        for r in cfg.reviewers:
            args += ["--reviewer", r]
        return _gh(*args).strip().splitlines()[-1]

    def state(self, cfg: ReviewConfig, pr_url: str) -> Literal["open", "merged", "closed"]:
        info = json.loads(_gh("pr", "view", pr_url, "--json", "state"))
        return {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}[info["state"]]

    def close(self, cfg: ReviewConfig, pr_url: str, comment: str) -> None:
        if self.state(cfg, pr_url) == "open":
            _gh("pr", "close", pr_url, "--comment", comment)

    def find_orphans(self, cfg: ReviewConfig, branch_prefix: str) -> list[tuple[str, str]]:
        prs = json.loads(_gh(
            "pr", "list", "--repo", cfg.repo, "--state", "open",
            "--json", "url,headRefName"))
        return [(p["headRefName"], p["url"]) for p in prs
                if p["headRefName"].startswith(branch_prefix + "/")]
