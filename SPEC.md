# Codemods — Specification

Version 0.3 (draft). The key words MUST, SHOULD, and MAY are to be
interpreted as in RFC 2119.

## Scope

This document describes the structure of a **codemods** system: the
components it consists of, the lifecycle it manages, the guarantees it must
provide, and the points at which it must be adapted to a particular
organization's software-development-lifecycle stack.

It is written for someone implementing the system from scratch. Because the
system integrates with organization-specific infrastructure at nearly every
boundary (version control, code review, notification, machine provisioning,
state storage), a shared off-the-shelf implementation is usually
impractical; the expectation is that each enterprise — sometimes each major
repository — implements its own, against this document.

This repository also contains one complete working implementation (Python,
PostgreSQL, git, GitHub, email), specified prescriptively in
[EXAMPLE_SPEC.md](EXAMPLE_SPEC.md). It exists for guidance: it shows what an
end-to-end conforming system looks like, including a worked state machine,
schema, and driver set. It is an example, not the product.

## 1. Problem

A large mechanical change (a `clang-tidy -fix` sweep, an API rename, a
lint-rule rollout) applied as a single commit in a large organization
produces a review whose required-reviewer set spans every code-owner group
the change touches. Such reviews routinely require sign-off from dozens to
hundreds of reviewers, stall, and go stale against the moving base branch.

A codemods system addresses this by splitting the change into units scoped
to a review boundary (a file, a directory, a build target, a code-owner
group), executing and verifying the change per unit, opening one review per
unit, and tracking each unit's progress in a database over the days or
weeks the campaign takes to land.

## 2. Design constraints

The following constraints shape the structure described in this document.

**The unit of work is the unit of review.** Decomposition happens along
whatever boundary the organization reviews by. All downstream machinery
(execution, verification, review, notification) operates per unit.

**Bespoke logic lives in scripts.** Build systems, test selection, and the
transformations themselves vary per repository and are not modeled by the
system. The transformation (*run*) and verification (*postmod*) steps are
opaque executables supplied by the campaign author under a small fixed
contract (§5). The system schedules them, observes exit codes, and captures
resulting changes.

**State lives in a durable store; processes are disposable.** The
orchestrator is a *reconciler*: each invocation reads the store, advances
what can be advanced, and exits. Termination at any instant MUST NOT lose
or corrupt state. Progress is checkpointed before effects where re-running
is cheap, and after effects where duplication must be avoided. The same
state machine can be driven by a one-shot CLI, cron, or a daemon.

**Steps are idempotent from their checkpoint.** Crash recovery is
re-running the current step, not replaying history. Steps that create
external artifacts (a pushed branch, an open review) MUST detect an
artifact left by an interrupted previous attempt and adopt it rather than
create a duplicate.

**Organization-specific behavior sits behind interfaces.** Each integration
boundary (§6) is a driver with a defined contract. The lifecycle,
guarantees, and operator surface do not depend on which drivers are in use.

**State drifts from reality and must be repairable.** Reviews get closed
out-of-band, checkouts get deleted, owners disappear from ownership files.
The system MUST expose its state for inspection, keep an append-only audit
log, and provide a *doctor* facility that detects drift and repairs it only
on explicit request.

**Campaigns are owned, managed objects.** Run as a service, the system
hosts many concurrent campaigns written by many authors; some will fail
repeatedly, some will open more reviews than the organization can absorb.
Every campaign therefore records an author, can be paused, resumed, and
cancelled as a whole, is subject to rate limits on open reviews, and can be
exercised in a testing stage that cannot interrupt code owners (§4.2, §7).

## 3. Components

| Component | Role |
|---|---|
| **Codemod** | A registered change campaign: configuration + a *run* script + an optional *postmod* script + a decomposition rule. Carries an author, a stage (testing or production), and an operational status (active, paused, cancelled). |
| **Unit** | One item produced by decomposition (a path, a target, an owner). Identified by its literal string value, unique within its codemod. |
| **Subtask** | The durable lifecycle record of one unit within one codemod: state, workspace and review references, attempt count, logs, claim metadata. |
| **Workspace** | An isolated, writable checkout of the target repository in which one subtask's scripts execute (a git clone or worktree, a container, a dev VM). |
| **Driver** | An adapter implementing one integration boundary (§6). |
| **Reconciler** | The engine that advances every non-terminal subtask toward a terminal state, one durable step at a time. |

