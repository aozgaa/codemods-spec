"""Review driver interface (EXAMPLE_SPEC.md §9)."""

from __future__ import annotations

from typing import Literal, Protocol

from ..config import ReviewConfig


class ReviewError(Exception):
    pass


class ReviewDriver(Protocol):
    def open(self, cfg: ReviewConfig, branch: str, base_branch: str,
             title: str, body: str) -> str:
        """Open a review for the already-pushed `branch`; return its URL.
        MUST be idempotent: if an open review for `branch` exists, return it."""
        ...

    def state(self, cfg: ReviewConfig, pr_url: str) -> Literal["open", "merged", "closed"]: ...

    def close(self, cfg: ReviewConfig, pr_url: str, comment: str) -> None: ...

    def find_orphans(self, cfg: ReviewConfig, branch_prefix: str) -> list[tuple[str, str]]:
        """(branch, pr_url) pairs of open reviews under `branch_prefix`."""
        ...


def get_review_driver(name: str) -> ReviewDriver:
    if name == "github":
        from .github import GithubReviewDriver

        return GithubReviewDriver()
    if name == "fake":
        from .fake import FakeReviewDriver

        return FakeReviewDriver()
    raise ReviewError(f"unknown review driver {name!r}")
