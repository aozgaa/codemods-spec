# codemods

A specification for a system that lands big mechanical refactors
(clang-tidy sweeps, API renames, …) by splitting them into small, per-unit
changes that are run, verified, and code-reviewed independently, with every
unit tracked end-to-end in a database.

**The deliverable of this repository is the specification.** Codemods
integrates with organization-specific infrastructure at nearly every
boundary (version control, code review, notifications, machine
provisioning, state storage), so each enterprise is expected to implement
the system from scratch against its own stack:

- **[SPEC.md](SPEC.md)** — the general specification: system structure,
  lifecycle requirements, and the integration boundaries where enterprise
  stacks differ. This is the document an implementer works from.

**Everything else in this repository is a demo** — one worked example of a
conforming implementation, kept runnable so implementers can see the whole
lifecycle execute and compare behavior against their own system. It is not
a tool intended for production use:

- **[EXAMPLE_SPEC.md](EXAMPLE_SPEC.md)** — the prescriptive spec of the
  demo implementation: HCL config schema, exact state machine, SQL schema,
  driver signatures, CLI. None of its technology choices are normative.
- **`src/codemods/`** — the demo implementation: Python, PostgreSQL, git
  worktrees by local clone, GitHub reviews via `gh`, email via SMTP.
  Orchestration lives in `engine.py` behind injectable driver interfaces; a
  file-backed fake review driver (`review/fake.py`) runs the whole
  lifecycle without GitHub.
- **`examples/clang-tidy-curl/`** — a demo campaign against a fork of curl:
  public, cross-platform, one PR per `lib/vauth/*.c` file. `curl.hcl` opens
  real GitHub PRs; `curl-fake.hcl` runs the same codemod with the fake
  review driver.

## Running the demo

Requires [pixi](https://pixi.sh) and, for the real-PR variant, a logged-in
`gh` (`gh auth login`).

```sh
pixi install
pixi run db-init           # one-time: create local Postgres data dir
pixi run db-start          # postgres on localhost:5499
pixi run smtp-sink &       # demo mail sink on localhost:8025 -> .mail/
pixi run codemods init-db

examples/clang-tidy-curl/scripts/init-fork.sh   # fork curl + clone to ../curl
pixi run codemods register examples/clang-tidy-curl/curl-fake.hcl
pixi run codemods sync --codemod curl-tidy-braces-fake  # run + verify + fake reviews
pixi run codemods status --codemod curl-tidy-braces-fake
```

Use `examples/clang-tidy-curl/curl.hcl` instead to open real PRs on your
curl fork (and `scripts/clean-fork.sh` to close them afterwards).

`sync` is a reconciler: every invocation advances each subtask as far as it
can (run script → commit → postmod → push → open PR → poll review state) and
is safe to kill and re-run at any point. Repeated runs carry every subtask
to MERGED / NOOP / ABANDONED.

Operator commands (SPEC.md §7, as realized by the demo):

```sh
pixi run codemods status --json        # machine-readable
pixi run codemods doctor               # report drift (stale claims, orphans…)
pixi run codemods doctor --fix         # repair it
pixi run codemods retry  <codemod> <unit>
pixi run codemods abandon <codemod> <unit>
```

To point the demo at a different repository or transformation: write a run
script (argv[1] = unit, exit 0 on success; empty diff ⇒ NOOP), optionally a
postmod script (build + test), and an HCL file naming them — see
`examples/clang-tidy-curl/curl-fake.hcl` and EXAMPLE_SPEC.md §3.

## Tests

```sh
pixi run db-start   # tests need the local postgres
pixi run test
```

`tests/test_engine.py` exercises the complete state machine — including
crash recovery and doctor repairs — against temp git repos, the fake review
driver, and a recording notifier; no GitHub or SMTP needed. SPEC.md §9
requires this kind of hermetic testability of any implementation.
