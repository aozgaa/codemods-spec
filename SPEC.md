# Codemods — Specification

Version 0.2 (draft). The key words MUST, SHOULD, and MAY are to be
interpreted as in RFC 2119.

This document describes the philosophy and architecture of **codemods**: a
system for landing large mechanical changes in large organizations by
splitting them into independently reviewable units. It deliberately avoids
prescribing technologies. A companion document,
[EXAMPLE_SPEC.md](EXAMPLE_SPEC.md), makes every choice concrete for the
reference implementation that ships in this repository; treat this document
as the contract an implementation must honor, and EXAMPLE_SPEC.md as one
fully worked instantiation of it.

## 1. Motivation

Large mechanical changes — a `clang-tidy -fix` sweep, an API rename, a
header reshuffle, a lint-rule rollout — are easy to *generate* and hard to
*land*. Applied as one commit in an organization with 1000+ engineers, the
review fans out to every code-owner group the change touches; a single
change can demand sign-off from 100+ reviewers, stall indefinitely, and rot
against a moving main branch.

The fix is social as much as technical: deliver each owner a review scoped
to what they own, small enough to be read, while a machine tracks the
long tail — the run that failed, the review nobody answered, the directory
that vanished in a refactor — for weeks if necessary. Codemods is that
machine.

## 2. Philosophy

**The unit of work is the unit of review.** A campaign is decomposed by
whatever boundary the organization reviews along — a file, a directory, a
build target, a code-owner group. Everything downstream (execution,
verification, review, notification) happens per unit, so no reviewer ever
faces more than their own slice.

**Bespoke logic lives in scripts; orchestration lives in the system.** Every
organization builds, tests, and selects tests differently. Codemods does not
model any of that: the transformation (*run*) and verification (*postmod*)
are opaque executables supplied by the campaign author, with a deliberately
tiny contract (§5). The orchestrator only knows how to schedule them,
observe their exit codes, and capture what they changed.

**All state lives in a durable store; processes are disposable.** The
orchestrator is a *reconciler*: each invocation reads the store, advances
whatever can be advanced, and exits. Killing it at any instant MUST NOT lose
or corrupt state — progress checkpoints are written *before* effects where a
re-run is cheap, and *after* effects where a re-run must be avoided.
Long-lived daemons, cron jobs, and one-shot CLI runs are all valid drivers
of the same state machine.

**Every step is idempotent from its checkpoint.** Recovery from a crash is
re-running the step, never replaying history. Steps that create external
artifacts (a pushed branch, an open review) MUST detect an artifact created
by a previous, interrupted attempt and adopt it rather than duplicate it.

**Adaptation happens at named seams, not by forking the orchestrator.**
Everything organization-specific — version control, review tooling,
notification channels, machine provisioning, the state store itself — sits
behind a driver interface (§6). A conforming implementation swaps drivers;
the lifecycle, guarantees, and operator surface stay recognizably the same.

**Operators see and repair everything.** Reality drifts from the database:
reviews get closed by hand, checkouts get deleted, owners disappear from
CODEOWNERS. The system MUST expose its full state for inspection, record an
append-only audit trail, and ship a *doctor* that detects drift and — only
when asked — repairs it.

## 3. System model

| Concept | Definition |
|---|---|
| **Codemod** | A named, registered change campaign: configuration + a *run* script + an optional *postmod* script + a decomposition rule. |
| **Unit** | One item produced by decomposition (a path, a target, an owner). Identified by its literal string value, unique within its codemod. |
| **Subtask** | The durable lifecycle record of one unit within one codemod: state, workspace and review references, attempt count, logs, claim metadata. |
| **Workspace** | An isolated, writable checkout of the target repository in which one subtask's scripts execute (a "worktree" in the git sense, a VM or container in others). |
| **Driver** | A pluggable adapter for one organization-specific seam (§6). |
| **Reconciler** | The engine that advances every non-terminal subtask toward a terminal state, one observable, durable step at a time. |

The flow of one subtask:

```
decompose ─▶ pending ─▶ transform (run script in fresh workspace)
                           │  no change ─▶ done (noop)
                           ▼
                        verify (postmod script)
                           ▼
                        publish branch + open review
                           ▼
            poll review ─▶ merged ▷ done   /   declined ▷ abandoned
        (failures at any step are recorded, notified, and retryable)
```

## 4. Lifecycle guarantees

Implementations choose their own state names and storage (EXAMPLE_SPEC.md §5
and §6 give a complete worked state machine and schema); whatever the
representation, the following are normative:

- **Durability.** Entering and leaving a work-in-flight phase MUST be
  persisted: the store records that a script is about to run before it
  runs, and records the outcome only once its effects (e.g. a commit)
  exist.
- **Mutual exclusion.** Work-in-flight phases are protected by a *claim* —
  an owner identity plus a lease — acquired with an atomic compare-and-swap,
  so concurrent reconcilers sharing one store never double-run a subtask.
