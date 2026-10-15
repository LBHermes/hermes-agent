"""Same transcript request and exception message, different failure type."""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest


@pytest.mark.parametrize("blocked", [False, True], ids=["video", "host"])
@pytest.mark.parametrize("text_only", [False, True], ids=["json", "text"])
def test_failure_attribution(blocked, text_only, monkeypatch, capsys):
    path = Path(__file__).parents[2] / "skills/media/youtube-content/scripts/fetch_transcript.py"
    spec = importlib.util.spec_from_file_location("transcript_helper", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    package = ModuleType("youtube_transcript_api")
    errors = ModuleType("youtube_transcript_api._errors")
    class RequestBlocked(Exception):
        pass
    errors.RequestBlocked = RequestBlocked
    errors.IpBlocked = type("IpBlocked", (RequestBlocked,), {})
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", package)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api._errors", errors)
    error = RequestBlocked if blocked else ValueError
    def fetch(*args):
        raise error("identical failure message")
    monkeypatch.setattr(helper, "fetch_transcript", fetch)
    monkeypatch.setattr(sys, "argv", [str(path), "abcdefghijk"] + (["--text-only"] if text_only else []))
    with pytest.raises(SystemExit) as exited:
        helper.main()
    captured = capsys.readouterr()
    assert exited.value.code == (3 if blocked else 1)
    if blocked:
        output = captured.err if text_only else captured.out
        assert "youtube_host_block" in output
        if not text_only:
            assert json.loads(output)["attributed_to"] == "host"
    else:
        assert "youtube_host_block" not in captured.out + captured.err
