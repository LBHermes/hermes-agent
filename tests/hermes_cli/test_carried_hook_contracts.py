"""Callback declaration, identity and bounded recovery controls."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_cli import plugins_dispatch as dispatch


def context():
    manager = PluginManager()
    return manager, PluginContext(PluginManifest(name="screen-test", version="1"), manager)


@pytest.mark.parametrize("identified", [False, True], ids=["anonymous", "identified"])
def test_concurrent_calls_do_not_drop_screening(identified, monkeypatch):
    manager, ctx = context()
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def screen(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return "screened"
    ctx.register_hook("transform_tool_result", screen)
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 10)
    first = {"tool_call_id": "first"} if identified else {}
    second = {"tool_call_id": "second"} if identified else {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(manager.invoke_hook, "transform_tool_result", **first)
        try:
            assert entered.wait(5)
            assert manager.invoke_hook("transform_tool_result", **second) == ["screened"]
        finally:
            release.set()
        assert future.result(5) == ["screened"]


@pytest.mark.parametrize("declared", [False, True])
def test_callback_declaration_withholds_on_skip(declared, monkeypatch):
    manager, ctx = context()
    def screen(**kwargs):
        return "screened"
    ctx.register_hook("transform_tool_result", screen, **({"fail_closed": True} if declared else {}))
    monkeypatch.setattr(manager, "_run_hook_callback_bounded", lambda *args: dispatch._HOOK_SKIPPED)
    result = manager.invoke_hook("transform_tool_result", result="PRIVATE", tool_name="read_file")
    if declared:
        assert "WITHHELD" in result[0]
        assert "PRIVATE" not in result[0]
    else:
        assert result == []


@pytest.mark.parametrize("declared", [False, True])
def test_transient_skip_retries_only_when_declared(declared, monkeypatch):
    manager, ctx = context()
    ctx.register_hook("transform_tool_result", lambda **k: "screened", **({"fail_closed": True} if declared else {}))
    calls = []
    def attempt(*args):
        calls.append(1)
        return dispatch._HOOK_SKIPPED if len(calls) == 1 else "screened"
    monkeypatch.setattr(manager, "_run_hook_callback_bounded", attempt)
    result = manager.invoke_hook("transform_tool_result", result="PRIVATE", tool_name="read_file")
    assert result == (["screened"] if declared else [])
    assert len(calls) == (2 if declared else 1)


@pytest.mark.parametrize("declared", [False, True])
def test_persistent_skip_is_bounded_and_audited(declared, monkeypatch, tmp_path):
    manager, ctx = context()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx.register_hook("transform_tool_result", lambda **k: None, **({"fail_closed": True} if declared else {}))
    calls = []
    def attempt(*args):
        calls.append(1)
        return dispatch._HOOK_SKIPPED
    monkeypatch.setattr(manager, "_run_hook_callback_bounded", attempt)
    result = manager.invoke_hook("transform_tool_result", result="PRIVATE", tool_name="read_file")
    ledger = tmp_path / "logs/hook_withholds.jsonl"
    if declared:
        assert len(calls) == 3
        assert "WITHHELD" in result[0]
        row = json.loads(ledger.read_text())
        assert row["attempts"] == 3
        assert row["bytes"] == 7
        assert "PRIVATE" not in ledger.read_text()
    else:
        assert len(calls) == 1
        assert not ledger.exists()


@pytest.mark.parametrize("release_requested", [False, True])
def test_operator_recovery_keeps_live_worker_accounting(release_requested, monkeypatch, tmp_path):
    from hermes_cli import plugins_fail_closed as policy
    manager, ctx = context()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(policy, "RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.02)
    entered = threading.Event()
    finish = threading.Event()
    calls = []
    def screen(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert finish.wait(5)
        return "screened"
    ctx.register_hook("transform_tool_result", screen, fail_closed=True)
    try:
        first = manager.invoke_hook("transform_tool_result", tool_call_id="first")
        assert entered.is_set()
        assert "WITHHELD" in first[0]
        marker = tmp_path / "hook_fail_closed_release"
        if release_requested:
            marker.touch()
        second = manager.invoke_hook("transform_tool_result", tool_call_id="second")
        assert second == (["screened"] if release_requested else first)
        assert len(calls) == (2 if release_requested else 1)
        assert sum(map(len, manager._hook_abandoned.values())) == 1
        assert not marker.exists()
        third = manager.invoke_hook("transform_tool_result", tool_call_id="third")
        assert third == (["screened"] if release_requested else first)
        assert len(calls) == (3 if release_requested else 1)
    finally:
        finish.set()


@pytest.mark.parametrize("ledger_broken", [False, True])
def test_audit_failure_never_releases_payload(ledger_broken, monkeypatch, tmp_path):
    manager, ctx = context()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if ledger_broken:
        (tmp_path / "logs").write_text("cannot be a directory")
    ctx.register_hook("transform_tool_result", lambda **k: None, fail_closed=True)
    monkeypatch.setattr(manager, "_run_hook_callback_bounded", lambda *a: dispatch._HOOK_SKIPPED)
    result = manager.invoke_hook("transform_tool_result", result="PRIVATE")
    assert "WITHHELD" in result[0]
    assert "PRIVATE" not in result[0]


@pytest.mark.parametrize("declared", [False, True])
def test_async_screening_errors_fail_closed(declared):
    import asyncio
    manager, ctx = context()
    async def screen(**kwargs):
        raise RuntimeError("screen unavailable")
    ctx.register_hook("transform_tool_result", screen, **({"fail_closed": True} if declared else {}))
    result = asyncio.run(manager.ainvoke_hook("transform_tool_result", result="PRIVATE"))
    if declared:
        assert "WITHHELD" in result[0]
        assert "PRIVATE" not in result[0]
    else:
        assert result == []


def test_disposing_declared_registration_removes_withholding(monkeypatch):
    manager, ctx = context()
    handle = ctx.register_hook("transform_tool_result", lambda **k: None, fail_closed=True)
    monkeypatch.setattr(manager, "_run_hook_callback_bounded", lambda *a: dispatch._HOOK_SKIPPED)
    assert "WITHHELD" in manager.invoke_hook("transform_tool_result")[0]
    handle.dispose()
    assert manager.invoke_hook("transform_tool_result") == []
