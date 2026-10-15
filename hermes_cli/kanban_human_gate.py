"""Subscribe configured human decision routes while recording a block."""
import logging
import time

logger = logging.getLogger(__name__)


def resolve_route(assignee):
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly().get("kanban", {})
    local = config.get("profile_notification_targets") or {}
    shared = config.get("global_notification_targets") or {}
    orchestrator = config.get("orchestrator_profile")
    candidates = [(table, name) for name in (assignee, orchestrator) for table in (local, shared)]
    if isinstance(shared, dict) and len(shared) == 1:
        candidates.append((shared, next(iter(shared))))
    for table, name in candidates:
        target = table.get(name) if isinstance(table, dict) and name else None
        if not isinstance(target, str):
            continue
        platform, separator, chat = target.partition(":")
        if separator and platform.strip() and chat.strip():
            return name, platform.strip().lower(), chat.strip()
    return None


def subscribe_block(conn, task_id, payload, event_id):
    if payload.get("kind") not in {"needs_input", "capability"} and payload.get("reason") != "initial_status":
        return
    row = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()
    try:
        route = resolve_route(row["assignee"])
    except (OSError, ValueError) as exc:
        logger.warning("Cannot resolve human-gate route for %s: %s", task_id, exc)
        return
    if route is None:
        logger.warning("Human gate %s has no configured notification route", task_id)
        return
    profile, platform, chat = route
    conn.execute(
        "INSERT OR IGNORE INTO kanban_notify_subs "
        "(task_id, platform, chat_id, thread_id, chat_type, notifier_profile, delivery_mode, created_at, last_event_id) "
        "VALUES (?, ?, ?, '', 'dm', ?, 'notify', strftime('%s','now'), ?)",
        (task_id, platform, chat, profile, event_id - 1),
    )


def append_event(conn, task_id, kind, payload=None, *, run_id=None):
    """Record one event and subscribe human gates within the same transaction."""
    from hermes_cli import kanban_db as kb
    cursor = conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, kind, kb._json_or_null(payload), int(time.time())),
    )
    if kind == "blocked":
        subscribe_block(conn, task_id, payload or {}, cursor.lastrowid)
