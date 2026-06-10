"""Campaign management and authorship workflow (SPEC.md §4.2, §7):
pause/resume/cancel, review throttling, auto-pause, test stage, promote,
daemon. Review drivers resolve from config (driver = "fake") so the
test-stage overrides apply exactly as in production use."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from codemods import db, state as st
from codemods.engine import Engine

AUTHOR = "author@example.com"


class RecordingNotifier:
    def __init__(self):
        self.sent = []  # (event, unit, to)

    def send(self, cfg, event, codemod, unit, subject, body):
        self.sent.append((event, unit, tuple(cfg.to)))


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "upstream"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for d in ("alpha", "beta", "gamma"):
        (repo / d).mkdir()
        (repo / d / "code.txt").write_text(f"original {d}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def project(tmp_path, upstream):
    proj = tmp_path / "proj"
    proj.mkdir()
    run = proj / "run.sh"
    run.write_text('#!/bin/sh\necho mod >> "$1/code.txt"\n')
    run.chmod(0o755)
    return proj


def write_hcl(project, upstream, *, limits="", test_block=""):
    (project / "demo.hcl").write_text(textwrap.dedent(f'''
        codemod "demo" {{
          author      = "{AUTHOR}"
          repo        = "{upstream}"
          base_branch = "main"
          run         = "./run.sh"
          decomposition {{
            type    = "glob"
            include = ["*"]
            kind    = "directory"
          }}
          review {{
            driver = "fake"
            repo   = "{project}/prs-prod.json"
          }}
          notify {{
            driver = "email"
            to     = ["owners@example.com"]
            on     = ["failed", "noop", "pr_open", "merged", "abandoned"]
          }}
          {limits}
          {test_block}
        }}
    '''))
    return project / "demo.hcl"


@pytest.fixture
def engine(conn):
    notifier = RecordingNotifier()
    eng = Engine(conn, notifier=notifier)
    eng.recorded = notifier.sent
    return eng


def states(conn):
    return {s["unit"]: s["state"] for s in db.list_subtasks(conn)}


def prs(path):
    return json.loads(path.read_text()) if path.exists() else {}


def test_pause_resume(engine, conn, project, upstream):
    engine.register(write_hcl(project, upstream))
    engine.pause("demo", reason="too noisy")
    outcomes = engine.sync()
    assert any("skipped: codemod is paused" in str(o) for o in outcomes)
    assert set(states(conn).values()) == {st.PENDING}

    engine.resume("demo")
    engine.sync()
    assert set(states(conn).values()) == {st.PR_OPEN}

    with pytest.raises(ValueError, match="not paused"):
        engine.resume("demo")


def test_cancel_abandons_and_closes_reviews(engine, conn, project, upstream):
    engine.register(write_hcl(project, upstream))
    engine.sync()
    engine.cancel("demo")
    assert set(states(conn).values()) == {st.ABANDONED}
    assert all(p["state"] == "closed" for p in prs(project / "prs-prod.json").values())
    cm = db.get_codemod(conn, "demo")
    assert cm["status"] == st.CM_CANCELLED
    # Cancelled is terminal: sync skips, cancel again errors.
    assert any("skipped" in str(o) for o in engine.sync())
    with pytest.raises(ValueError, match="already cancelled"):
        engine.cancel("demo")


def test_review_throttle(engine, conn, project, upstream):
    engine.register(write_hcl(project, upstream,
                              limits="limits {\n  max_open_reviews = 1\n}"))
    engine.sync()
    by_state = sorted(states(conn).values())
    assert by_state == [st.PR_OPEN, st.VERIFIED, st.VERIFIED]

    # A merge frees a slot; the next sync opens exactly one more.
    from codemods.review import fake
    url = next(s["pr_url"] for s in db.list_subtasks(conn) if s["pr_url"])
    fake.merge(project / "prs-prod.json", url)
    engine.sync()
    assert sorted(states(conn).values()) == [st.MERGED, st.PR_OPEN, st.VERIFIED]


def test_auto_pause_on_failures(engine, conn, project, upstream):
    (project / "run.sh").write_text("#!/bin/sh\nexit 1\n")
    engine.register(write_hcl(project, upstream,
                              limits="limits {\n  max_failures = 2\n}"))
    outcomes = engine.sync()
    assert any("auto-paused" in str(o) for o in outcomes)
    cm = db.get_codemod(conn, "demo")
    assert cm["status"] == st.CM_PAUSED
    assert "auto-paused" in cm["status_reason"]
    # The author was notified, beyond the per-subtask failure notifications.
    assert ("paused", "*", (AUTHOR,)) in engine.recorded

    # Fix the script, retry the failures, resume: campaign completes.
    (project / "run.sh").write_text('#!/bin/sh\necho mod >> "$1/code.txt"\n')
    for sub in db.list_subtasks(conn, states=[st.FAILED]):
        engine.retry("demo", sub["unit"])
    engine.resume("demo")
    engine.sync()
    assert set(states(conn).values()) == {st.PR_OPEN}


def test_test_stage_overrides(engine, conn, project, upstream):
    engine.register(write_hcl(project, upstream), test=True)
    cm = db.get_codemod(conn, "demo")
    assert cm["stage"] == st.STAGE_TEST

    engine.sync()
    assert set(states(conn).values()) == {st.PR_OPEN}
    # Reviews are drafts with a [test] title, on the production fake file
    # (no test.review override in this config).
    for pr in prs(project / "prs-prod.json").values():
        assert pr["draft"] is True
        assert pr["title"].startswith("[test] ")
    # Notifications went to the author only, not the owner list.
    assert engine.recorded and all(to == (AUTHOR,) for _, _, to in engine.recorded)


def test_sample_and_promote(engine, conn, project, upstream):
    test_block = textwrap.dedent(f'''
        test {{
          sample = 2
          review {{
            driver = "fake"
            repo   = "{project}/prs-test.json"
          }}
        }}
    ''')
    engine.register(write_hcl(project, upstream, test_block=test_block), test=True)
    assert len(db.list_subtasks(conn)) == 2  # sampled

    engine.sync()
    assert set(states(conn).values()) == {st.PR_OPEN}
    assert len(prs(project / "prs-test.json")) == 2
    assert not (project / "prs-prod.json").exists()

    r = engine.promote("demo")
    assert r["abandoned"] == 2 and r["generation"] == 2 and r["units"] == 3
    cm = db.get_codemod(conn, "demo")
    assert cm["stage"] == st.STAGE_PRODUCTION and cm["generation"] == 2
    # Test reviews closed; current generation is fresh.
    assert all(p["state"] == "closed" for p in prs(project / "prs-test.json").values())
    assert set(states(conn).values()) == {st.PENDING}
    assert len(db.list_subtasks(conn)) == 3
    # Old generation retained for audit.
    assert len(db.list_subtasks(conn, current_generation_only=False)) == 5

    engine.sync()
    assert set(states(conn).values()) == {st.PR_OPEN}
    prod = prs(project / "prs-prod.json")
    assert len(prod) == 3 and all(not p["draft"] for p in prod.values())

    with pytest.raises(ValueError, match="only test-stage"):
        engine.promote("demo")


def test_daemon_runs_and_stops(conn, project, upstream, test_database):
    """The daemon is the same engine in a loop: it must advance work and
    exit cleanly on SIGTERM."""
    eng = Engine(conn, notifier=RecordingNotifier())
    eng.register(write_hcl(project, upstream))

    env = dict(os.environ, CODEMODS_DSN=test_database)
    proc = subprocess.Popen(
        [sys.executable, "-m", "codemods.cli", "daemon", "--interval", "1"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if set(states(conn).values()) == {st.PR_OPEN}:
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"daemon never advanced subtasks: {states(conn)}")
    finally:
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=15)
    assert proc.returncode == 0, out
    assert "daemon stopped" in out
