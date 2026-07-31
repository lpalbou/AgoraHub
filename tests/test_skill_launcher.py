from pathlib import Path

import pytest

from agora.skill import agora_protocol


def test_skill_launcher_execs_native_drive_and_forwards_arguments(monkeypatch):
    seen = {}
    monkeypatch.setattr(agora_protocol.shutil, "which",
                        lambda name: "/opt/bin/agora" if name == "agora" else None)
    monkeypatch.setattr(agora_protocol.sys, "argv", [
        "agora_protocol.py", "--harness", "codex", "--turn-log",
    ])

    def fake_execv(path, argv):
        seen.update(path=path, argv=argv)
        raise RuntimeError("exec boundary")

    monkeypatch.setattr(agora_protocol.os, "execv", fake_execv)
    with pytest.raises(RuntimeError, match="exec boundary"):
        agora_protocol.main()
    assert seen == {
        "path": "/opt/bin/agora",
        "argv": ["/opt/bin/agora", "drive", "--harness", "codex",
                 "--turn-log"],
    }


def test_skill_launcher_fails_actionably_without_native_agora(monkeypatch,
                                                               capsys):
    monkeypatch.setattr(agora_protocol.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc:
        agora_protocol.main()
    assert exc.value.code == 127
    error = capsys.readouterr().err
    assert "reason=agora-not-found" in error
    assert "uv tool install --force --reinstall agorahub" in error


def test_skill_launcher_contains_no_alternate_reception_engine():
    source = Path(agora_protocol.__file__).read_text()
    for forbidden in (
        "subprocess", "urllib", "cursor-agent", "agora listen",
        "inline_loop", "owed_signature", "AGORA_PROTOCOL_MODEL",
    ):
        assert forbidden not in source
