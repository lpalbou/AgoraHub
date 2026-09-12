"""A structural declaration must never masquerade as a live verification."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import agora.harness_check as hc


@pytest.mark.parametrize("live,binary_present,native_status,expected_status,exit_code", [
    (False, True, hc.PASS, hc.SKIP, 0),
    (True, True, hc.PASS, hc.PASS, 0),
    (True, True, hc.FAIL, hc.FAIL, 1),
    (True, False, hc.PASS, hc.SKIP, 1),
])
def test_live_verdict_and_exit_status(tmp_path, monkeypatch, live, binary_present,
                                      native_status, expected_status, exit_code):
    """All harness operations are simulated, including the explicitly live check."""
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    monkeypatch.setattr(hc.shutil, "which", lambda _: "/simulated/codex" if binary_present else None)
    monkeypatch.setattr(hc.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr("agora.mcp.runtime.resolve_mcp_command", lambda: "agora-mcp")
    monkeypatch.setattr("agora.mcp.runtime.probe_mcp_runtime", lambda _:
                        SimpleNamespace(ok=True, agora_version="test", sdk_version="test"))
    monkeypatch.setattr("agora.drive._make_adapter", lambda *a, **kw: SimpleNamespace())
    monkeypatch.setattr(hc, "_surface", lambda *a: "agora-mcp seat")
    monkeypatch.setattr(hc, "_knob_probe", lambda *a:
                        hc.Probe("C8", "knobs", hc.PASS, "simulated"))
    native = Mock(return_value=hc.Probe("C9", "live-turn", native_status, "simulated"))
    monkeypatch.setattr(hc, "_live_probe", native)

    report = hc.run_check("codex", workspace=tmp_path, agent_id="seat",
                          url="http://unused.invalid", live=live)

    assert native.call_count == int(live and binary_present)
    assert report.exit_code() == exit_code
    payload = json.loads(report.to_json())
    assert payload["drivable"] is binary_present  # existing structural meaning
    assert payload["live_status"] == expected_status
    probes = {p.capability: p for p in report.probes}
    # Even a successful identity check cannot verify event formats or resume.
    for capability in ("evidence", "continuity"):
        assert probes[capability].status == hc.WARN
        assert "declared:" in probes[capability].detail
    headline = next(line for line in report.render().splitlines() if line.startswith("VERDICT:"))
    if expected_status == hc.PASS:
        assert "live identity check passed" in headline
    else:
        assert "NOT LIVE-VERIFIED" in headline
        assert ("failed" if expected_status == hc.FAIL else "skipped") in headline


def test_absent_live_probe_is_not_verification():
    """A partial report must not turn missing evidence into success."""
    report = hc.Report("synthetic")
    assert json.loads(report.to_json())["live_status"] == hc.SKIP
    assert "NOT LIVE-VERIFIED" in report.render()
