"""Operator CLI (EXAMPLE_SPEC.md §7) — a thin presentation layer over engine.Engine."""

from __future__ import annotations

import json
import sys

import click

from . import db, state as st
from .engine import Engine


@click.group()
@click.option("--dsn", envvar="CODEMODS_DSN", default=None,
              help="Postgres DSN (default: $CODEMODS_DSN)")
@click.pass_context
def main(ctx, dsn):
    """Split big refactors into per-unit, individually reviewed changes."""
    ctx.obj = dsn


def _engine(ctx) -> tuple[Engine, object]:
    conn = db.connect(ctx.obj)
    return Engine(conn), conn


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (LookupError, ValueError) as e:
        raise click.ClickException(str(e)) from e


def _print(outcomes) -> None:
    for o in outcomes:
        click.echo(str(o))
    if not outcomes:
        click.echo("nothing to do")


@main.command("init-db")
@click.pass_context
def init_db(ctx):
    """Create the database schema (idempotent)."""
    with db.connect(ctx.obj) as conn:
        db.init_db(conn)
    click.echo("schema ready")


@main.command()
@click.argument("config_path", type=click.Path(exists=True))
@click.pass_context
def register(ctx, config_path):
    """Register a codemod config and decompose it into subtasks."""
    engine, conn = _engine(ctx)
    with conn:
        r = engine.register(config_path)
    click.echo(f"codemod {r['name']!r}: {r['units']} units "
               f"({len(r['new'])} new, {r['existing']} existing)")
    for u in r["new"]:
        click.echo(f"  + {u}")
    for u in r["vanished"]:
        click.echo(f"  ? vanished (doctor --fix to abandon): {u}")


@main.command()
@click.option("--codemod", "name", default=None, help="Only this codemod.")
@click.option("--limit", type=int, default=None, help="Advance at most N subtasks.")
@click.pass_context
def sync(ctx, name, limit):
    """Advance every non-terminal subtask as far as it can go."""
    engine, conn = _engine(ctx)
    with conn:
        _print(_run(engine.sync, name, limit))


@main.command()
@click.option("--codemod", "name", default=None, help="Only this codemod.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def status(ctx, name, as_json):
    """Show subtask states and per-codemod rollups."""
    with db.connect(ctx.obj) as conn:
        cm = db.get_codemod(conn, name) if name else None
        if name and cm is None:
            raise click.ClickException(f"no codemod named {name!r}")
        rows = db.list_subtasks(conn, cm["id"] if cm else None)
    if as_json:
        click.echo(json.dumps(
            [{k: str(v) if k.endswith("_at") else v for k, v in r.items()}
             for r in rows], indent=2, default=str))
        return
    if not rows:
        click.echo("no subtasks registered")
        return
    widths = (max(len(r["codemod_name"]) for r in rows),
              max(len(r["unit"]) for r in rows))
    for r in rows:
        line = (f"{r['codemod_name']:<{widths[0]}}  {r['unit']:<{widths[1]}}  "
                f"{r['state']:<9}  attempts={r['attempts']}")
        if r["pr_url"]:
            line += f"  {r['pr_url']}"
        if r["state"] == st.FAILED and r["last_error"]:
            line += f"  ({r['last_error']})"
        click.echo(line)
    rollup: dict[str, dict[str, int]] = {}
    for r in rows:
        rollup.setdefault(r["codemod_name"], {}).setdefault(r["state"], 0)
        rollup[r["codemod_name"]][r["state"]] += 1
    for cm_name, counts in sorted(rollup.items()):
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        click.echo(f"-- {cm_name}: {summary}")


@main.command()
@click.option("--codemod", "name", default=None, help="Only this codemod.")
@click.option("--fix", is_flag=True, help="Repair findings instead of only reporting.")
@click.pass_context
def doctor(ctx, name, fix):
    """Detect (and with --fix, repair) state drift."""
    engine, conn = _engine(ctx)
    with conn:
        findings = _run(engine.doctor, fix=fix, codemod_name=name)
    _print(findings)
    if findings and not fix:
        sys.exit(1)


@main.command()
@click.argument("codemod_name")
@click.argument("unit")
@click.pass_context
def retry(ctx, codemod_name, unit):
    """Re-queue a FAILED subtask."""
    engine, conn = _engine(ctx)
    with conn:
        click.echo(str(_run(engine.retry, codemod_name, unit)))


@main.command()
@click.argument("codemod_name")
@click.argument("unit")
@click.pass_context
def abandon(ctx, codemod_name, unit):
    """Abandon a subtask, closing its review if one is open."""
    engine, conn = _engine(ctx)
    with conn:
        click.echo(str(_run(engine.abandon, codemod_name, unit)))


if __name__ == "__main__":
    main()
