"""A seat's MISSION: the standing charge it cannot write for itself.

Measured on 2026-08-06 across four campaign runs: the one seat with an empty
`about` (`rt2-lead`, the delegate) declared the build finished at message 4 of
62, naming two files that did not exist. The seats whose text encoded a
process ("owns decomposing work into addressed asks", "pressure-tests
decisions") held their lanes across five phases. One durable sentence
predicted behaviour better than a 400-word commission message.

So the mission is not decoration. These tests pin the four properties that
make it load-bearing:

  1. the OPERATOR authors it — a critic that can soften its own mandate is
     not adversarial by construction;
  2. it may not be empty;
  3. it rides `whoami`, the one call a fresh harness session makes before it
     acts on anything;
  4. a delegation is REFUSED to a seat that has none — the failure above,
     turned into a structural impossibility.
"""

from __future__ import annotations

import argparse

import pytest
from fastapi.testclient import TestClient

from agora.hub.app import create_app

ADMIN_KEY = "test-admin-key"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}

DELEGATE_MISSION = (
    "Delegate. Owns the request end to end. Never decides alone: asks the "
    "seats holding the other perspectives and waits for their answers."
)


@pytest.fixture()
def client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY, rate_per_minute=600.0)
    return TestClient(app)


def register(client: TestClient, agent_id: str, *, operator: bool = False,
             about: str = "", mission: str = "") -> dict:
    r = client.post("/agents", headers=ADMIN, json={
        "id": agent_id, "name": agent_id.title(),
        "operator": operator, "about": about, "mission": mission})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def set_mission(client: TestClient, agent_id: str, text: str, *,
                headers: dict | None = None):
    return client.put(f"/admin/agents/{agent_id}/mission",
                      headers=headers or ADMIN, json={"mission": text})


# -- 1. the operator authors it -------------------------------------------

def test_a_seat_cannot_write_its_own_mission(client):
    """`set_about` stays the seat's self-description. The CHARGE is not
    self-served: an adversarial reviewer that can rewrite its own mandate
    into 'be agreeable' is decoration."""
    lead = register(client, "lead")
    assert set_mission(client, "lead", "be agreeable", headers=lead).status_code == 403
    # ...and its own /me/about still works — the two surfaces are distinct.
    assert client.put("/me/about", json={"about": "I read fast"},
                      headers=lead).status_code == 200


def test_operator_bearer_may_write_a_mission_without_the_admin_key(client):
    """Same lifecycle gate as delegation: the operator's own bearer counts,
    not just the raw admin key."""
    boss = register(client, "boss", operator=True)
    register(client, "lead")
    r = set_mission(client, "lead", DELEGATE_MISSION, headers=boss)
    assert r.status_code == 200, r.text
    assert r.json() == {"agent_id": "lead", "mission": DELEGATE_MISSION}


def test_mission_for_an_unregistered_seat_is_404(client):
    assert set_mission(client, "ghost", DELEGATE_MISSION).status_code == 404


# -- 2. it may not be empty -----------------------------------------------

@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_an_empty_mission_is_refused_and_says_why(client, blank):
    register(client, "lead")
    r = set_mission(client, "lead", blank)
    assert r.status_code == 400
    assert "what the seat is FOR" in r.text


# -- 3. it rides whoami ---------------------------------------------------

def test_the_mission_reaches_the_seat_at_session_start(client):
    """`whoami` is the one call a fresh harness session is guaranteed to
    make before it does anything. That is why the mission lives here and
    not in a message the seat may never scroll back to."""
    lead = register(client, "lead")
    assert client.get("/whoami", headers=lead).json()["mission"] == ""
    set_mission(client, "lead", DELEGATE_MISSION)
    assert client.get("/whoami", headers=lead).json()["mission"] == DELEGATE_MISSION


