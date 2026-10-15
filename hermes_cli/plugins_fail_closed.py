"""Opt-in screening callbacks: declarations belong to registration lifetimes."""
from functools import wraps
import asyncio
import inspect
import json
import logging
import time
from hermes_constants import get_hermes_home



def register_hook(context, hook_name, callback, *, fail_closed=False):
    from hermes_cli.plugins import VALID_HOOKS
    if fail_closed:
        if hook_name not in {"transform_tool_result", "pre_tool_call"}:
            raise ValueError(f"Hook {hook_name!r} has no fail-closed result contract")
        original = callback
        @wraps(original)
        def callback(**kwargs):
            return original(**kwargs)
        callback._hermes_fail_closed = True
    return context._track_callback("hook", hook_name, callback, context._manager._hooks, VALID_HOOKS)


def declared(callback):
    return getattr(callback, "_hermes_fail_closed", False) is True


def withhold():
    return "[TOOL RESULT WITHHELD] A fail-closed screening callback did not complete. Retry after repairing the callback."

logger = logging.getLogger(__name__)
RETRY_ATTEMPTS = 2
RETRY_BUDGET_SECONDS = 45.0
RETRY_WAIT_SECONDS = 5.0


def _consume_release(manager, key):
    marker = get_hermes_home() / "hook_fail_closed_release"
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Cannot consume hook recovery request: %s", exc)
        return False
    with manager._hook_timeout_lock:
        # Keep live worker accounting intact: recovery must not bypass the thread cap.
        manager._hook_timeout_suppressed_until.pop(key, None)
        released = getattr(manager, "_hook_released_workers", None)
        if released is None:
            released = manager._hook_released_workers = {}
        released[key] = set(manager._hook_abandoned.get(key, ()))
    return True


def _wait_for_workers(manager, key, deadline):
    until = min(deadline, time.monotonic() + RETRY_WAIT_SECONDS)
    while True:
        with manager._hook_timeout_lock:
            abandoned = manager._hook_abandoned.get(key, set())
            released = getattr(manager, "_hook_released_workers", {})
            ignored = released.get(key, set())
            ignored.intersection_update(abandoned)
            if not ignored:
                released.pop(key, None)
            if not abandoned - ignored:
                manager._hook_timeout_suppressed_until.pop(key, None)
                return True
        if time.monotonic() >= until:
            return False
        time.sleep(min(0.05, max(0, until - time.monotonic())))


def _record_withhold(hook_name, kwargs, attempts, reason):
    payload = kwargs.get("result", "")
    row = {"hook": hook_name, "tool": kwargs.get("tool_name"),
           "bytes": len(str(payload).encode("utf-8")), "attempts": attempts,
           "reason": reason, "time": time.time()}
    logger.warning("Screening result withheld: tool=%s attempts=%s reason=%s", row["tool"], attempts, reason)
    try:
        ledger = get_hermes_home() / "logs" / "hook_withholds.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
    except OSError as exc:
        logger.warning("Cannot record hook withhold: %s", exc)
    return withhold()


def run_screen(manager, hook_name, callback, kwargs, timeout):
    from hermes_cli.plugins_dispatch import _HOOK_SKIPPED
    key = (hook_name, id(callback))
    released = _consume_release(manager, key)
    deadline = time.monotonic() + RETRY_BUDGET_SECONDS
    attempts = 0
    reason = "skipped"
    for index in range(RETRY_ATTEMPTS + 1):
        if index or not released:
            if not _wait_for_workers(manager, key, deadline):
                reason = "worker_still_running"
                break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "retry_budget"
            break
        attempts += 1
        try:
            result = manager._run_hook_callback_bounded(hook_name, callback, kwargs, min(timeout, remaining))
            if result is not _HOOK_SKIPPED:
                return result, False
        except (Exception, SystemExit):  # health: allow BLE001 -- plugin isolation boundary; exception text may contain withheld payload
            reason = "callback_error"
    return _record_withhold(hook_name, kwargs, attempts, reason), True


async def arun_screen(manager, hook_name, callback, kwargs, timeout):
    if not inspect.iscoroutinefunction(inspect.unwrap(callback)):
        return await asyncio.to_thread(run_screen, manager, hook_name, callback, kwargs, timeout)
    deadline = time.monotonic() + RETRY_BUDGET_SECONDS
    attempts = 0
    for _ in range(RETRY_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempts += 1
        try:
            result = callback(**manager._hook_callback_kwargs(callback, kwargs))
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, min(timeout, remaining))
            return result, False
        except (Exception, SystemExit):  # health: allow BLE001 -- plugin isolation boundary; exception text may contain withheld payload
            continue
    return _record_withhold(hook_name, kwargs, attempts, "callback_error_or_timeout"), True
