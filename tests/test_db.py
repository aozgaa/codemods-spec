import pytest

from codemods import db, state as st
from codemods.config import CodemodConfig, Decomposition
from codemods.state import IllegalTransition


def make_config(name="demo"):
    return CodemodConfig(
        name=name,
        author="dev@example.com",
        repo="/tmp/repo",
        base_branch="main",
        run="/bin/true",
        decomposition=Decomposition(type="literal", items=["a", "b"]),
    )


def test_register_is_idempotent(conn):
    cfg = make_config()
    r1 = db.register(conn, cfg, "/tmp/demo.hcl", ["a", "b"])
    assert r1["new"] == ["a", "b"] and r1["existing"] == 0 and r1["vanished"] == []

    r2 = db.register(conn, cfg, "/tmp/demo.hcl", ["a", "b", "c"])
    assert r2["new"] == ["c"] and r2["existing"] == 2

    r3 = db.register(conn, cfg, "/tmp/demo.hcl", ["a", "c"])
    assert r3["new"] == [] and r3["vanished"] == ["b"]

    subtasks = db.list_subtasks(conn, r1["codemod_id"])
    assert [s["unit"] for s in subtasks] == ["a", "b", "c"]
    assert all(s["state"] == st.PENDING for s in subtasks)


def test_config_round_trip_through_db(conn):
    cfg = make_config()
    r = db.register(conn, cfg, "/tmp/demo.hcl", ["a"])
    row = db.get_codemod(conn, "demo")
    assert row["id"] == r["codemod_id"]
    assert db.config_of(row) == cfg


def test_slug_collisions_disambiguated(conn):
    cfg = make_config()
    db.register(conn, cfg, "/tmp/demo.hcl", ["x/y", "x:y"])
    slugs = [s["unit_slug"] for s in db.list_subtasks(conn)]
    assert slugs == ["x-y", "x-y-2"]


def test_happy_path_transitions(conn):
    cfg = make_config()
    r = db.register(conn, cfg, "/t", ["a"])
    s = db.get_subtask(conn, r["codemod_id"], "a")

    assert db.transition(conn, s, st.RUNNING, claim=True, worktree="/w", branch="b")
    row = db.get_subtask(conn, r["codemod_id"], "a")
    assert row["state"] == st.RUNNING
    assert row["claimed_by"] == db.claim_owner()
    assert row["branch"] == "b"

    assert db.transition(conn, s, st.MODDED)
    row = db.get_subtask(conn, r["codemod_id"], "a")
    assert row["claimed_by"] is None

    assert db.transition(conn, s, st.VERIFYING, claim=True)
    assert db.transition(conn, s, st.VERIFIED)
    assert db.transition(conn, s, st.PR_OPEN, pr_url="http://pr/1")
    assert db.transition(conn, s, st.MERGED)

    kinds = [e["kind"] for e in conn.execute("SELECT kind FROM events ORDER BY id")]
    assert kinds == ["register"] + ["state_change"] * 6


def test_illegal_transition_raises(conn):
    cfg = make_config()
    r = db.register(conn, cfg, "/t", ["a"])
    s = db.get_subtask(conn, r["codemod_id"], "a")
    with pytest.raises(IllegalTransition):
        db.transition(conn, s, st.MERGED)


def test_cas_loses_race(conn):
    cfg = make_config()
    r = db.register(conn, cfg, "/t", ["a"])
    s = db.get_subtask(conn, r["codemod_id"], "a")
    stale_view = dict(s)
    assert db.transition(conn, s, st.RUNNING, claim=True)
    # A second reconciler holding the old PENDING row loses the race.
    assert not db.transition(conn, stale_view, st.RUNNING, claim=True)


def test_claim_blocks_foreign_unexpired(conn):
    cfg = make_config()
    r = db.register(conn, cfg, "/t", ["a"])
    s = db.get_subtask(conn, r["codemod_id"], "a")
    assert db.transition(conn, s, st.RUNNING, claim=True)
    # Simulate another live owner holding the claim.
    conn.execute("UPDATE subtasks SET claimed_by = 'otherhost:1' WHERE id = %s", (s["id"],))
    conn.commit()
    fresh = db.get_subtask(conn, r["codemod_id"], "a")
    assert not db.transition(conn, fresh, st.PENDING, claim=True)


def test_stale_claims_detects_expired_and_dead(conn):
    cfg = make_config()
    r = db.register(conn, cfg, "/t", ["a", "b"])
    sa = db.get_subtask(conn, r["codemod_id"], "a")
    sb = db.get_subtask(conn, r["codemod_id"], "b")
    db.transition(conn, sa, st.RUNNING, claim=True)
    db.transition(conn, sb, st.RUNNING, claim=True)
    # a: lease expired; b: held by us, alive.
    conn.execute(
        "UPDATE subtasks SET claimed_at = now() - interval '2 hours' WHERE id = %s",
        (sa["id"],))
    conn.commit()
    stale = db.stale_claims(conn, lease_seconds=3600)
    assert [x["unit"] for x in stale] == ["a"]
    # b becomes stale when its owner pid is dead on this host.
    import socket
    conn.execute("UPDATE subtasks SET claimed_by = %s WHERE id = %s",
                 (f"{socket.gethostname()}:999999", sb["id"]))
    conn.commit()
    stale = db.stale_claims(conn, lease_seconds=3600)
    assert sorted(x["unit"] for x in stale) == ["a", "b"]
    # Recovery transition: RUNNING -> PENDING.
    for row in stale:
        assert db.transition(conn, row, st.PENDING)
    assert all(s["state"] == st.PENDING for s in db.list_subtasks(conn))


def test_notifications_dedupe(conn):
    cfg = make_config()
    r = db.register(conn, cfg, "/t", ["a"])
    s = db.get_subtask(conn, r["codemod_id"], "a")
    assert not db.notified(conn, s["id"], "pr_open")
    db.record_notification(conn, s["id"], "pr_open", "email", "failed")
    assert not db.notified(conn, s["id"], "pr_open")
    db.record_notification(conn, s["id"], "pr_open", "email", "sent")
    assert db.notified(conn, s["id"], "pr_open")
