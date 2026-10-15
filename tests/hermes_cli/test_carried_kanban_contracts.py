"""Controlled dispatcher and notification contracts against a real temporary board."""
import json
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text("kanban:\n  profile_notification_targets:\n    default: 'telegram:123'\n")
    kb.init_db()
    with kbc.connect() as conn:
        yield conn, home


@pytest.mark.parametrize("kind", ["transient", "needs_input", "capability"])
def test_human_block_subscribes_before_event(board, kind):
    conn, _ = board
    tid = kb.create_task(conn, title="Decision", assignee="default")
    assert kb.block_task(conn, tid, kind=kind, reason="need decision")
    subs = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id=?", (tid,)).fetchall()
    if kind == "transient":
        assert not subs
    else:
        assert len(subs) == 1
        event = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchone()
        assert subs[0]["last_event_id"] < event["id"]
        assert subs[0]["notifier_profile"] == "default"


@pytest.mark.parametrize("status", ["running", "blocked"])
def test_created_human_gate_subscription(board, status):
    conn, _ = board
    tid = kb.create_task(conn, title="Decision", assignee="default", initial_status=status)
    subs = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id=?", (tid,)).fetchall()
    assert bool(subs) == (status == "blocked")


def tick(conn, tid, monkeypatch, reason):
    monkeypatch.setattr(dispatch, "check_respawn_guard", lambda *a, **k: reason)
    return dispatch.dispatch_once(conn, spawn_fn=lambda *a: None, max_in_progress=10)



@pytest.mark.parametrize("repetitions", [1, 60])
def test_guard_records_transition_not_ticks(board, monkeypatch, repetitions):
    conn, _ = board
    tid = kb.create_task(conn, title="Guard", assignee="default")
    for _ in range(repetitions):
        result = tick(conn, tid, monkeypatch, "active_pr")
        assert result.respawn_guarded == [(tid, "active_pr")]
        assert not result.spawned
    rows = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='respawn_guarded'", (tid,)).fetchall()
    assert len(rows) == 1
    if repetitions == 1:
        return
    assert json.loads(rows[0]["payload"])["repeats"] == repetitions
    tick(conn, tid, monkeypatch, None)
    released = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='respawn_guard_released'", (tid,)).fetchall()
    assert len(released) == 1


@pytest.mark.parametrize("installed", [False, True], ids=["missing", "present"])
def test_pinned_skill_checked_before_claim(board, monkeypatch, installed):
    conn, home = board
    if installed:
        folder = home / "skills" / "unique-required-skill"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: unique-required-skill\ndescription: Test skill.\n---\nInstructions.\n")
    tid = kb.create_task(conn, title="Pin", assignee="default", skills=["unique-required-skill"])
    result = tick(conn, tid, monkeypatch, None)
    task = kb.get_task(conn, tid)
    if installed:
        assert result.spawned
        assert task.status == "running"
    else:
        assert not result.spawned
        assert task.status == "blocked"
        assert task.block_kind == "capability"
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='pinned_skill_missing'", (tid,)).fetchone()[0] == 1


@pytest.mark.parametrize("flapping", [False, True])
def test_guard_event_rate_is_bounded(board, monkeypatch, flapping):
    conn, _ = board
    tid = kb.create_task(conn, title="Guard", assignee="default")
    for index in range(60):
        reason = "blocker_auth" if flapping and index % 2 else "active_pr"
        assert tick(conn, tid, monkeypatch, reason).respawn_guarded == [(tid, reason)]
    count = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind IN ('respawn_guarded','respawn_guard_released')", (tid,)).fetchone()[0]
    assert count <= 6


@pytest.mark.parametrize("installed", [False, True])
def test_review_lane_checks_forced_skill(board, monkeypatch, installed):
    conn, home = board
    if installed:
        folder = home / "skills" / "sdlc-review"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: sdlc-review\ndescription: Review.\n---\nReview instructions.\n")
    tid = kb.create_task(conn, title="Review", assignee="default")
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    result = dispatch.DispatchResult()
    monkeypatch.setattr(dispatch, "check_respawn_guard", lambda *a, **k: None)
    dispatch._dispatch_lane_task(conn, row, "default", result, lane="review",
        spawn_fn=lambda *a: None, ttl_seconds=60, failure_limit=2,
        dry_run=False, board=None, per_profile_cap=None, per_profile_running={})
    assert bool(result.spawned) == installed
    if not installed:
        assert kb.get_task(conn, tid).block_kind == "capability"
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 0
