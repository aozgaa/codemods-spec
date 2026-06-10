# Codemods — Example Specification (Reference Implementation)

Version 0.2 (draft). The key words MUST, MUST NOT, SHOULD, and MAY are to be
interpreted as in RFC 2119.

This document is the prescriptive specification of the **reference
implementation** in this repository: one concrete instantiation of the
general [SPEC.md](SPEC.md). Where SPEC.md says "an implementation chooses",
this document says exactly what *this* implementation chose:

| Adaptation point (SPEC.md §6) | Choice here |
|---|---|
| Repository topology | one git repository per codemod |
| Version control | git |
| State store | PostgreSQL |
| Code review | GitHub pull requests via the `gh` CLI (plus a hermetic file-backed fake) |
| Notification | email over SMTP |
| Workspace provisioning | local `git clone` per subtask |
| Decomposition | `literal`, `glob`, `command`, `codeowners` |
| Configuration format | HCL |
| Execution mode | reconciler CLI |

An organization adapting codemods to a different stack should read SPEC.md
for the contract and treat this document the way one treats a working
example: steal the parts that fit, replace the drivers that don't.

## 1. Overview

The implementation splits a repository-wide change into per-unit subtasks,
runs the transformation per unit in an isolated clone, verifies each result,
opens one GitHub PR per unit, and tracks every subtask through its full
lifecycle in PostgreSQL. The motivation, philosophy, and normative
guarantees live in SPEC.md; everything below is the concrete design — the
config schema, the state machine, the SQL schema, the driver signatures, and
the CLI.

## 2. Concepts

| Term | Definition |
|---|---|
| **Codemod** | A named, registered change campaign: configuration + a *run* script + an optional *postmod* script + a decomposition rule. |
| **Unit** | One item produced by decomposition (a path, a target name, an owner). Identified by its literal string value. |
| **Subtask** | The lifecycle record of one unit within one codemod: a database row carrying state, branch, review URL, logs, and claim metadata. |
| **Worktree** | An isolated checkout of the target repository in which one subtask's scripts execute. |
| **Driver** | A pluggable adapter: review driver (open/poll/close reviews), notify driver (deliver messages), decomposer (produce units), worktree provider (produce checkouts). |
| **Reconciler** | The engine that advances every non-terminal subtask toward a terminal state. Each invocation observes current state and performs the next step; it never relies on in-memory state surviving between invocations. |

## 3. Configuration

