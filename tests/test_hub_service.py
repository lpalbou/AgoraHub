"""Behavioral tests of HubService: membership, ordering, inbox, store, safety.

These illustrate the intended semantics; the service logic itself is
general-purpose and contains nothing specific to these scenarios.
"""

from __future__ import annotations

import asyncio

import pytest

from agora.db import Database, StoreConflict
from agora.hub.service import HubError, HubService
from agora.models import PostMessage, Status, Urgency


@pytest.fixture()
def service() -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=600.0)


@pytest.fixture()
def agents(service):
    """Two registered agents with a private channel owned by the first."""
    alice, _ = service.register_agent("alice", "Alice")
    bob, _ = service.register_agent("bob", "Bob")
    service.create_channel(alice, "design", private=True)
    return alice, bob


def test_register_and_authenticate(service):
    info, api_key = service.register_agent("alice", "Alice")
    assert info.id == "alice"
    assert service.authenticate(api_key).id == "alice"
    with pytest.raises(HubError) as e:
        service.authenticate("wrong-key")
    assert e.value.status_code == 401


def test_duplicate_agent_rejected(service):
    service.register_agent("alice", "")
    with pytest.raises(HubError) as e:
        service.register_agent("alice", "")
    assert e.value.status_code == 409


def test_private_channel_requires_invite(service, agents):
    alice, bob = agents
    with pytest.raises(HubError) as e:
        service.join_channel(bob, "design", invite_token=None)
    assert e.value.status_code == 403
    token = service.create_invite(alice, "design", invitee="bob")
    service.join_channel(bob, "design", invite_token=token)
    assert service.db.is_member("design", "bob")


def test_invite_is_single_use_and_addressable(service, agents):
    alice, bob = agents
    carol, _ = service.register_agent("carol", "")
    token = service.create_invite(alice, "design", invitee="bob")
    # Carol cannot redeem Bob's invite.
    with pytest.raises(HubError):
        service.join_channel(carol, "design", invite_token=token)
    service.join_channel(bob, "design", invite_token=token)
    # Token is spent now.
    with pytest.raises(HubError):
        service.join_channel(carol, "design", invite_token=token)


def test_only_owner_can_invite(service, agents):
    alice, bob = agents
    token = service.create_invite(alice, "design", invitee="bob")
    service.join_channel(bob, "design", invite_token=token)
    with pytest.raises(HubError) as e:
        service.create_invite(bob, "design", invitee=None)
    assert e.value.status_code == 403


def test_non_member_cannot_read_or_post(service, agents):
    alice, bob = agents
    with pytest.raises(HubError) as e:
        service.post_message(bob, "design", PostMessage(body="hi"))
    assert e.value.status_code == 403
    with pytest.raises(HubError):
        service.get_messages(bob, "design")
    with pytest.raises(HubError):
        service.store_set(bob, "design", "k", 1)


def test_seq_is_monotonic_per_channel(service, agents):
    alice, _ = agents
    system_offset = service.db.last_seq("design")  # channel-created system message
    for i in range(5):
        message = service.post_message(alice, "design", PostMessage(body=f"m{i}"))
        assert message.seq == system_offset + i + 1
    history = service.get_messages(alice, "design", since_seq=system_offset)
    assert [m.seq for m in history] == list(range(system_offset + 1, system_offset + 6))


def test_inbox_excludes_own_and_ack_advances(service, agents):
    alice, bob = agents
    token = service.create_invite(alice, "design", invitee="bob")
    service.join_channel(bob, "design", invite_token=token)
    # An fyi carries no obligation, so acking its envelope drains the inbox.
    service.post_message(alice, "design", PostMessage(body="hello bob", status=Status.fyi))
    unread = service.inbox(bob)
    bodies = [m.body for m in unread if m.kind == "message"]
    assert bodies == ["hello bob"]
    assert all(m.sender != "bob" for m in unread)
    service.ack_inbox(bob, {"design": max(m.seq for m in unread)})
    assert service.inbox(bob) == []


async def test_wait_inbox_wakes_on_post(service, agents):
    alice, bob = agents
    token = service.create_invite(alice, "design", invitee="bob")
    service.join_channel(bob, "design", invite_token=token)
    service.ack_inbox(bob, {"design": service.db.last_seq("design")})

    async def post_later():
        await asyncio.sleep(0.05)
        service.post_message(alice, "design", PostMessage(body="wake up", urgency=Urgency.next_turn))

    waiter = asyncio.create_task(service.wait_inbox(bob, timeout=5.0))
    await post_later()
    messages = await asyncio.wait_for(waiter, timeout=2.0)
    assert any(m.body == "wake up" for m in messages)


async def test_wait_inbox_times_out_empty(service, agents):
    alice, _ = agents
    service.ack_inbox(alice, {"design": service.db.last_seq("design")})
    messages = await service.wait_inbox(alice, timeout=0.1)
    assert messages == []


def test_store_cas(service, agents):
    alice, _ = agents
    entry = service.store_set(alice, "design", "contract", {"v": 1}, expect_version=0)
    assert entry.version == 1
    # Stale expectation fails and reports the current version.
    with pytest.raises(StoreConflict) as e:
        service.store_set(alice, "design", "contract", {"v": 2}, expect_version=0)
    assert e.value.current_version == 1
    entry = service.store_set(alice, "design", "contract", {"v": 2}, expect_version=1)
    assert entry.version == 2
    assert service.store_get(alice, "design", "contract").value == {"v": 2}


def test_rate_limit_arrests_reply_loops(agents):
    # Fresh service with a tight budget to exercise the safety valve.
    service = HubService(Database(":memory:"), rate_per_minute=1.0)
    alice, _ = service.register_agent("alice", "")
    service.create_channel(alice, "loop", private=True)
    burst_allowed = 0
    with pytest.raises(HubError) as e:
        for _ in range(100):
            service.post_message(alice, "loop", PostMessage(body="again"))
            burst_allowed += 1
    assert e.value.status_code == 429
    assert burst_allowed < 100


def test_message_size_cap(service, agents):
    alice, _ = agents
    with pytest.raises(HubError) as e:
        service.post_message(alice, "design", PostMessage(body="x" * 70_000))
    assert e.value.status_code == 413
