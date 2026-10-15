"""The dispatcher-required review skill survives opt-in builtin pruning."""
import pytest
from tools import skill_usage


@pytest.mark.parametrize("name", ["ordinary-builtin", "sdlc-review"])
def test_archive_preserves_dispatch_requirement(tmp_path, monkeypatch, name):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    folder = tmp_path / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Test.\n---\nBody.\n")
    (tmp_path / "skills/.bundled_manifest").write_text(f"{name}:abc\n")
    monkeypatch.setattr(skill_usage, "_prune_builtins_enabled", lambda: True)
    archived, _ = skill_usage.archive_skill(name)
    assert archived == (name != "sdlc-review")
    assert folder.exists() == (name == "sdlc-review")
