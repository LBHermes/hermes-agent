"""Resolve a dispatch lane's skill requirements in the assignee's profile."""
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


def forced_review_skills(lane):
    return ["sdlc-review"] if lane == "review" else []


def missing_pins(assignee, skills):
    from hermes_cli.profiles import get_profile_dir
    from agent.skill_commands import _load_skill_payload, _disabled_skill_names
    home = get_profile_dir(assignee)
    token = set_hermes_home_override(home)
    try:
        disabled = _disabled_skill_names()
        missing = []
        for name in skills:
            loaded = _load_skill_payload(name)
            if loaded is None or name in disabled or loaded[2] in disabled:
                missing.append(name)
        return missing, str(home / "skills")
    finally:
        reset_hermes_home_override(token)


def refuse_missing_pins(conn, row, assignee, lane, *, dry_run):
    from hermes_cli import kanban_db as kb
    skills = list(dict.fromkeys([*(kb.get_task(conn, row["id"]).skills or []), *forced_review_skills(lane)]))
    if not skills:
        return False
    try:
        missing, searched = missing_pins(assignee, skills)
    except (OSError, ValueError) as exc:
        missing, searched = skills, str(exc)
    if not missing:
        return False
    if not dry_run:
        reason = f"Missing pinned skills {', '.join(missing)} for profile {assignee}; searched {searched}"
        if lane == "review":
            reason += "; review lane requires sdlc-review"
        with kb.write_txn(conn):
            # Pre-claim refusal creates no synthetic attempt; task_runs stays untouched.
            changed = conn.execute(
                "UPDATE tasks SET status='blocked', block_kind='capability' "
                "WHERE id=? AND status IN ('ready', 'review') AND claim_lock IS NULL", (row["id"],),
            ).rowcount
            if not changed:
                return True
            kb._append_event(conn, row["id"], "pinned_skill_missing", {"missing": missing, "assignee": assignee, "searched": searched})
            kb._append_event(conn, row["id"], "blocked", {"kind": "capability", "reason": reason})
    return True