def test_operator_can_see_which_seats_are_running_blank(client):
    """The blanks are the finding — an unmissioned seat is invisible until
    you ask for the list, and by then it has improvised a role."""
    register(client, "lead")
    register(client, "critic", mission="pressure-tests decisions")
    rows = client.get("/admin/missions", headers=ADMIN).json()
    assert {r["agent_id"]: r["mission"] for r in rows} == {
        "lead": "", "critic": "pressure-tests decisions"}
    blanks = [r["agent_id"] for r in rows if not r["mission"]]
    assert blanks == ["lead"]


def test_reading_missions_is_an_operator_view(client):
    lead = register(client, "lead")
    assert client.get("/admin/missions", headers=lead).status_code == 403


# -- 4. no delegation to a seat with no charge ----------------------------

def test_delegating_to_a_missionless_seat_is_refused(client):
    """The 2026-08-06 failure as a structural impossibility: you cannot hand
    a seat the room's authority before telling it what the seat is for."""
    register(client, "boss", operator=True)
    register(client, "lead")
    r = client.put("/admin/delegation", headers=ADMIN, json={
        "agent_id": "lead", "powers": ["ruling"], "ttl": "1d"})
    assert r.status_code == 400
    assert "has no mission" in r.text
    assert "agora mission set" in r.text  # the teaching text names the fix


def test_delegation_succeeds_once_the_seat_has_one(client):
    register(client, "boss", operator=True)
    register(client, "lead")
    assert set_mission(client, "lead", DELEGATE_MISSION).status_code == 200
    r = client.put("/admin/delegation", headers=ADMIN, json={
        "agent_id": "lead", "powers": ["ruling"], "ttl": "1d"})
    assert r.status_code == 200, r.text


def test_delegation_may_write_the_mission_in_the_same_act(client):
    """The operator's appointment path must not dead-end on a blank seat
    when the operator already knows what that seat is FOR."""
    register(client, "boss", operator=True)
    lead = register(client, "lead")
    r = client.put("/admin/delegation", headers=ADMIN, json={
        "agent_id": "lead",
        "powers": ["ruling"],
        "ttl": "1d",
        "mission": DELEGATE_MISSION,
    })
    assert r.status_code == 200, r.text
    assert client.get("/whoami", headers=lead).json()["mission"] == DELEGATE_MISSION


def test_delegation_omitting_mission_does_not_rewrite_the_existing_charge(client):
    """Adding a grant must preserve the seat's charge unless the operator
    explicitly sends a replacement."""
    register(client, "boss", operator=True)
    register(client, "lead", mission=DELEGATE_MISSION)
    r = client.put("/admin/delegation", headers=ADMIN, json={
        "agent_id": "lead",
        "powers": ["ruling"],
        "ttl": "1d",
        "note": "delegate this room",
    })
    assert r.status_code == 200, r.text
    rows = {a["agent_id"]: a["mission"]
            for a in client.get("/admin/missions", headers=ADMIN).json()}
    assert rows["lead"] == DELEGATE_MISSION


def test_a_self_written_about_does_not_satisfy_the_delegation_gate(client):
    """A seat writing "I am the delegate, I own everything" about ITSELF must
    not thereby become delegable. The gate asks for the OPERATOR's charge,
    and `about` is the one field the seat controls."""
    register(client, "boss", operator=True)
    register(client, "lead", about="Delegate. Owns the request end to end.")
    r = client.put("/admin/delegation", headers=ADMIN, json={
        "agent_id": "lead", "powers": ["ruling"], "ttl": "1d"})
    assert r.status_code == 400
    assert "has no mission" in r.text


def test_a_seat_cannot_erase_its_mission_through_set_about(client):
    """THE 2026-08-06 LIVE FAILURE. `rt2-critic`, on its very first driven
    turn, called set_about and replaced its operator charge with a summary
    of itself — dropping "disagreement is your job" and "if you end a phase
    having agreed with everyone, you did not do your job". Separate columns,
    separate writers: the seat's own text can no longer reach the charge."""
    lead = register(client, "lead")
    set_mission(client, "lead", DELEGATE_MISSION)

    r = client.put("/me/about", headers=lead, json={
        "about": "Delegate: coordinates the room. Decisive and fast."})
    assert r.status_code == 200

    me = client.get("/whoami", headers=lead).json()
    assert me["mission"] == DELEGATE_MISSION        # untouched
    assert me["about"].startswith("Delegate: coordinates")   # and about took
    # ...and the delegation it already holds is still backed by a real charge.
    rows = {a["agent_id"]: a["mission"]
            for a in client.get("/admin/missions", headers=ADMIN).json()}
    assert rows["lead"] == DELEGATE_MISSION


