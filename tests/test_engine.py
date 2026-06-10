"""Full state-machine tests: real git on temp repos, fake review driver,
recording notifier. No GitHub, no SMTP."""

import os
import socket
import stat
import subprocess
import textwrap

import pytest

from codemods import db, state as st
from codemods.engine import Engine
from codemods.review import fake
from codemods.review.fake import FakeReviewDriver


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, cfg, event, codemod, unit, subject, body):
        self.sent.append((event, unit))


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def upstream(tmp_path):
    """The repo being refactored: three dirs, one file each."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for d in ("alpha", "beta", "gamma"):
        (repo / d).mkdir()
        (repo / d / "code.txt").write_text(f"original {d}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def write_script(path, body):
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def project(tmp_path, upstream):
    """Config dir with run/postmod scripts and an HCL config using fakes."""
    proj = tmp_path / "proj"
    proj.mkdir()
    write_script(proj / "run.sh", 'echo "modded by $CODEMODS_NAME" >> "$1/code.txt"\n')
    write_script(proj / "postmod.sh", 'grep -q modded "$1/code.txt"\n')
    (proj / "demo.hcl").write_text(f'''
codemod "demo" {{
  author      = "author@example.com"
  repo        = "{upstream}"
  base_branch = "main"
  run         = "./run.sh"
  postmod     = "./postmod.sh"
  decomposition {{
    type    = "glob"
    include = ["*"]
    kind    = "directory"
  }}
  review {{
    driver = "fake"
    repo   = "{tmp_path}/prs.json"
  }}
  notify {{
    driver = "email"
    to     = ["dev@example.com"]
    on     = ["failed", "noop", "pr_open", "merged", "abandoned"]
  }}
}}
''')
    return proj


@pytest.fixture
def engine(conn, project):
    notifier = RecordingNotifier()
    eng = Engine(conn, review_driver=FakeReviewDriver(), notifier=notifier)
    eng.recorded = notifier.sent
    eng.project = project
    return eng


def states(conn):
    return {s["unit"]: s["state"] for s in db.list_subtasks(conn)}


def test_happy_path_to_merged_and_abandoned(engine, conn, tmp_path, upstream):
    r = engine.register(engine.project / "demo.hcl")
    assert r["units"] == 3 and len(r["new"]) == 3

    engine.sync()
    assert states(conn) == {"alpha": st.PR_OPEN, "beta": st.PR_OPEN, "gamma": st.PR_OPEN}
    assert sorted(e for e, _ in engine.recorded) == ["pr_open"] * 3

    # Branches really exist on the upstream remote.
    out = subprocess.run(["git", "-C", str(upstream), "branch", "--list", "codemods/demo/*"],
                         capture_output=True, text=True, check=True).stdout
    assert sorted(b.strip() for b in out.splitlines()) == [
        "codemods/demo/alpha", "codemods/demo/beta", "codemods/demo/gamma"]

    # Sync again: PR still open, nothing changes, no duplicate PRs/notifications.
    engine.sync()
    assert states(conn)["alpha"] == st.PR_OPEN
    assert len(engine.recorded) == 3

    # The review tool merges one and rejects another.
    subs = {s["unit"]: s for s in db.list_subtasks(conn)}
    fake.merge(tmp_path / "prs.json", subs["alpha"]["pr_url"])
    fake.close(tmp_path / "prs.json", subs["beta"]["pr_url"])
    engine.sync()
    assert states(conn) == {"alpha": st.MERGED, "beta": st.ABANDONED, "gamma": st.PR_OPEN}
    assert ("merged", "alpha") in engine.recorded
    assert ("abandoned", "beta") in engine.recorded
    # Terminal subtasks' worktrees are cleaned up.
    assert not os.path.exists(subs["alpha"]["worktree"])
    assert not os.path.exists(subs["beta"]["worktree"])


def test_failed_run_then_retry(engine, conn):
    write_script(engine.project / "run.sh", "exit 7\n")
    engine.register(engine.project / "demo.hcl")
    engine.sync()
    assert set(states(conn).values()) == {st.FAILED}
    assert sorted(e for e, _ in engine.recorded) == ["failed"] * 3
    sub = db.list_subtasks(conn)[0]
    assert "exited 7" in sub["last_error"]
    assert sub["attempts"] == 1

    # FAILED is sticky across syncs (no auto-retry), then operator retries.
    engine.sync()
    assert states(conn)[sub["unit"]] == st.FAILED
    write_script(engine.project / "run.sh",
                 'echo "modded by $CODEMODS_NAME" >> "$1/code.txt"\n')
    engine.retry("demo", sub["unit"])
    engine.sync()
    assert states(conn)[sub["unit"]] == st.PR_OPEN
    assert db.get_subtask(conn, sub["codemod_id"], sub["unit"])["attempts"] == 2


def test_noop_when_script_changes_nothing(engine, conn):
    write_script(engine.project / "run.sh", "true\n")
    engine.register(engine.project / "demo.hcl")
    engine.sync()
    assert set(states(conn).values()) == {st.NOOP}
    assert sorted(e for e, _ in engine.recorded) == ["noop"] * 3


def test_postmod_failure(engine, conn):
    write_script(engine.project / "postmod.sh", "exit 3\n")
    engine.register(engine.project / "demo.hcl")
    engine.sync()
    assert set(states(conn).values()) == {st.FAILED}
    assert all("postmod" in s["last_error"] for s in db.list_subtasks(conn))


def test_crash_recovery_dead_claim(engine, conn):
    engine.register(engine.project / "demo.hcl")
    # Simulate a reconciler that died mid-run: RUNNING, claim held by a dead
    # pid on this host.
    sub = db.list_subtasks(conn)[0]
    db.transition(conn, sub, st.RUNNING, claim=True)
    conn.execute("UPDATE subtasks SET claimed_by = %s WHERE id = %s",
                 (f"{socket.gethostname()}:999999", sub["id"]))
    conn.commit()
    engine.sync()  # recovers to PENDING, then runs to completion
    assert states(conn)[sub["unit"]] == st.PR_OPEN


def test_doctor_missing_worktree(engine, conn, project):
    # No review block -> subtasks rest at VERIFIED, holding a worktree.
    hcl = (project / "demo.hcl").read_text()
    (project / "demo.hcl").write_text(hcl[:hcl.index("  review")] + "}\n")
    engine.register(project / "demo.hcl")
    engine.sync()
    assert set(states(conn).values()) == {st.VERIFIED}

    sub = db.list_subtasks(conn)[0]
    import shutil
    shutil.rmtree(sub["worktree"])
    findings = engine.doctor(fix=False)
    assert any("worktree missing" in f.message for f in findings)
    assert states(conn)[sub["unit"]] == st.VERIFIED  # report-only

    engine.doctor(fix=True)
    assert states(conn)[sub["unit"]] == st.PENDING
    engine.sync()
    assert states(conn)[sub["unit"]] == st.VERIFIED  # recovered end-to-end


def test_doctor_vanished_unit_and_orphan_worktree(engine, conn, upstream, tmp_path):
    engine.register(engine.project / "demo.hcl")
    engine.sync()

    # A directory disappears upstream after registration.
    git(upstream, "rm", "-r", "-q", "gamma")
    git(upstream, "commit", "-q", "-m", "drop gamma")
    # And someone leaves a stray worktree directory behind.
    cfg = db.config_of(db.get_codemod(conn, "demo"))
    stray = (tmp_path / "proj/work/worktrees/demo-stray")
    stray.mkdir(parents=True)

    findings = engine.doctor(fix=True)
    msgs = [f.message for f in findings]
    assert any("unit vanished" in m for m in msgs)
    assert any("orphaned worktree" in m for m in msgs)
    assert states(conn)["gamma"] == st.ABANDONED
    assert not stray.exists()
    # The abandoned unit's fake PR was closed.
    sub = {s["unit"]: s for s in db.list_subtasks(conn)}["gamma"]
    assert FakeReviewDriver().state(cfg.review, sub["pr_url"]) == "closed"


def test_doctor_adopts_and_closes_orphan_reviews(engine, conn, tmp_path):
    engine.register(engine.project / "demo.hcl")
    engine.sync()
    cfg = db.config_of(db.get_codemod(conn, "demo"))
    driver = FakeReviewDriver()

    # Crash between "PR created" and "PR_OPEN recorded": rewind one subtask
    # to VERIFIED and forget its pr_url; its open PR is now untracked.
    sub = db.list_subtasks(conn)[0]
    conn.execute("UPDATE subtasks SET state = 'VERIFIED', pr_url = NULL WHERE id = %s",
                 (sub["id"],))
    # Plus a PR on a branch no subtask owns at all.
    orphan_url = driver.open(cfg.review, "codemods/demo/zzz-gone", "main", "t", "b")
    conn.commit()

    engine.doctor(fix=True)
    fresh = db.get_subtask(conn, sub["codemod_id"], sub["unit"])
    assert fresh["state"] == st.PR_OPEN and fresh["pr_url"]  # adopted
    assert driver.state(cfg.review, orphan_url) == "closed"


def test_abandon_closes_review_and_cleans_up(engine, conn, tmp_path):
    engine.register(engine.project / "demo.hcl")
    engine.sync()
    sub = {s["unit"]: s for s in db.list_subtasks(conn)}["alpha"]
    engine.abandon("demo", "alpha")
    cfg = db.config_of(db.get_codemod(conn, "demo"))
    assert states(conn)["alpha"] == st.ABANDONED
    assert FakeReviewDriver().state(cfg.review, sub["pr_url"]) == "closed"
    assert not os.path.exists(sub["worktree"])
    with pytest.raises(ValueError, match="already terminal"):
        engine.abandon("demo", "alpha")
