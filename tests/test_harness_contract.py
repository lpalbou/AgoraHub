"""The harness contract: agora describes frameworks, it does not embed them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agora.drive import (_DRIVE_ADAPTERS, _HARD_CAPABILITIES,
                         AbstractCodeTuiDriveAdapter, CodexDriveAdapter,
                         run_drive)
from agora.harness_check import run_check
from agora.mcp.runtime import MCPBinding
from agora.setup_harness import SUPPORTED_HARNESSES


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    return tmp_path


def test_every_declared_harness_answers_the_contract():
    """A harness is DATA: adding a framework declares capabilities rather than
    adding branches to agora's generic code."""
    assert set(_DRIVE_ADAPTERS) == set(SUPPORTED_HARNESSES)
    for name, cls in _DRIVE_ADAPTERS.items():
        assert cls.name == name
        assert isinstance(cls.SUPPORTS, frozenset) and "session" in cls.SUPPORTS
        assert cls.CONTINUITY in ("resume-id", "state-file", None)
        assert isinstance(cls.PROBE_ARGV, tuple) and cls.PROBE_ARGV
        for capability in cls.UNMET:          # declared in the CONTRACT's words
            assert capability in _HARD_CAPABILITIES + ("evidence", "continuity",
                                                       "sandbox", "model")
        if "reasoning" in cls.SUPPORTS:
            assert cls.REASONING_VOCAB, f"{name} claims reasoning, declares none"


def test_an_unmet_hard_capability_is_refused_without_naming_a_vendor(home):
    """The refusal must be as useful to the fifth framework as to the first.

    Uses a synthetic harness: every REAL one currently meets the hard set, and
    a test that only passes because some vendor is broken would silently stop
    testing anything the day they fix it.
    """
    class Partial(CodexDriveAdapter):
        name = "partial"
        UNMET = ("single-turn", "tool-reach")

    _DRIVE_ADAPTERS["partial"] = Partial
    try:
        with pytest.raises(SystemExit) as excinfo:
            run_drive(agent_id="w", url="http://hub:1", harness="partial",
                      cwd=home, once=True)
        detail = str(excinfo.value)
        assert "does not meet the agora harness contract" in detail
        assert "unmet: single-turn, tool-reach" in detail   # contract's words
        assert "harness-check partial" in detail            # where to get detail
        for leak in ("gateway", "ABSTRACT_ENABLE", "_runtime.", "basic-agent"):
            assert leak not in detail
    finally:
        del _DRIVE_ADAPTERS["partial"]


def test_process_scoped_identity_is_declared_and_drivable(home):
    """AbstractCode-TUI was verified driving a real hub turn on 2026-07-30, so
    declaring `single-turn`/`tool-reach` unmet was agora guessing wrong about
    another framework. What it genuinely cannot do is scope identity per turn —
    a DEGRADED capability, warned about loudly, not a refusal."""
    cls = AbstractCodeTuiDriveAdapter
    assert cls.IDENTITY_SCOPE == "process"
    assert "single-turn" not in cls.UNMET and "tool-reach" not in cls.UNMET
    assert cls.UNMET == ("evidence",)
    assert cls.CONTINUITY == "resume-id"        # `--session`, caller-chosen


def test_permission_requirement_is_a_vocabulary_not_a_branch(home):
    """`if harness == "codex"` in generic validation is the shape being removed.

    0.12.60: the codex-shaped `--sandbox` tri-state became the `permissions`
    vocabulary. Codex declares ("write",) — the same declaration mechanism as
    REASONING_VOCAB now carries what REQUIRES_SANDBOX used to hardcode, and the
    legacy `--sandbox disabled` alias maps to `all` and is refused by it.
    """
    assert CodexDriveAdapter.PERMISSION_VOCAB == ("write",)
    with pytest.raises(SystemExit) as excinfo:
        run_drive(agent_id="w", url="http://hub:1", harness="codex",
                  sandbox="disabled", cwd=home, once=True)
    detail = str(excinfo.value)
    assert "accepts --permissions write" in detail
    assert "bypass MCP" in detail          # the WHY survives the migration