Flow of one subtask:

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

## 4. Lifecycle requirements

Implementations choose their own state names and storage (EXAMPLE_SPEC.md
§5 and §6 give a worked state machine and schema). Whatever the
representation:

### 4.1 Subtask lifecycle

- **Durability.** Entering and leaving a work-in-flight phase MUST be
  persisted: the store records that a script is about to run before it
  runs, and records the outcome only once its effects (e.g. a commit)
  exist.
- **Mutual exclusion.** Work-in-flight phases are protected by a *claim* —
  an owner identity plus a lease — acquired with an atomic compare-and-swap,
  so concurrent reconcilers sharing one store never double-run a subtask.
- **Crash recovery.** A subtask whose claim has expired (or whose owner is
  provably dead) is recovered automatically: its workspace is discarded and
  it re-enters the last safe phase. Scripts MUST therefore tolerate being
  re-run from a fresh workspace.
- **Empty changes terminate.** A transformation that changes nothing ends
  the subtask as a no-op — no commit, no review.
- **Review outcomes drive terminal states.** Merged ends the subtask
  successfully; closed-without-merge abandons it. Operators can retry
  failures and abandon any subtask; abandoning closes its open review.
- **Notifications fire exactly once per subtask per event**, with delivery
  outcome recorded; failed deliveries are retried on later reconciliations.
- **Audit.** Every state change, repair, and notification is recorded in an
  append-only event log.

### 4.2 Campaign lifecycle

The campaign (codemod) itself has durable state, distinct from its
subtasks:

- **Status.** A campaign is *active*, *paused*, or *cancelled*. The
  reconciler advances subtasks only for active campaigns; pausing freezes a
  campaign wherever it stands (no new claims, no new reviews, no review
  polling) without losing state, and resuming continues from there.
  Cancelling is terminal: every non-terminal subtask is abandoned and its
  open review closed.
- **Review throttling.** A campaign MAY declare a maximum number of
  concurrently open reviews; the reconciler MUST NOT open reviews beyond
  it. Further units progress as earlier reviews merge or close. This bounds
  the blast radius of a campaign that would otherwise flood reviewers.
- **Failure containment.** A campaign MAY declare a failure threshold; when
  the number of failed subtasks reaches it, the system MUST pause the
  campaign automatically, record why, and notify the author. A bad script
  stops itself instead of failing through the whole repository.
- **Stage.** A campaign is in the *testing* stage or the *production*
  stage. The testing stage exists so an author can exercise a campaign
  end-to-end without interrupting code owners (§7, authorship workflow);
  promotion to production discards testing artifacts and starts the
  production campaign cleanly.

## 5. Script contract

The interface between the orchestrator and the organization's bespoke
logic:

- The *run* and *postmod* scripts are executables invoked with **one
  argument — the unit** — and the workspace root as working directory.
- **Exit 0 means success**; anything else fails the subtask.
- Context is passed through environment variables: at minimum the codemod
  name, the unit, the workspace path, and the base revision. A
  codeowners-style decomposition also supplies the unit's file list.
- The *run* script's product is whatever it leaves changed in the
  workspace; the orchestrator commits it. Artifacts that must not land in
  review go in ignored locations.
- *postmod* verifies the committed change — build, tests, and any
  repo-specific test-selection logic. Modifications it leaves (e.g. a
  formatter pass) are folded into the change.
- Script output MUST be captured to per-subtask logs reachable from the
  subtask record.

## 6. Integration boundaries

These are the points at which an implementation binds to its
organization's stack; each is a driver interface. EXAMPLE_SPEC.md §9 gives
concrete signatures.

| Boundary | What varies | What the contract must preserve |
|---|---|---|
| **Repository topology** | monorepo vs. many repos | a codemod targets one repository at one base revision |
| **Version control** | git, svn, fossil, … | isolated workspaces, branch-equivalent publication, empty-change detection, commit-equivalent capture |
| **State store** | PostgreSQL, sqlite, redis, … | atomic compare-and-swap claiming, durable transitions, the audit log |
| **Code review** | GitHub, GitLab, Forgejo, Gerrit, sr.ht, … | open (idempotently), poll state (open/merged/declined), close with reason, enumerate orphaned reviews |
| **Notification** | email, Slack, ticket trackers (Jira/Asana/Linear), … | deliver the per-subtask events; connectors may also drive tickets through their own lifecycle |
| **Workspace provisioning** | local clones, worktree pools, dev VMs, containers | fresh writable checkout of the base revision, disposable at any time, recreated on demand |
| **Decomposition** | globs, explicit lists, command output, codeowners maps, build-graph queries | a deterministic-enough list of unique unit strings, re-evaluable to detect drift |

