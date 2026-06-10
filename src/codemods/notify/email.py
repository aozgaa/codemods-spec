"""SMTP email notifier (EXAMPLE_SPEC.md §9, §11).

The demo points `smtp` at a local aiosmtpd sink (`pixi run smtp-sink`);
production points it at a real relay. CODEMODS_SMTP overrides the config.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from ..config import NotifyConfig
from .base import NotifyError


class EmailNotifier:
    def send(self, cfg: NotifyConfig, event: str, codemod: str, unit: str,
             subject: str, body: str) -> None:
        if not cfg.to:
            raise NotifyError("email notifier requires 'to' addresses")
        host, _, port = os.environ.get("CODEMODS_SMTP", cfg.smtp).partition(":")
        msg = EmailMessage()
        msg["From"] = cfg.sender
        msg["To"] = ", ".join(cfg.to)
        msg["Subject"] = subject
        msg["X-Codemods-Event"] = event
        msg.set_content(body)
        try:
            with smtplib.SMTP(host, int(port or 25), timeout=10) as smtp:
                smtp.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            raise NotifyError(f"smtp delivery to {host}:{port or 25} failed: {e}") from e