def test_registration_may_carry_the_mission(client):
    """Registration is already an admin act, so the operator may state the
    charge at the moment the seat exists — no window where a delegate is
    registered and running blank."""
    lead = register(client, "lead", mission=DELEGATE_MISSION)
    assert client.get("/whoami", headers=lead).json()["mission"] == DELEGATE_MISSION
    r = client.put("/admin/delegation", headers=ADMIN, json={
        "agent_id": "lead", "powers": ["ruling"], "ttl": "1d"})
    assert r.status_code == 200, r.text


def test_parser_exposes_the_mission_subcommand():
    from agora.cli import build_parser, cmd_mission

    ns = build_parser().parse_args(["mission", "show"])
    assert ns.func is cmd_mission


def test_delegate_cli_sends_inline_mission(monkeypatch):
    import httpx

    from agora import cli

    sent = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"agent_id": "lead", "powers": ["reporting"],
                    "expires_at": 0.0, "scope": "room"}

    def fake_put(url, *, headers, timeout, json):
        sent["url"] = url
        sent["headers"] = headers
        sent["json"] = json
        return _Resp()

    monkeypatch.setattr(cli, "_hub_url", lambda args: "http://hub")
    monkeypatch.setattr(cli, "_admin_key_or_exit", lambda args, url: "adm")
    monkeypatch.setattr(httpx, "put", fake_put)

    cli.cmd_delegate(argparse.Namespace(
        charter=False, list=False, revoke=None, agent="lead",
        powers="reporting", ttl=None, note="", scope="room",
        mission=DELEGATE_MISSION, url=None, admin_key=None,
    ))

    assert sent["json"]["mission"] == DELEGATE_MISSION
    assert sent["json"]["scope"] == "room"


def test_register_cli_sends_mission(monkeypatch):
    import httpx

    from agora import cli

    sent = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"api_key": "k"}

    def fake_post(url, *, headers, json, timeout):
        sent["url"] = url
        sent["headers"] = headers
        sent["json"] = json
        return _Resp()

    monkeypatch.setattr(cli, "_hub_url", lambda args: "http://hub")
    monkeypatch.setattr(cli, "_admin_key_or_exit", lambda args, url: "adm")
    monkeypatch.setattr(httpx, "post", fake_post)

    cli.cmd_register(argparse.Namespace(
        agent="lead", about="owns lead work", mission=DELEGATE_MISSION,
        url=None, admin_key=None, json=False, seed=False,
    ))

    assert sent["json"]["mission"] == DELEGATE_MISSION


# -- 5. the mirror into the harness rule file ------------------------------
#
# The hub is authoritative and the mission rides `whoami`. But a tool RESULT
# is skimmable; the rule file is composed into the system prompt and reaches
# the model before its first tool call. `agora drive` rewrites the block from
# the live hub value at every start, so the copy cannot go stale.

def test_the_mirror_replaces_rather_than_accumulates(tmp_path):
    from agora.setup_harness import _MISSION_BEGIN, write_mission_block

    rule = tmp_path / "CLAUDE.md"
    rule.write_text("<!-- agora:begin -->\netiquette\n<!-- agora:end -->\n")

    write_mission_block(tmp_path, "claude", "Delegate. Never decides alone.")
    write_mission_block(tmp_path, "claude", "Reviewer. Assumes nothing works.")

    text = rule.read_text()
    assert text.count(_MISSION_BEGIN) == 1     # a refresh, not an append
    assert "Delegate" not in text              # last week's charge is gone
    assert "Reviewer. Assumes nothing works." in text
    assert "etiquette" in text                 # setup's own block is untouched