A codemod is defined in an [HCL](https://github.com/hashicorp/hcl) file. HCL
was chosen for its regular block syntax, native lists/maps, heredocs for
embedded shell, and comments (the general spec leaves the format open,
SPEC.md §8).

### 3.1 Schema

```hcl
codemod "clang-tidy-fix" {
  description = "Apply clang-tidy fixes, one src subdirectory at a time"

  # Where worktrees are cloned from. Local path or URL, anything the
  # implementation's SCM tooling can clone.
  repo        = "/path/to/public-repo"
  base_branch = "main"

  # Branches are named <branch_prefix>/<codemod-name>/<unit-slug>.
  branch_prefix = "codemods"

  # Where worktrees and per-subtask logs are created.
  # Relative paths resolve against the config file's directory.
  workdir = "./work"

  # Example of one way to fan out work. Other possible mechanisms
  # are described below
  decomposition {
    type    = "glob"
    # match each subdir under src/ into a separate unit
    include = ["src/*"]
    kind    = "directory"
  }

  # Transformation script. argv[1] = unit. Exit 0 = success.
  run = "./mods/clang-tidy-fix.sh"

  # Optional verification script (build, tests). Same contract.
  postmod = "./mods/build-and-test.sh"

  review {
    driver    = "github"
    repo      = "example/public-repo"                 # driver-specific target
    push_url  = "git@github.com:example/public-repo.git" # where branches are pushed
    title     = "[codemods] {codemod}: {unit}"
    body      = "Automated change `{codemod}` applied to `{unit}`."
    reviewers = []          # driver-specific reviewer handles
    draft     = false
  }

  notify {
    driver = "email"
    to     = ["owner@example.com"]
    from   = "codemods@example.com"
    smtp   = "localhost:8025"        # driver-specific
    on     = ["failed", "pr_open", "merged", "abandoned"]
  }
}
```

Top-level keys `repo`, `base_branch`, `run`, and one `decomposition` block are
REQUIRED. `branch_prefix` defaults to `codemods`; `workdir` defaults to
`./work`; `postmod`, `review`, and `notify` are OPTIONAL (a codemod without a
`review` block stops at VERIFIED — useful for dry runs).

Relative paths (`run`, `postmod`, `workdir`, codeowners `path`) resolve
against the directory containing the config file.

`title` and `body` are templates; implementations MUST substitute `{codemod}`
and `{unit}`.

### 3.2 Decomposition types

Exactly one `decomposition` block per codemod. Decomposition is evaluated at
**registration time** against the content of `repo` (implementations SHOULD
evaluate against a clean checkout of `base_branch`). Re-running `register`
re-evaluates; see §8 for drift handling.

**`literal`** — explicit list.

```hcl
decomposition {
  type  = "literal"
  items = ["src/app", "src/lib", "src/tools"]
}
```

**`glob`** — shell-style globs relative to the repository root. `kind`
restricts matches to `"directory"`, `"file"`, or `"any"` (default). `exclude`
globs are removed from the result.

```hcl
decomposition {
  type    = "glob"
  include = ["src/*"]
  exclude = ["src/meta"]
  kind    = "directory"
}
```

**`command`** — arbitrary command executed with the repository root as
working directory; its stdout is split into units. `format` is `"lines"`
(default, one unit per non-empty line) or `"nul"` (NUL-delimited, for
filenames containing newlines).

```hcl
decomposition {
  type    = "command"
  command = "find src -maxdepth 1 -mindepth 1 -type d -print0"
  format  = "nul"
}
```

**`codeowners`** — parse a GitHub-format CODEOWNERS file; each distinct owner
becomes one unit (the owner string, e.g. `@org/payments-team`). The set of
repository files owned by that owner (last-match-wins semantics, as GitHub
evaluates them) is passed to the scripts via `CODEMODS_UNIT_FILES` (§4).

```hcl
decomposition {
  type = "codeowners"
  path = ".github/CODEOWNERS"
}
```

Unit strings MUST be unique within a codemod. Implementations MUST reject a
decomposition that yields duplicates, and SHOULD reject one that yields zero
units.

### 3.3 Unit slugs

Each unit gets a *slug* used in branch and directory names: the unit string
lowercased, with every character outside `[a-z0-9._-]` replaced by `-`,
collapsed runs of `-`, trimmed to 60 characters. If two units in one codemod
slug identically, a numeric suffix (`-2`, `-3`, …) disambiguates
deterministically by registration order.

## 4. Script contract

Both `run` and `postmod` are executables (typically shell scripts) invoked
as:

- **argv[1]** — the unit string.
- **cwd** — the worktree root.
- **environment** — inherited, plus:
  - `CODEMODS_NAME` — codemod name
  - `CODEMODS_UNIT` — unit string (same as argv[1])
  - `CODEMODS_WORKTREE` — absolute worktree path
  - `CODEMODS_BASE_BRANCH` — base branch name
  - `CODEMODS_UNIT_FILES` — (codeowners decomposition only) path to a
    NUL-delimited file listing the unit's owned files

Exit status 0 means success; anything else means failure and moves the
subtask to FAILED. stdout and stderr MUST be captured to a per-subtask log
file recorded in the database.

After a successful `run`, the orchestrator inspects the worktree:

- **No change** to tracked or untracked-and-unignored files → the subtask is
  **NOOP** (terminal). No commit, no review.
- **Changes present** → the orchestrator commits all non-ignored changes on
  the subtask branch. Scripts MUST confine build artifacts to gitignored
  locations; anything not ignored is considered part of the change.

`postmod` verifies the committed change (configure, build, run tests —
including any bespoke repo-specific test-selection logic). If `postmod`
leaves additional non-ignored modifications (e.g. a formatter pass), the
orchestrator amends them into the subtask commit.

Scripts MUST be safe to re-run from a fresh checkout: on crash recovery the
orchestrator discards the worktree and re-executes from scratch (§5.2).

## 5. Subtask lifecycle

### 5.1 States and transitions

```
                        ┌────────── retry ───────────┐
                        ▼                            │
PENDING ──claim──▶ RUNNING ──ok+diff──▶ MODDED ──claim──▶ VERIFYING ──ok──▶ VERIFIED
                      │   └─ok+empty─▶ NOOP*              │                    │
                      └──fail──▶ FAILED ◀───── fail ──────┘                 open PR
                                                                               ▼
                MERGED* ◀──merged── PR_OPEN ──closed unmerged──▶ ABANDONED*
```

`*` = terminal. Full transition table (normative):

| From | To | Trigger |
|---|---|---|
| PENDING | RUNNING | reconciler claims subtask; worktree prepared; `run` starts |
| RUNNING | MODDED | `run` exited 0, diff non-empty, commit created |
| RUNNING | NOOP | `run` exited 0, no changes |
| RUNNING | FAILED | `run` exited non-zero, or worktree/setup error |
| RUNNING | PENDING | crash recovery: claim expired (§5.2) |
| MODDED | VERIFYING | reconciler claims subtask; `postmod` starts |
| MODDED | VERIFIED | no `postmod` configured |
| VERIFYING | VERIFIED | `postmod` exited 0 |
| VERIFYING | FAILED | `postmod` exited non-zero |
| VERIFYING | MODDED | crash recovery: claim expired |
| MODDED, VERIFIED | PENDING | doctor: required worktree missing (§7 check 2) |
| VERIFIED | PR_OPEN | branch pushed, review opened; review URL recorded |
| VERIFIED | VERIFIED | no `review` block configured (rest state) |
| PR_OPEN | MERGED | review driver reports merged |
| PR_OPEN | ABANDONED | review driver reports closed without merge |
| FAILED | PENDING | operator `retry` (attempt counter increments) |
| any non-terminal | ABANDONED | operator `abandon` (open review closed if any) |

States MUST be persisted before and after each step: a reconciler MUST write
RUNNING before invoking `run`, and the success state only after the commit
exists on disk. Every transition MUST be recorded in an append-only event log
(§6).

### 5.2 Claims, idempotency, crash recovery

Work-in-flight states (RUNNING, VERIFYING) are protected by a **claim**:
`claimed_by` (an owner identity, e.g. `host:pid`), `claimed_at`, and a lease
duration (implementation default SHOULD be ≥ the longest expected script
runtime; the claim is refreshable). A subtask in RUNNING/VERIFYING whose
claim has expired — or whose owner is provably dead — is **stale**: the
reconciler or `doctor` MUST recover it by deleting its worktree and resetting
RUNNING → PENDING, or VERIFYING → MODDED (the commit already exists; only
verification re-runs).

Claiming MUST be safe under concurrency (e.g. `SELECT … FOR UPDATE SKIP
LOCKED` or an equivalent compare-and-swap), so multiple reconcilers — or a
future daemon's worker pool — can share one database without double-running a
subtask.

All reconciler steps are idempotent from their checkpoint: re-preparing a
worktree from PENDING discards any half-finished previous attempt; opening a
review from VERIFIED MUST first check whether a review for the subtask branch
already exists (crash between "PR created" and "PR_OPEN written") and adopt
it instead of opening a duplicate.

## 6. Database

Lifecycle state lives in PostgreSQL (per SPEC.md §6, any store with atomic
compare-and-swap claiming works). Normative schema — implementations MAY add
columns but MUST preserve these semantics:

```sql
CREATE TABLE codemods (
  id          BIGSERIAL PRIMARY KEY,
  name        TEXT UNIQUE NOT NULL,
  config      JSONB NOT NULL,          -- normalized config snapshot
  config_path TEXT NOT NULL,           -- where it was registered from
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE subtasks (
  id         BIGSERIAL PRIMARY KEY,
  codemod_id BIGINT NOT NULL REFERENCES codemods(id) ON DELETE CASCADE,
  unit       TEXT NOT NULL,
  unit_slug  TEXT NOT NULL,
  state      TEXT NOT NULL DEFAULT 'PENDING',
  branch     TEXT,                     -- set when worktree is prepared
  worktree   TEXT,                     -- absolute path, set while one exists
  pr_url     TEXT,                     -- review identifier/URL
  attempts   INT NOT NULL DEFAULT 0,
  last_error TEXT,
  log_path   TEXT,
  claimed_by TEXT,
  claimed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (codemod_id, unit)
);

CREATE TABLE events (                  -- append-only audit log
  id         BIGSERIAL PRIMARY KEY,
  codemod_id BIGINT REFERENCES codemods(id) ON DELETE CASCADE,
  subtask_id BIGINT REFERENCES subtasks(id) ON DELETE CASCADE,
  at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind       TEXT NOT NULL,            -- state_change | error | doctor | register
  from_state TEXT,
  to_state   TEXT,
  detail     JSONB
);

CREATE TABLE notifications (           -- outbound message record
  id         BIGSERIAL PRIMARY KEY,
  subtask_id BIGINT REFERENCES subtasks(id) ON DELETE CASCADE,
  event      TEXT NOT NULL,            -- failed | noop | pr_open | merged | abandoned
  driver     TEXT NOT NULL,
  status     TEXT NOT NULL,            -- sent | failed
  sent_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  detail     JSONB
);
```

The `UNIQUE (codemod_id, unit)` constraint is what makes `register`
idempotent.

## 7. Operator interface (CLI)

A conforming implementation provides these commands. Exit code 0 on success,
non-zero on any error; commands MUST be safe to interrupt and re-run.

| Command | Behavior |
|---|---|
| `codemods init-db` | Create the schema. Idempotent. |
| `codemods register <config.hcl>` | Parse and validate config; upsert the codemod (config snapshot updated); evaluate decomposition; insert a PENDING subtask per *new* unit, leaving existing subtask rows untouched; report counts of new, existing, and vanished units (§8). |
| `codemods sync [--codemod NAME] [--limit N]` | The reconciler: claim and advance each eligible subtask — run PENDING ones, verify MODDED ones, open reviews for VERIFIED ones, poll review state for PR_OPEN ones, recover stale claims — then fire notifications for the events produced. One invocation advances each subtask as far as it can get. |
| `codemods status [--codemod NAME] [--json]` | Per-subtask table: unit, state, attempts, branch, review URL, last error. Plus per-codemod rollup counts. |
| `codemods doctor [--fix]` | Detect (and with `--fix`, repair) drift; see below. |
| `codemods retry <codemod> <unit>` | FAILED → PENDING, increment attempts. |
| `codemods abandon <codemod> <unit>` | Any non-terminal → ABANDONED; close its open review, delete its worktree. |

### Doctor checks

`doctor` MUST detect at least:

1. **Stale claims** — RUNNING/VERIFYING past lease (or dead owner). Fix:
   recover per §5.2.
2. **Missing worktrees** — states that require one (MODDED, VERIFIED) whose
   `worktree` path no longer exists. Fix: reset to PENDING.
3. **Vanished units** — re-evaluate the decomposition; non-terminal subtasks
   whose unit no longer exists. Fix: abandon (closing any open review).
4. **Orphaned reviews** — open reviews on branches under `branch_prefix`
   with no subtask in PR_OPEN tracking them. Fix: close the review, or adopt
   it if a VERIFIED subtask matches the branch.
5. **Orphaned worktrees** — directories under `workdir` not referenced by
   any non-terminal subtask. Fix: delete.

Without `--fix`, doctor only reports; with it, doctor repairs and logs every
repair to `events`.

## 8. Re-registration and drift

Decomposition inputs change over time: directories appear, owners are
re-mapped. Re-running `register` on an updated repo:

- **New units** → new PENDING subtasks.
- **Existing units** → untouched, whatever their state.
- **Vanished units** → reported, never auto-abandoned by `register`;
  `doctor --fix` performs the cleanup (check 3). This split keeps `register`
  read-mostly and surprise-free.

Config changes on re-register update the stored snapshot and apply to
*future* steps of all subtasks; steps already taken are not rewound.

## 9. Drivers and hooks

Implementations adapt codemods to their stack by supplying drivers (the
seams are enumerated in SPEC.md §6). Required interfaces (signatures shown
in Python for concreteness; any language works):

```python
class ReviewDriver(Protocol):
    def open(self, branch: str, title: str, body: str,
             reviewers: list[str], draft: bool) -> str: ...
    """Open a review for the already-pushed `branch`; return its URL/id.
       (The worktree provider publishes the branch.) MUST be idempotent:
       if an open review for `branch` already exists, return it."""

    def state(self, pr_url: str) -> Literal["open", "merged", "closed"]: ...

    def close(self, pr_url: str, comment: str) -> None: ...

    def find_orphans(self, branch_prefix: str) -> list[tuple[str, str]]: ...
    """(branch, pr_url) pairs of open reviews under the prefix."""

class Notifier(Protocol):
    def send(self, event: str, codemod: str, unit: str,
             subject: str, body: str) -> None: ...

class Decomposer(Protocol):
    def units(self, repo_root: Path) -> list[str]: ...
```

**Notification events** — emitted exactly once per subtask per event, with
delivery outcome recorded in `notifications`: `failed`, `noop`, `pr_open`,
`merged`, `abandoned`. The `on` list in the `notify` block selects which are
delivered. Connectors for chat systems or ticket trackers (Slack, Jira,
Asana, Linear, …) are Notifiers that react to the same events — e.g. a Jira
notifier that files a ticket on `failed` and transitions it on `merged`.

**Review drivers** — `github` drives real pull requests through the `gh`
CLI; `fake` is a hermetic file-backed driver that satisfies the same
protocol, so the entire state machine can run and be tested without GitHub
(SPEC.md §9 requires such a driver).

**Worktree provider** — this implementation clones locally
(`git clone <repo> <workdir>/<codemod>-<slug>`); an enterprise
implementation might allocate remote dev machines, container checkouts, or
sparse/shallow clones of a monorepo. The contract: produce a writable
checkout of `base_branch` with a fresh branch
`<branch_prefix>/<codemod>/<slug>` checked out; deletion must be possible at
any time (the orchestrator recreates on demand).

## 10. Execution modes

**Reconciler CLI (normative).** `sync` is invoked by an operator, cron, or CI
schedule. Because all state is in the database and every step is
checkpointed and claim-protected, frequency only affects latency, never
correctness.

**Daemon (informative, future).** A long-running service with a worker pool
claiming subtasks concurrently (the claim semantics of §5.2 already permit
this), a review-state poller or webhook listener, and a notification queue
drainer. It is the same state machine driven by threads instead of
invocations; nothing in the schema or transitions changes.

## 11. Implementation map

The reference implementation (Python, `pixi`-managed, PostgreSQL, GitHub via
`gh`, SMTP email):

| Spec section | Code |
|---|---|
| §3 configuration | `src/codemods/config.py` (python-hcl2) |
| §3.2 decomposers | `src/codemods/decompose.py`, `src/codemods/codeowners.py` |
| §4 script contract | `src/codemods/runner.py` |
| §5 state machine | `src/codemods/state.py` |
| §5–§8 reconciler, doctor | `src/codemods/engine.py` (injectable drivers) |
| §6 schema | `src/codemods/db.py` |
| §7 CLI | `src/codemods/cli.py` (click) |
| §9 review drivers | `src/codemods/review/github.py` (`gh` CLI), `src/codemods/review/fake.py` (hermetic) |
| §9 notifier | `src/codemods/notify/email.py` (SMTP; demo sink via aiosmtpd) |
| §9 worktrees | `src/codemods/worktree.py` (local `git clone`) |

An end-to-end public example (clang-tidy over curl's `lib/vauth/*.c` files)
lives in `examples/clang-tidy-curl/`, with a real-PR config (`curl.hcl`), a
hermetic variant (`curl-fake.hcl`), and fork lifecycle helpers
(`scripts/init-fork.sh`, `scripts/clean-fork.sh`). `tests/test_engine.py`
exercises the complete state machine against the fake review driver.
