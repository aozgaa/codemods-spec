"""Notifier interface (EXAMPLE_SPEC.md §9)."""

from __future__ import annotations

from typing import Protocol

from ..config import NotifyConfig


class NotifyError(Exception):
    pass


class Notifier(Protocol):
    def send(self, cfg: NotifyConfig, event: str, codemod: str, unit: str,
             subject: str, body: str) -> None: ...


def get_notifier(name: str) -> Notifier:
    if name == "email":
        from .email import EmailNotifier

        return EmailNotifier()
    raise NotifyError(f"unknown notify driver {name!r}")
