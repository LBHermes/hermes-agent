"""RPC transport keeps screening notices intact without hiding transport failures."""
import json
from unittest.mock import Mock

import pytest

from tools.code_execution_tool import generate_hermes_tools_module


@pytest.mark.parametrize("transport", ["uds", "file"])
@pytest.mark.parametrize("encoded", [False, True], ids=["plain", "json"])
def test_screening_notice_roundtrip(transport, encoded, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_RPC_DIR", str(tmp_path))
    namespace = {}
    exec(generate_hermes_tools_module(["read_file"], transport), namespace)
    notice = "  WITHHELD: content requires review  "
    body = json.dumps(notice) if encoded else notice
    if transport == "file":
        (tmp_path / "res_000001").write_text(body)
    else:
        conn = Mock()
        conn.recv.return_value = (body + "\n").encode()
        namespace["_connect"] = lambda: conn
    assert namespace["read_file"]("secret") == notice


def test_disconnect_still_raises(monkeypatch):
    monkeypatch.delenv("HERMES_RPC_PERSISTENT", raising=False)
    namespace = {}
    exec(generate_hermes_tools_module(["read_file"]), namespace)
    conn = Mock()
    conn.recv.return_value = b""
    namespace["_connect"] = lambda: conn
    with pytest.raises(RuntimeError, match="disconnected"):
        namespace["read_file"]("secret")


def test_file_timeout_still_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_RPC_DIR", str(tmp_path))
    namespace = {}
    exec(generate_hermes_tools_module(["read_file"], "file"), namespace)
    ticks = iter([0, 301])
    from types import SimpleNamespace
    namespace["time"] = SimpleNamespace(monotonic=lambda: next(ticks))
    with pytest.raises(RuntimeError, match="RPC timeout"):
        namespace["read_file"]("secret")
