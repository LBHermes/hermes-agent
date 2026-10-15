"""Bounded transition ledger for the dispatcher respawn guard."""
import json
import time

GUARD_EVENT_KINDS = ("respawn_guarded", "respawn_guard_released")
GUARD_EVENT_MAX_ROWS_PER_HOUR = 6


def guard_event_plan(previous, reason, now):
    """Return the next transition or an in-place repeat, independent of storage."""
    if previous and previous["kind"] == "respawn_guarded":
        payload = json.loads(previous["payload"] or "{}")
        if "first_seen_at" in payload:
            payload["last_seen_at"] = now
            if payload.get("reason") == reason:
                payload["repeats"] = payload.get("repeats", 1) + 1
                return "repeat", payload
            if reason is None:
                payload["duration_seconds"] = max(0, now - payload["first_seen_at"])
                return "respawn_guard_released", payload
    if reason is None:
        return None, None
    return "respawn_guarded", {"reason": reason, "repeats": 1,
                               "first_seen_at": now, "last_seen_at": now}


def apply_guard_event(conn, task_id, reason):
    from hermes_cli import kanban_db as kb
    now = int(time.time())
    with kb.write_txn(conn):
        previous = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND kind IN (?, ?) ORDER BY id DESC LIMIT 1",
            (task_id, *GUARD_EVENT_KINDS),
        ).fetchone()
        kind, payload = guard_event_plan(previous, reason, now)
        if kind is None:
            return
        if kind == "repeat":
            conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), previous["id"]))
            return
        count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind IN (?, ?) AND created_at>?",
            (task_id, *GUARD_EVENT_KINDS, now - 3600),
        ).fetchone()[0]
        if count < GUARD_EVENT_MAX_ROWS_PER_HOUR:
            kb._append_event(conn, task_id, kind, payload)
