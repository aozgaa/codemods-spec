"""File-backed fake review driver.

Lets the entire state machine run without a review system: PRs are rows in
a JSON file. Select it with `driver = "fake"` in the review block; `repo`
is the path of the JSON state file. Tests (or a human, for a dry run) flip
PR states with `merge()` / `close()` or `python -m codemods.review.fake`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from ..config import ReviewConfig
from .base import ReviewError


def _load(path: Path) -> dict[str, dict]:
    return json.loads(path.read_text()) if path.exists() else {}


def _save(path: Path, prs: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prs, indent=2) + "\n")


class FakeReviewDriver:
    def _path(self, cfg: ReviewConfig) -> Path:
        if not cfg.repo:
            raise ReviewError("fake review driver needs 'repo' = path of its state file")
        return Path(cfg.repo)

    def open(self, cfg: ReviewConfig, branch: str, base_branch: str,
             title: str, body: str) -> str:
        path = self._path(cfg)
        prs = _load(path)
        for url, pr in prs.items():
            if pr["branch"] == branch and pr["state"] == "open":
                return url
        url = f"fake://pr/{len(prs) + 1}"
        prs[url] = {"branch": branch, "base": base_branch, "title": title,
                    "body": body, "state": "open", "draft": cfg.draft}
        _save(path, prs)
        return url

    def state(self, cfg: ReviewConfig, pr_url: str) -> Literal["open", "merged", "closed"]:
        pr = _load(self._path(cfg)).get(pr_url)
        if pr is None:
            raise ReviewError(f"unknown fake PR {pr_url}")
        return pr["state"]

    def close(self, cfg: ReviewConfig, pr_url: str, comment: str) -> None:
        set_state(self._path(cfg), pr_url, "closed")

    def find_orphans(self, cfg: ReviewConfig, branch_prefix: str) -> list[tuple[str, str]]:
        return [(pr["branch"], url) for url, pr in _load(self._path(cfg)).items()
                if pr["state"] == "open" and pr["branch"].startswith(branch_prefix + "/")]


def set_state(path: Path | str, pr_url: str, state: str) -> None:
    """Test/operator helper: simulate the review tool merging or closing."""
    path = Path(path)
    prs = _load(path)
    if pr_url not in prs:
        raise ReviewError(f"unknown fake PR {pr_url}")
    prs[pr_url]["state"] = state
    _save(path, prs)


def merge(path: Path | str, pr_url: str) -> None:
    set_state(path, pr_url, "merged")


def close(path: Path | str, pr_url: str) -> None:
    set_state(path, pr_url, "closed")


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 4 or sys.argv[2] not in ("merge", "close"):
        sys.exit("usage: python -m codemods.review.fake <state.json> merge|close <pr-url>")
    set_state(sys.argv[1], sys.argv[3], {"merge": "merged", "close": "closed"}[sys.argv[2]])
