"""What a seat actually receives — the contract `docs/architecture.md` states.

The hub's instruction model is layered, and the layers differ in the one
property that matters: whether the hub PUSHES the text or the seat must PULL
it. `whoami` pushes the hub rules in full and the seat's own mission; it
pushes the hub charter only as a POINTER, because re-sending an
authority-labelled document on every session-start call is the periodic
injection ADR-0002 forbids. Everything else is pulled.

These tests pin the parts a documentation page can otherwise assert forever
without anyone noticing they stopped being true. The posting gate in
particular had NO test before this file: the architecture diagram promised a
409, and the only occurrence of that refusal in the repo was the `raise`
itself.

Adjacent, deliberately not duplicated here: `tests/test_mission.py` owns who
may author a mission and the delegation refusal; `tests/test_charter_attention.py`
owns the /owed debt row and the inbox header.
"""

from __future__ import annotations

import pytest

from agora.db import Database
from agora.governance import ROLE_CHARTER, charter_view
from agora.hub.service import CHANNEL_META_KEY, HubError, HubService
from agora.models import PostMessage, Status


@pytest.fixture()
def hub():
    svc = HubService(Database(":memory:"), rate_per_minute=6000.0)
    owner, _ = svc.register_agent("alice", "Alice", about="owns billing",
                                  mission="Own billing end to end.")
    member, _ = svc.register_agent("bob", "Bob", about="owns search",
                                   mission="Own search relevance.")
    svc.create_channel(owner, "design", private=True)
    svc.join_channel(member, "design",
                     svc.create_invite(owner, "design", invitee="bob"))
    return svc, owner, member


# -- what whoami pushes -------------------------------------------------------


def test_whoami_pushes_the_hub_rules_as_TEXT(hub):
    """The rules are the one document with no pull step: a seat that never
    calls read_charter still has them."""
    svc, _, bob = hub
    rules = svc.hub_rules()
    assert rules["text"].strip(), "the packaged default must not be empty"
    assert "version" in rules


def test_whoami_pushes_the_seats_own_mission(hub):
    """The one operator-authored text that is PER SEAT. It rides whoami
    because that is the call a fresh session makes before it acts."""
    svc, _, bob = hub
    assert svc.db.get_mission(bob.id) == "Own search relevance."


def test_whoami_carries_the_hub_charter_as_a_POINTER_never_text(hub):
    """ADR-0002: a charter re-pushed every session-start call would be the
    periodic authority injection the tier model exists to forbid."""
    svc, _, bob = hub
    pointer = svc.hub_charter_pointer(bob.id, bob.operator)
    assert set(pointer) >= {"version", "your_receipt", "current", "read_with"}
    assert not any(isinstance(v, str) and len(v) > 200 for v in pointer.values()), (
        "the pointer must not smuggle the charter text")


# -- what a seat learns about its peers ---------------------------------------


def test_describe_channel_carries_every_members_about_AND_mission(hub):
    """A seat routes work by what its peers own. `about` alone could not
    serve — the seat writes its own, and one replaced its adversarial charge
    with a tidy summary of itself — so the member row carries the
    operator-authored mission beside it."""
    svc, _, bob = hub
    members = {m["agent_id"]: m for m in svc.channel_info(bob, "design")["members"]}
    assert members["alice"]["about"] == "owns billing"
    assert members["alice"]["mission"] == "Own billing end to end.", (
        "a seat must be able to see what the operator charged its peers with")


# -- the charter posting gate: the one mechanical consequence of a charter ----


def test_an_ungated_room_never_asks_anyone_to_read_anything(hub):
    svc, _, bob = hub
    assert svc.post_message(bob, "design", PostMessage(
        status=Status.fyi, body="hello")).seq > 0


def test_norms_required_refuses_posting_until_the_charter_is_READ(hub):
    """The read IS the receipt, so the refusal is self-healing in one call.
    The hub forces attention to the rules, never agreement with them."""
    svc, alice, bob = hub
    svc.store_set(alice, "design", CHANNEL_META_KEY, {"norms_required": True})

    with pytest.raises(HubError) as e:
        svc.post_message(bob, "design", PostMessage(status=Status.fyi, body="hi"))
    assert e.value.status_code == 409
    assert "read_charter" in str(e.value.detail), "the refusal must name its own fix"

    svc.fs_read(bob, "design", "channel/charter.md")
    assert svc.post_message(bob, "design", PostMessage(
        status=Status.fyi, body="hi")).seq > 0


def test_editing_the_charter_re_arms_the_gate_for_everyone_behind(hub):
    """A receipt is for a VERSION. Rules that changed under a seat are rules
    it has not read."""
    svc, alice, bob = hub
    svc.store_set(alice, "design", CHANNEL_META_KEY, {"norms_required": True})
    svc.fs_read(bob, "design", "channel/charter.md")
    svc.post_message(bob, "design", PostMessage(status=Status.fyi, body="one"))

    svc.fs_write(alice, "design", "channel/charter.md",
                 "# charter\nv2: answer within one turn.", expect_version=1)
    with pytest.raises(HubError) as e:
        svc.post_message(bob, "design", PostMessage(status=Status.fyi, body="two"))
    assert e.value.status_code == 409

    svc.fs_read(bob, "design", "channel/charter.md")
    assert svc.post_message(bob, "design", PostMessage(
        status=Status.fyi, body="two")).seq > 0


def test_the_gate_stops_POSTING_and_nothing_else(hub):
    """Scope stated honestly: `_require_charter_read` has one call site. A
    seat behind on the charter can still read, write files, and record
    decisions — only its voice in the room is held."""
    svc, alice, bob = hub
    svc.store_set(alice, "design", CHANNEL_META_KEY, {"norms_required": True})
    svc.fs_write(alice, "design", "channel/charter.md", "# charter\nv2",
                 expect_version=1)

    svc.store_set(bob, "design", "decision:x", {"summary": "still allowed"})
    svc.fs_write(bob, "design", "notes.md", "still allowed")
    with pytest.raises(HubError):
        svc.post_message(bob, "design", PostMessage(status=Status.fyi, body="held"))


# -- slicing: the hub charter is scoped, a room's charter never is ------------


def test_the_hub_charter_is_sliced_to_the_seats_own_kind(hub):
    """A member is served the member section, not the operator's."""
    full = charter_view(ROLE_CHARTER, roles=("member",), full=True).text
    member = charter_view(ROLE_CHARTER, roles=("member",)).text
    assert "## Member" in member
    assert "## Operator" not in member, "a member is not served the operator section"
    assert "## Operator" in full, "full=true still serves everything"


def test_a_rooms_own_charter_is_served_whole_and_verbatim(hub):
    """Never sliced: slicing a room's rules per seat would let an owner hide
    a rule from the member it binds."""
    svc, alice, bob = hub
    svc.fs_write(alice, "design", "channel/charter.md",
                 "# charter\n## Member\nm\n## Owner\no\n## Operator\np",
                 expect_version=1)
    served = svc.read_channel_charter(bob, "design")
    text = served["content"]
    assert "## Owner" in text and "## Operator" in text, (
        "the room's own text must reach every member unsliced")
