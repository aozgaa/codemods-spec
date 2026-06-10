"""Database access: schema, registration, claims, transitions (EXAMPLE_SPEC.md §6)."""

from __future__ import annotations

import json
import os
import socket
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import state as st
from .config import CodemodConfig, slugify

DEFAULT_LEASE_SECONDS = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS codemods (
  id          BIGSERIAL PRIMARY KEY,
  name        TEXT UNIQUE NOT NULL,
  config      JSONB NOT NULL,
  config_path TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subtasks (
  id         BIGSERIAL PRIMARY KEY,
  codemod_id BIGINT NOT NULL REFERENCES codemods(id) ON DELETE CASCADE,
  unit       TEXT NOT NULL,
  unit_slug  TEXT NOT NULL,
  state      TEXT NOT NULL DEFAULT 'PENDING',
  branch     TEXT,
  worktree   TEXT,
  pr_url     TEXT,
  attempts   INT NOT NULL DEFAULT 0,
  last_error TEXT,
  log_path   TEXT,
  claimed_by TEXT,
  claimed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (codemod_id, unit)
);

CREATE TABLE IF NOT EXISTS events (
  id         BIGSERIAL PRIMARY KEY,
  codemod_id BIGINT REFERENCES codemods(id) ON DELETE CASCADE,
  subtask_id BIGINT REFERENCES subtasks(id) ON DELETE CASCADE,
  at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind       TEXT NOT NULL,
  from_state TEXT,
  to_state   TEXT,
  detail     JSONB
);

CREATE TABLE IF NOT EXISTS notifications (
  id         BIGSERIAL PRIMARY KEY,
  subtask_id BIGINT REFERENCES subtasks(id) ON DELETE CASCADE,
  event      TEXT NOT NULL,
  driver     TEXT NOT NULL,
  status     TEXT NOT NULL,
  sent_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  detail     JSONB
);
"""


def default_dsn() -> str:
    return os.environ.get("CODEMODS_DSN", "postgresql://codemods@localhost:5499/codemods")


def connect(dsn: str | None = None) -> psycopg.Connection:
    # autocommit so each db.transition() is durable the moment it returns
    # (EXAMPLE_SPEC.md §5.1); multi-statement atomicity comes from explicit
    # `with conn.transaction()` blocks, which would otherwise silently become
    # savepoints inside psycopg's implicit transaction.
    return psycopg.connect(dsn or default_dsn(), row_factory=dict_row, autocommit=True)


def claim_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def init_db(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(SCHEMA)


def log_event(conn: psycopg.Connection, kind: str, *, codemod_id: int | None = None,
              subtask_id: int | None = None, from_state: str | None = None,
              to_state: str | None = None, detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO events (codemod_id, subtask_id, kind, from_state, to_state, detail)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (codemod_id, subtask_id, kind, from_state, to_state,
         Jsonb(detail) if detail is not None else None),
    )


def register(conn: psycopg.Connection, config: CodemodConfig, config_path: str,
             units: list[str]) -> dict[str, Any]:
    """Upsert codemod + insert subtasks for new units (EXAMPLE_SPEC.md §7, §8).

    Returns {"codemod_id", "new", "existing", "vanished"}.
    """
    with conn.transaction():
        row = conn.execute(
            """INSERT INTO codemods (name, config, config_path)
               VALUES (%s, %s, %s)
               ON CONFLICT (name) DO UPDATE
                 SET config = EXCLUDED.config,
                     config_path = EXCLUDED.config_path,
                     updated_at = now()
               RETURNING id""",
            (config.name, Jsonb(config.to_dict()), config_path),
        ).fetchone()
        codemod_id = row["id"]

        existing = {
            r["unit"]: r["unit_slug"]
            for r in conn.execute(
                "SELECT unit, unit_slug FROM subtasks WHERE codemod_id = %s",
                (codemod_id,),
            )
        }
        taken = set(existing.values())
        new_units = [u for u in units if u not in existing]
        for unit in new_units:
            slug = slugify(unit, taken)
            row = conn.execute(
                "INSERT INTO subtasks (codemod_id, unit, unit_slug) VALUES (%s, %s, %s)"
                " RETURNING id",
                (codemod_id, unit, slug),
            ).fetchone()
            log_event(conn, "register", codemod_id=codemod_id, subtask_id=row["id"],
                      to_state=st.PENDING, detail={"unit": unit})
        vanished = sorted(set(existing) - set(units))
        return {
            "codemod_id": codemod_id,
            "new": new_units,
            "existing": len(existing),
            "vanished": vanished,
        }


def get_codemod(conn: psycopg.Connection, name: str) -> dict | None:
    return conn.execute("SELECT * FROM codemods WHERE name = %s", (name,)).fetchone()


def list_codemods(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM codemods ORDER BY name").fetchall()


def config_of(row: dict) -> CodemodConfig:
    cfg = row["config"]
    return CodemodConfig.from_dict(cfg if isinstance(cfg, dict) else json.loads(cfg))


def get_subtask(conn: psycopg.Connection, codemod_id: int, unit: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM subtasks WHERE codemod_id = %s AND unit = %s",
        (codemod_id, unit),
    ).fetchone()


def list_subtasks(conn: psycopg.Connection, codemod_id: int | None = None,
                  states: list[str] | None = None) -> list[dict]:
    q = "SELECT s.*, c.name AS codemod_name FROM subtasks s JOIN codemods c ON c.id = s.codemod_id"
    clauses, params = [], []
    if codemod_id is not None:
        clauses.append("s.codemod_id = %s")
        params.append(codemod_id)
    if states:
        clauses.append("s.state = ANY(%s)")
        params.append(states)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY c.name, s.id"
    return conn.execute(q, params).fetchall()


def transition(conn: psycopg.Connection, subtask: dict, to_state: str, *,
               claim: bool = False, lease_seconds: int = DEFAULT_LEASE_SECONDS,
               error: str | None = None, detail: dict | None = None,
               **fields: Any) -> bool:
    """Atomically move `subtask` from its observed state to `to_state`.

    Compare-and-swap on (id, state); also requires the claim to be free or
    expired when claiming, and clears the claim when entering an unclaimed
    state. Returns False if another reconciler won the race.
    Extra `fields` (branch, worktree, pr_url, log_path) are persisted.
    """
    from_state = subtask["state"]
    st.check_transition(from_state, to_state)

    sets = ["state = %s", "updated_at = now()"]
    params: list[Any] = [to_state]
    if claim:
        sets += ["claimed_by = %s", "claimed_at = now()"]
        params.append(claim_owner())
    elif to_state not in st.CLAIMED_RECOVERY:  # leaving in-flight: release claim
        sets += ["claimed_by = NULL", "claimed_at = NULL"]
    sets.append("last_error = %s")
    params.append(error)
    for col in ("branch", "worktree", "pr_url", "log_path", "attempts"):
        if col in fields:
            sets.append(f"{col} = %s")
            params.append(fields[col])

    guard = "id = %s AND state = %s"
    params += [subtask["id"], from_state]
    if claim:
        guard += (" AND (claimed_by IS NULL OR claimed_by = %s"
                  " OR claimed_at + make_interval(secs => %s) < now())")
        params += [claim_owner(), lease_seconds]

    with conn.transaction():
        cur = conn.execute(f"UPDATE subtasks SET {', '.join(sets)} WHERE {guard}", params)
        if cur.rowcount != 1:
            return False
        log_event(conn, "state_change", codemod_id=subtask["codemod_id"],
                  subtask_id=subtask["id"], from_state=from_state, to_state=to_state,
                  detail=detail)
    subtask["state"] = to_state
    for col, val in fields.items():
        subtask[col] = val
    return True


def stale_claims(conn: psycopg.Connection,
                 lease_seconds: int = DEFAULT_LEASE_SECONDS) -> list[dict]:
    """In-flight subtasks whose claim has expired, or whose owner is a dead
    process on this host (cheap liveness check for the single-host sample)."""
    rows = conn.execute(
        "SELECT s.*, c.name AS codemod_name FROM subtasks s"
        " JOIN codemods c ON c.id = s.codemod_id"
        " WHERE s.state = ANY(%s)", (list(st.CLAIMED_RECOVERY),),
    ).fetchall()
    out = []
    host = socket.gethostname()
    for r in rows:
        expired = conn.execute(
            "SELECT %s::timestamptz + make_interval(secs => %s) < now() AS e",
            (r["claimed_at"], lease_seconds),
        ).fetchone()["e"] if r["claimed_at"] else True
        if not expired and r["claimed_by"] and r["claimed_by"].startswith(f"{host}:"):
            pid = int(r["claimed_by"].rsplit(":", 1)[1])
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                expired = True
            except PermissionError:
                pass
        if expired:
            out.append(r)
    return out


def record_notification(conn: psycopg.Connection, subtask_id: int, event: str,
                        driver: str, status: str, detail: dict | None = None) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO notifications (subtask_id, event, driver, status, detail)"
            " VALUES (%s, %s, %s, %s, %s)",
            (subtask_id, event, driver, status, Jsonb(detail) if detail else None),
        )


def notified(conn: psycopg.Connection, subtask_id: int, event: str) -> bool:
    """Has a successful notification for (subtask, event) already been sent?"""
    return conn.execute(
        "SELECT 1 FROM notifications WHERE subtask_id = %s AND event = %s AND status = 'sent'",
        (subtask_id, event),
    ).fetchone() is not None