## 7. Operator surface

An implementation MUST offer, by CLI or UI:

- **register** — submit or update a campaign (into the testing stage when
  requested). New units become subtasks; existing subtasks are never
  disturbed; vanished units are reported and cleaned up only by an explicit
  repair, never as a registration side effect.
- **reconcile** (*sync*) — advance everything advanceable; safe to run at
  any frequency, from anywhere that reaches the store.
- **status** — per-subtask states and per-campaign rollups, human- and
  machine-readable.
- **pause / resume / cancel** — campaign-level controls (§4.2), usable by
  operators and by campaign authors on their own campaigns.
- **promote** — move a campaign from the testing stage to production
  (§4.2).
- **doctor** — detect drift between store and reality (stale claims,
  missing workspaces, vanished units, orphaned reviews, orphaned
  workspaces); repair only with an explicit flag, logging every repair.
- **retry** / **abandon** — operator overrides for individual subtasks.

How author identity maps to permissions (who may pause or cancel whose
campaign) is organization-specific and out of scope; the system MUST record
the author so such policy can be enforced at the service boundary.

### Authorship workflow

A new campaign is wrong more often than it is right — the script breaks on
unconsidered inputs, the decomposition is too coarse, the diff is not what
the author intended. An implementation MUST support a testing workflow in
which a campaign runs end-to-end — decompose, transform, verify, review,
notify — while remaining invisible to code owners. Concretely, in the
testing stage:

- reviews MUST NOT request attention from code owners: marked as drafts,
  opened against a test repository or test review system, or created
  through a hermetic driver, per configuration;
- notifications go to the author only (personal email, DM, …), not to
  owner-facing channels;
- the decomposition MAY be sampled (first N units) to keep iteration fast.

The author iterates — inspect the draft reviews and logs, fix the script,
re-test — and then **promotes**. Promotion abandons the testing subtasks
(closing their reviews), and re-decomposes the campaign cleanly in the
production stage with owner-facing review and notification policies in
effect.

### Execution modes

The same lifecycle logic serves interactive use and service deployment: a
one-shot reconcile command for operators and cron, and a long-running
service that repeats the reconcile (and may add concurrency under the claim
discipline of §4.1). These are thin shells over one shared engine — the
business logic MUST NOT be duplicated per mode. Frequency affects only
latency, never correctness.

## 8. Configuration

A campaign is a declarative document naming the author, the target
repository and base revision, the decomposition rule, the two scripts, the
review and notification policies, the campaign limits (§4.2), and the
testing-stage overrides (§7). The format is an implementation choice (HCL,
TOML, JSON, …) but MUST support nested structure, string lists, comments,
and embedded multi-line commands (decomposition-by-command is a required
capability, §6). Re-registering an updated document updates policy for
*future* steps only; history is never rewound.

## 9. Conformance

An implementation conforms to this specification if it:

1. decomposes campaigns into per-unit subtasks and executes the script
   contract of §5;
2. provides the lifecycle requirements of §4 — subtask and campaign — on
   its chosen store;
3. exposes the operator surface of §7, including campaign management and
   the authorship workflow;
4. isolates the boundaries of §6 behind replaceable interfaces, including
   at least one review driver that requires no external service (so the
   full lifecycle is testable hermetically);
5. drives interactive and service execution modes through one shared
   engine.

## 10. Relationship to the example implementation

[EXAMPLE_SPEC.md](EXAMPLE_SPEC.md) specifies the implementation in this
repository: Python + pixi, PostgreSQL, git worktrees by local clone, GitHub
reviews via `gh`, SMTP email, HCL configuration, a reconciler CLI,
exercised end-to-end against a fork of curl. Consult it for a concrete
state machine, schema, driver signatures, and operator CLI when
implementing your own; none of its technology choices are normative.
