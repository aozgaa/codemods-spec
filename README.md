# codemods

Split big mechanical refactors (clang-tidy sweeps, API renames, …) into
small, per-unit changes that are run, verified, and code-reviewed
independently — so a 100-reviewer mega-PR becomes N reviewable ones, tracked
end-to-end in a database.

- **[SPEC.md](SPEC.md)** — the general specification: philosophy,
  architecture, lifecycle guarantees, and the adaptation points along which
  enterprise stacks differ. Implementation-agnostic.
- **[EXAMPLE_SPEC.md](EXAMPLE_SPEC.md)** — the prescriptive spec of the
  reference implementation: HCL config schema, exact state machine, SQL
  schema, driver signatures, CLI. One concrete instantiation of SPEC.md.
- **`src/codemods/`** — the reference implementation: Python, PostgreSQL,
  git worktrees by local clone, GitHub reviews via `gh`, email notification
  via SMTP. All orchestration lives in `engine.py` behind injectable driver
  interfaces; a file-backed fake review driver (`review/fake.py`) runs the
  whole lifecycle without GitHub.
- **`examples/clang-tidy-curl/`** — public, cross-platform campaign against
  a curl fork. `curl.hcl` opens GitHub PRs; `curl-fake.hcl` runs the same
  codemod with the local fake review driver.

## Quickstart

Requires [pixi](https://pixi.sh) and, for GitHub reviews, a logged-in `gh`
(`gh auth login`).

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

Use `examples/clang-tidy-curl/curl.hcl` instead when you want real PRs on
`aozgaa/curl`.

`sync` is a reconciler: every invocation advances each subtask as far as it
can (run script → commit → postmod → push → open PR → poll review state) and
is safe to kill and re-run at any point. Run it from cron until everything
is MERGED / NOOP / ABANDONED.

Operations:

```sh
pixi run codemods status --json        # machine-readable
pixi run codemods doctor               # report drift (stale claims, orphans…)
pixi run codemods doctor --fix         # repair it
pixi run codemods retry  <codemod> <unit>
pixi run codemods abandon <codemod> <unit>
```

## Tests

```sh
pixi run db-start   # tests need the local postgres
pixi run test
```

`tests/test_engine.py` exercises the complete state machine — including
crash recovery and doctor repairs — against temp git repos, the fake review
driver, and a recording notifier; no GitHub or SMTP needed.

## Writing your own codemod

1. Write a run script: takes one unit (file/dir/target/owner) as `$1`,
   applies the change, exits 0. Empty diff ⇒ the unit is recorded NOOP.
2. Optionally a postmod script: build + test the result.
3. Describe it in HCL (see `examples/clang-tidy-curl/curl-fake.hcl`; the four
   decomposition types are `literal`, `glob`, `command`, `codeowners`).
4. `codemods register your.hcl && codemods sync`.

See [EXAMPLE_SPEC.md](EXAMPLE_SPEC.md) for the full contract, and
[SPEC.md](SPEC.md) for the philosophy behind it.
