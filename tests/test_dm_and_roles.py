"""Tests for v0.3: direct 1:1 channels, self-descriptions (about),
join onboarding (no history flood), and channel language policy."""

from __future__ import annotations

import pytest

from agora.db import Database
from agora.hub.service import CHANNEL_META_KEY, HubError, HubService
from agora.models import PostMessage, Status


@pytest.fixture()
def service() -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=600.0)


@pytest.fixture()
def agents(service):
    alice, _ = service.register_agent("alice", "Alice", about="owns the runtime package")
    bob, _ = service.register_agent("bob", "Bob")
    eve, _ = service.register_agent("eve", "Eve")
    return alice, bob, eve


# -- direct (1:1) channels -----------------------------------------------------------

def test_dm_is_created_on_first_send_and_is_pairwise(service, agents):
    alice, bob, eve = agents
    message = service.post_dm(alice, "bob", PostMessage(body="private ping", status=Status.open))
    assert message.channel == "dm:alice--bob"
    assert message.to == ["bob"]                      # hub-addressed: inlined for bob
    [envelope] = [e for e in service.inbox(bob) if e.kind == "message"]
    assert envelope.to_me and envelope.body == "private ping"
    # Same channel regardless of who initiates (idempotent, order-independent).
    reply = service.post_dm(bob, "alice", PostMessage(body="pong"))
    assert reply.channel == "dm:alice--bob"
    # A third agent has no access of any kind.
    with pytest.raises(HubError) as e:
        service.get_messages(eve, "dm:alice--bob")
    assert e.value.status_code == 403


def test_dm_is_structurally_closed(service, agents):
    alice, bob, eve = agents
    service.post_dm(alice, "bob", PostMessage(body="hi"))
    # No owner exists, so invites cannot be minted by anyone...
    for member in (alice, bob):
        with pytest.raises(HubError) as e:
            service.create_invite(member, "dm:alice--bob", invitee="eve")
        assert e.value.status_code == 403
    # ...joins are rejected outright...
    with pytest.raises(HubError) as e:
        service.join_channel(eve, "dm:alice--bob", invite_token=None)
    assert e.value.status_code == 403
    # ...and channel-meta writes fail too (hub defaults apply in DMs).
    with pytest.raises(HubError):
        service.store_set(alice, "dm:alice--bob", CHANNEL_META_KEY, {"purpose": "x"})
    # The pairwise working store still works (ordinary keys are member-writable).
    entry = service.store_set(alice, "dm:alice--bob", "shared-scratch", {"x": 1})
    assert entry.version == 1


def test_dm_edge_cases(service, agents):
    alice, _, _ = agents
    with pytest.raises(HubError) as e:
        service.post_dm(alice, "alice", PostMessage(body="me myself"))
    assert e.value.status_code == 400
    with pytest.raises(HubError) as e:
        service.post_dm(alice, "ghost", PostMessage(body="anyone?"))
    assert e.value.status_code == 404
    with pytest.raises(HubError) as e:
        service.create_channel(alice, "dm:sneaky--pair")
    assert e.value.status_code == 400                 # prefix reserved


# -- self-descriptions (functional roles) ------------------------------------------------

def test_about_is_visible_to_members_and_editable(service, agents):
    alice, bob, _ = agents
    service.create_channel(alice, "design")
    token = service.create_invite(alice, "design", invitee="bob")
    service.join_channel(bob, "design", invite_token=token)
    members = {m.agent_id: m for m in service.db.list_members("design")}
    assert members["alice"].about == "owns the runtime package"
    updated = service.set_about(bob, "owns the memory package: graph store\nask before touching")
    assert "\n" not in updated.about                  # sanitized like titles
    members = {m.agent_id: m for m in service.db.list_members("design")}
    assert members["bob"].about.startswith("owns the memory package")


def test_join_announcement_carries_about(service, agents):
    alice, bob, _ = agents
    service.create_channel(alice, "design")
    service.set_about(bob, "owns the memory package")
    token = service.create_invite(alice, "design", invitee="bob")
    service.join_channel(bob, "design", invite_token=token)
    history = service.get_messages(alice, "design", since_seq=0)
    joins = [m for m in history if m.kind == "system" and "bob joined" in m.body]
    assert joins and "owns the memory package" in joins[0].body


# -- join onboarding -----------------------------------------------------------------------

def test_join_does_not_flood_inbox_but_history_stays_readable(service, agents):
    alice, bob, _ = agents
    service.create_channel(alice, "design")
    for i in range(10):
        service.post_message(alice, "design", PostMessage(body=f"old discussion {i}"))
    token = service.create_invite(alice, "design", invitee="bob")
    response = service.join_channel(bob, "design", invite_token=token)
    # One-call onboarding payload: meta, members (with abouts), language.
    assert response["joined"] is True
    assert {m["agent_id"] for m in response["members"]} == {"alice", "bob"}
    assert response["language"] == "plain"
    # Inbox: nothing from before the join (the flood bug), only what comes after.
    assert service.inbox(bob) == []
    service.post_message(alice, "design", PostMessage(body="fresh"))
    assert [e.body for e in service.inbox(bob)] == ["fresh"]
    # Full history remains a deliberate read.
    history = service.get_messages(bob, "design", since_seq=0)
    assert sum(1 for m in history if m.body.startswith("old discussion")) == 10


# -- channel language policy ------------------------------------------------------------------

def test_language_meta_is_validated_and_exposed(service, agents):
    alice, _, _ = agents
    service.create_channel(alice, "telemetry")
    with pytest.raises(HubError) as e:
        service.store_set(alice, "telemetry", CHANNEL_META_KEY, {"language": "klingon"})
    assert e.value.status_code == 400
    service.store_set(alice, "telemetry", CHANNEL_META_KEY,
                      {"purpose": "high-frequency status traffic", "language": "structured"})
    info = service.channel_info(alice, "telemetry")
    assert info["language"] == "structured"
    assert info["is_dm"] is False