def test_harness_check_reports_per_capability_and_names_every_degrade(home):
    report = run_check("abstractcode-tui", workspace=home, agent_id="seat",
                       url="http://hub:1")
    rendered = report.render()
    # A degraded capability is NAMED, never silent.
    assert any("evidence=exit-code-only" in (p.limitation or "")
               for p in report.probes), rendered
    payload = json.loads(report.to_json())
    assert payload["probes"] and "drivable" in payload


def test_harness_check_never_reports_a_bearer_as_acceptable(home, monkeypatch):
    """C5 must fail loudly if a credential reaches the harness surface."""
    import agora.harness_check as hc

    monkeypatch.setattr(hc, "_surface",
                        lambda *a, **k: "agora_" + "a" * 40)
    report = run_check("codex", workspace=home, agent_id="seat",
                       url="http://hub:1")
    identity = next(p for p in report.probes if p.capability == "identity")
    assert identity.status == "FAIL"
    assert "bearer" in identity.detail


def test_knob_probe_catches_a_knob_accepted_and_silently_dropped(home):
    """The failure this exists for: a seat that arms healthy and then answers
    the hub with the wrong brain."""
    class Sloppy(CodexDriveAdapter):
        name = "sloppy"

        def build_command(self, prompt, session_id):
            return [self.binary, "exec", prompt]      # ignores EVERY knob

    _DRIVE_ADAPTERS["sloppy"] = Sloppy
    try:
        report = run_check("sloppy", workspace=home, agent_id="seat",
                           url="http://hub:1")
        knobs = next(p for p in report.probes if p.capability == "knobs")
        assert knobs.status == "FAIL"
        assert "ACCEPTED AND DROPPED" in knobs.detail
    finally:
        del _DRIVE_ADAPTERS["sloppy"]


def test_operator_supplies_framework_specific_args_agora_has_no_opinion_on(home):
    """`--harness-arg workflow=x` is how a framework's own concept reaches it
    WITHOUT agora growing a flag per vendor concept — the mechanism that keeps
    another product's internals out of this codebase."""
    adapter = AbstractCodeTuiDriveAdapter(
        model="m", permissions="write", cwd=home,
        mcp=MCPBinding(command="agora-mcp", agent_id="s", url="http://h:1",
                       home=home),
        harness_args={"workflow": "my-bundle:my-flow"})
    cmd = adapter.build_command("prompt", None)
    assert "--workflow" in cmd
    assert cmd[cmd.index("--workflow") + 1] == "my-bundle:my-flow"
    # agora never invents a value for it.
    bare = AbstractCodeTuiDriveAdapter(
        model="m", permissions="write", cwd=home,
        mcp=MCPBinding(command="agora-mcp", agent_id="s", url="http://h:1",
                       home=home))
    assert "--workflow" not in bare.build_command("prompt", None)


def test_contract_doc_exists_and_matches_the_code():
    """The refusal and setup messages point here; a dangling pointer is the
    'looks healthy, is mute' failure in documentation form."""
    doc = (Path(__file__).resolve().parents[1] / "docs" /
           "harness_contract.md").read_text()
    for capability in _HARD_CAPABILITIES:
        assert f"`{capability}`" in doc
    for probe in ("C1", "C4", "C5", "C8", "C9"):
        assert probe in doc
    assert "agora harness-check" in doc


def test_every_declared_knob_survives_the_knob_probe(home, monkeypatch):
    """The C8 probe, run over EVERY registered adapter, must be green.

    This is the single test that would have blocked 0.12.60's launch defect:
    opencode accepted `--reasoning-effort` and `--provider` and silently
    dropped both (the effort could only attach through a provider+model config
    entry it never derived), and pi dropped `--provider` when no model was
    named. A knob that is declared must reach the turn; a knob that cannot be
    expressed must be refused — never accepted-and-dropped.
    """
    from agora.drive import _DRIVE_ADAPTERS
    from agora.harness_check import PASS, _knob_probe
    from agora.mcp.runtime import MCPBinding

    monkeypatch.setenv("AGORA_HOME", str(home))
    binding = MCPBinding(command="agora-mcp", agent_id="probe",
                         url="http://hub:1", home=home)
    failures = {}
    for name, cls in _DRIVE_ADAPTERS.items():
        probe = _knob_probe(name, cls, binding, home)
        if probe.status != PASS:
            failures[name] = probe.detail
    assert failures == {}, failures