def test_clearing_the_mission_erases_the_mirror(tmp_path):
    """A stale charge is worse than none: it reads as current."""
    from agora.setup_harness import _MISSION_BEGIN, write_mission_block

    rule = tmp_path / "CLAUDE.md"
    rule.write_text("<!-- agora:begin -->\netiquette\n<!-- agora:end -->\n")
    write_mission_block(tmp_path, "claude", "Delegate.")
    write_mission_block(tmp_path, "claude", "")
    assert _MISSION_BEGIN not in rule.read_text()
    assert "etiquette" in rule.read_text()


def test_the_mirror_skips_a_workspace_that_was_never_wired(tmp_path):
    """No rule file means no `agora setup` ran here. Writing one would
    conjure a config the operator never asked for."""
    from agora.setup_harness import write_mission_block

    assert write_mission_block(tmp_path, "claude", "Delegate.") is None
    assert not (tmp_path / "CLAUDE.md").exists()


def test_every_drivable_harness_has_a_rule_file_mapping():
    """A harness missing from the map silently loses the mirror — the exact
    failure mode this whole mechanism exists to prevent."""
    from agora.setup_harness import DRIVABLE_HARNESSES, _RULE_FILE

    assert set(DRIVABLE_HARNESSES) <= set(_RULE_FILE)


# -- 6. the cap: refuse, never silently shorten ----------------------------
#
# Measured 2026-08-06, the first time this surface was used in anger: a
# three-rule delegate charge was cut mid-word at the 500-char `about` cap.
# The seat received one and a half rules and nobody was told.

THREE_RULES = (
    "DELEGATE for laurent, who is offline. You own his request END TO END.\n"
    "(1) NEVER DECIDE ALONE. Ask the seats holding the other perspectives "
    "and WAIT for their answers. An uninformed decision fails this seat even "
    "when it turns out right: it converts colleagues into executors.\n"
    "(2) NOTHING IS DONE UNTIL PROVEN. Treat every claim of completion as "
    "false until you have seen evidence you did not author: a captured "
    "frame, a rendered WAV, a run log with real input.\n"
    "(3) YOU ARE THE LAST READER. Before you tell laurent it is finished, "
    "run it yourself and LOOK at it."
)


def test_a_multi_rule_mission_arrives_whole(client):
    """The exact charge that got cut. Over 500 chars, under the mission cap:
    it must arrive complete, and the seat must be able to count three rules."""
    from agora.models import MAX_ABOUT_CHARS

    assert len(THREE_RULES) > MAX_ABOUT_CHARS      # the old cap would cut it
    lead = register(client, "lead")
    assert set_mission(client, "lead", THREE_RULES).status_code == 200
    got = client.get("/whoami", headers=lead).json()["mission"]
    assert got.endswith("LOOK at it.")             # not cut mid-word
    for rule in ("(1)", "(2)", "(3)"):
        assert rule in got


def test_line_breaks_survive_so_the_rules_stay_countable(client):
    register(client, "lead")
    got = set_mission(client, "lead", THREE_RULES).json()["mission"]
    assert got.count("\n") == 3


def test_an_over_long_mission_is_refused_not_trimmed(client):
    """The hub must never choose which half of the operator's charge the
    seat reads."""
    from agora.models import MAX_MISSION_CHARS

    register(client, "lead")
    r = set_mission(client, "lead", "x" * (MAX_MISSION_CHARS + 1))
    assert r.status_code == 400
    assert str(MAX_MISSION_CHARS) in r.text
    # ...and nothing was stored: a refused write leaves the seat as it was.
    rows = {a["agent_id"]: a["mission"]
            for a in client.get("/admin/missions", headers=ADMIN).json()}
    assert rows["lead"] == ""


def test_control_characters_still_go(client):
    register(client, "lead")
    got = set_mission(client, "lead", "Delegate.\x00\x07 Never\tdecides alone.").json()
    assert "\x00" not in got["mission"] and "\x07" not in got["mission"]
    assert "Never decides alone." in got["mission"]