- **Crash recovery.** A subtask whose claim expired (or whose owner is
  provably dead) is recovered automatically: its workspace is discarded and
  it re-enters the last safe phase. Scripts MUST therefore tolerate being
  re-run from a fresh workspace.
- **Empty changes are first-class.** A transformation that changes nothing
  terminates the subtask as a no-op — no commit, no review, no reviewer
  interruption.
- **Review outcomes drive terminal states.** Merged ends the subtask
  successfully; closed-without-merge abandons it. Operators can retry
  failures and abandon anything, and abandoning closes any open review.
- **Notifications fire exactly once per subtask per event**, with delivery
  outcome recorded; failed deliveries are retried on later reconciliations.
- **Audit.** Every state change, repair, and notification is recorded in an
  append-only event log.

## 5. The script contract

This is the universal interface between the orchestrator and the
organization's bespoke logic, and it is intentionally minimal:

- The *run* and *postmod* scripts are executables invoked with **one
  argument — the unit** — and the workspace root as working directory.
- **Exit 0 means success**; anything else fails the subtask.
- The orchestrator passes context through environment variables (at minimum
  the codemod name, the unit, the workspace path, and the base revision; a
  codeowners-style decomposition also supplies the unit's file list).
- The *run* script's product is whatever it leaves changed in the
  workspace; the orchestrator commits it. Artifacts that must not land in
  review go in ignored locations.
- *postmod* verifies the committed change — build, tests, whatever
  repo-specific selection logic the organization encodes. Modifications it
  leaves (e.g. a formatter pass) are folded into the change.
- Script output MUST be captured to per-subtask logs reachable from the
  subtask record.

## 6. Adaptation points

These are the seams along which organizations differ; each is a driver
interface in a conforming implementation. EXAMPLE_SPEC.md §9 gives concrete
signatures.

| Seam | What varies | What the contract must preserve |
|---|---|---|
| **Repository topology** | monorepo vs. many repos | a codemod targets one repository at one base revision |
| **Version control** | git, svn, fossil, … | isolated workspaces, branch-equivalent publication, empty-change detection, commit-equivalent capture |
| **State store** | PostgreSQL, sqlite, redis, … | atomic compare-and-swap claiming, durable transitions, the audit log |
| **Code review** | GitHub, GitLab, Forgejo, Gerrit, sr.ht, … | open (idempotently), poll state (open/merged/declined), close with reason, enumerate orphaned reviews |
| **Notification** | email, Slack, ticket trackers (Jira/Asana/Linear), … | deliver the per-subtask events; connectors may also drive tickets through their own lifecycle |
| **Workspace provisioning** | local clones, worktree pools, dev VMs, containers | fresh writable checkout of the base revision, disposable at any time, recreated on demand |
| **Decomposition** | globs, explicit lists, command output, codeowners maps, build-graph queries | a deterministic-enough list of unique unit strings, re-evaluable to detect drift |

## 7. Operator capabilities

A conforming implementation MUST offer, by CLI or UI:

- **register** — submit or update a campaign; new units become subtasks,
  existing subtasks are never disturbed, vanished units are reported (and
  cleaned up only by an explicit repair, never as a registration side
  effect).
- **reconcile** (*sync*) — advance everything advanceable; safe to run at
  any frequency, from anywhere that reaches the store.
- **status** — per-subtask states and per-campaign rollups, human- and
  machine-readable.
- **doctor** — detect drift between store and reality (stale claims, missing
  workspaces, vanished units, orphaned reviews, orphaned workspaces);
  repair only with an explicit flag, logging every repair.
- **retry** / **abandon** — operator overrides for individual subtasks.

## 8. Configuration

A campaign is a declarative document naming the target repository and base
revision, the decomposition rule, the two scripts, and the review and
notification policies. The format is an implementation choice (HCL, TOML,
JSON, …) but MUST support nested structure, string lists, comments, and
embedded multi-line commands — decomposition-by-command is part of the
philosophy (bespoke logic in scripts), not an extension. Re-registering an
updated document updates policy for *future* steps only; history is never
rewound.

## 9. Conformance

An implementation conforms to this specification if it:

1. decomposes campaigns into per-unit subtasks and executes the script
   contract of §5;
2. provides the lifecycle guarantees of §4 on its chosen store;
3. exposes the operator capabilities of §7;
4. isolates the seams of §6 behind replaceable interfaces, including at
   least one review driver that requires no external service (so the full
   lifecycle is testable hermetically).

## 10. Reference implementation

This repository contains a complete instantiation: Python + pixi,
PostgreSQL, git worktrees by local clone, GitHub reviews via `gh`, SMTP
email, HCL configuration, exercised end-to-end against a fork of curl.
[EXAMPLE_SPEC.md](EXAMPLE_SPEC.md) specifies it prescriptively — exact
config schema, state machine, SQL schema, driver signatures, CLI — and maps
each section to the code in `src/codemods/`.
