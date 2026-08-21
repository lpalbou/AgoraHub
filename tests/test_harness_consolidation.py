"""One workspace, several harnesses: no silent wrong contract, no missing skill.

Three defects found on 2026-08-21, each silent in a running fleet:

1. `--harness all` ran four writers at one `AGENTS.md` and kept whichever
   finished last (pi). A Codex seat then read "no idle wake, END your turn"
   while its own contract is a standing `wait_for_messages(45)` loop as its
   ONLY reachability — exact opposites, and the seat simply goes deaf.
2. `opencode` and `pi` had no `_SKILL_DIRS` entry, so `install_skill` skipped
   them and the protocol their rule file promised was never installed. Both
   harnesses read `~/.agents/skills/` (verified in opencode's own binary and
   pi's skills documentation).
3. The mission mirror ran from `agora drive` only, so an interactive seat had
   its mission nowhere but an erased tool result — and a workspace where the
   driver once ran kept a FROZEN copy under a heading calling it unsoftenable.
"""

from __future__ import annotations

from pathlib import Path

from agora.setup_harness import (
    _RULE_FILE,
    _SKILL_DIRS,
    _WAIT_CODEX_DEDICATED,
    expand_harness_selection,
    install_skill,
    upsert_marked_section,
    reconcile_shared_rule_file,
    rule_text,
    shared_rule_file_harnesses,
    write_mission_block,
)


# -- 1. the shared rule file ---------------------------------------------------


def test_harness_all_still_aims_four_writers_at_one_file():
    """The condition the reconciliation exists for. If this ever goes empty,
    the collision was designed out and the reconciler can go with it."""
    shared = shared_rule_file_harnesses(expand_harness_selection("all"))
    assert shared, "no shared rule file — did _RULE_FILE change?"
    assert set(shared["AGENTS.md"]) == {"codex", "abstractcode", "opencode", "pi"}


def test_a_shared_rule_file_never_keeps_one_harnesss_private_contract(tmp_path):
    """The bug, as a test: pi wrote last and a Codex seat inherited its ban.
    Now the file carries neither harness's private clause."""
    agents = tmp_path / "AGENTS.md"
    # Simulate the old last-writer-wins outcome: codex's dedicated contract.
    upsert_marked_section(agents, rule_text("seat", wake="codex wake", arming="",
                                            wait_policy=_WAIT_CODEX_DEDICATED))
    assert "wait_for_messages(45)` loop is the ONE sanctioned" in agents.read_text()

    notes = reconcile_shared_rule_file(tmp_path, "seat",
                                       ["codex", "opencode", "pi"])
    text = agents.read_text()
    assert notes and "shared by" in notes[0]
    assert "wait_for_messages(45)` loop is the ONE sanctioned" not in text, (
        "the seat must not be told to hold a loop the other harnesses ban")
    assert "NEVER wait or poll in the FOREGROUND" in text
    for harness in ("codex", "opencode", "pi"):
        assert harness in text, "the note must say what this workspace is"


def test_reconciliation_is_order_independent(tmp_path):
    """Deterministic: the same selection produces the same file whichever
    order the writers ran in."""
    a, b = tmp_path / "a", tmp_path / "b"
    for d, order in ((a, ["codex", "opencode", "pi"]), (b, ["pi", "codex", "opencode"])):
        d.mkdir()
        upsert_marked_section(d / "AGENTS.md",
                              rule_text("seat", wake="x", arming=""))
        reconcile_shared_rule_file(d, "seat", order)
    assert (a / "AGENTS.md").read_text() == (b / "AGENTS.md").read_text()


def test_a_single_harness_keeps_its_own_contract(tmp_path):
    """Wiring one harness alone is how a seat gets its specific reception
    contract — reconciliation must not touch it."""
    agents = tmp_path / "AGENTS.md"
    upsert_marked_section(agents, rule_text("seat", wake="codex wake", arming="",
                                            wait_policy=_WAIT_CODEX_DEDICATED))
    original = agents.read_text()
    assert reconcile_shared_rule_file(tmp_path, "seat", ["codex"]) == []
    assert agents.read_text() == original


# -- 2. every wired harness gets the skill ------------------------------------


def test_every_supported_harness_has_a_skill_directory():
    """A harness with no entry silently runs without the protocol its own
    rule file says it has."""
    missing = [h for h in _RULE_FILE if h not in _SKILL_DIRS]
    assert not missing, f"no skill directory for {missing}"


def test_opencode_and_pi_install_into_the_shared_agent_skills_dir(tmp_path):
    """Both read `~/.agents/skills/` — opencode auto-loads external skills
    from it, and it is the second entry in pi's own skills documentation."""
    for harness in ("opencode", "pi"):
        detail = install_skill(harness, home=tmp_path)
        assert "installed" in detail, detail
    installed = tmp_path / ".agents" / "skills" / "agora-channels" / "SKILL.md"
    assert installed.exists() and installed.read_text().strip()


# -- 3. the mission reaches the prompt, and never goes stale ------------------


def test_the_mission_block_is_replaced_not_appended(tmp_path):
    """A re-run must repair a stale charge, not stack a second one beside it."""
    upsert_marked_section(tmp_path / "AGENTS.md",
                          rule_text("seat", wake="x", arming=""))
    write_mission_block(tmp_path, "codex", "Own billing.")
    write_mission_block(tmp_path, "codex", "Own search.")
    text = (tmp_path / "AGENTS.md").read_text()
    assert text.count("agora:mission:begin") == 1
    assert "Own search." in text and "Own billing." not in text


def test_an_empty_mission_clears_the_block_rather_than_freezing_it(tmp_path):
    """A charge the operator revoked must not keep asserting itself."""
    upsert_marked_section(tmp_path / "AGENTS.md",
                          rule_text("seat", wake="x", arming=""))
    write_mission_block(tmp_path, "codex", "Own billing.")
    write_mission_block(tmp_path, "codex", "")
    assert "Own billing." not in (tmp_path / "AGENTS.md").read_text()
