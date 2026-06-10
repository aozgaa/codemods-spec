"""Subtask state machine (EXAMPLE_SPEC.md §5)."""

from __future__ import annotations

PENDING = "PENDING"
RUNNING = "RUNNING"
MODDED = "MODDED"
VERIFYING = "VERIFYING"
VERIFIED = "VERIFIED"
PR_OPEN = "PR_OPEN"
MERGED = "MERGED"
NOOP = "NOOP"
FAILED = "FAILED"
ABANDONED = "ABANDONED"

ALL_STATES = {PENDING, RUNNING, MODDED, VERIFYING, VERIFIED, PR_OPEN,
              MERGED, NOOP, FAILED, ABANDONED}
TERMINAL = {MERGED, NOOP, ABANDONED}
# In-flight states protected by a claim; mapping to the state a stale claim
# recovers to (EXAMPLE_SPEC.md §5.2).
CLAIMED_RECOVERY = {RUNNING: PENDING, VERIFYING: MODDED}

TRANSITIONS: set[tuple[str, str]] = {
    (PENDING, RUNNING),
    (RUNNING, MODDED),
    (RUNNING, NOOP),
    (RUNNING, FAILED),
    (RUNNING, PENDING),      # crash recovery
    (MODDED, VERIFYING),
    (MODDED, VERIFIED),      # no postmod configured
    (MODDED, PENDING),       # doctor: worktree missing
    (VERIFIED, PENDING),     # doctor: worktree missing
    (VERIFYING, VERIFIED),
    (VERIFYING, FAILED),
    (VERIFYING, MODDED),     # crash recovery
    (VERIFIED, PR_OPEN),
    (PR_OPEN, MERGED),
    (PR_OPEN, ABANDONED),
    (FAILED, PENDING),       # retry
}
# Operator abandon is allowed from any non-terminal state.
TRANSITIONS |= {(s, ABANDONED) for s in ALL_STATES - TERMINAL}


# Codemod-level lifecycle (EXAMPLE_SPEC.md §5.3).
CM_ACTIVE = "active"
CM_PAUSED = "paused"
CM_CANCELLED = "cancelled"
STAGE_TEST = "test"
STAGE_PRODUCTION = "production"


class IllegalTransition(Exception):
    pass


def check_transition(from_state: str, to_state: str) -> None:
    if (from_state, to_state) not in TRANSITIONS:
        raise IllegalTransition(f"{from_state} -> {to_state}")
