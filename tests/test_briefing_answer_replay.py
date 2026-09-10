"""A resumed seat can act on an answer older than its latest channel cursor."""
import json

import pytest

from agora.db import Database
from agora.hub.service import HubService
from agora.models import PostMessage, Status


@pytest.fixture
def room(tmp_path):
    db = Database(str(tmp_path / "hub.db"))
    hub = HubService(db, rate_per_minute=100000)
    agents = {}
    for name in ("operator", "delegate", "reviewer"):
        agents[name], _ = hub.register_agent(name, name, operator=name == "operator", mission=name)
    hub.create_channel(agents["operator"], "audit", private=False)
    for name in ("delegate", "reviewer"):
        hub.join_channel(agents[name], "audit", None)
    yield hub, agents
    db.close()


def reviewed_request(hub, agents):
    request = hub.post_message(agents["delegate"], "audit", PostMessage(
        title="Independent whole-artifact review", body="Review the consolidated roadmap.",
        status=Status.open, to=["reviewer"],
        asks=[{"id": "1", "text": "Return the review verdict", "to": ["reviewer"]}]))
    answer = hub.post_message(agents["reviewer"], "audit", PostMessage(
        title="Correct the stale coverage gate", body="Conditional approval: the coverage gate is stale.",
        status=Status.reply, reply_to=request.id, answers=["1"]))
    last = None
    for i in range(4):
        last = hub.post_message(agents["operator"], "audit", PostMessage(
            title=f"Independent update {i}", body=f"Unrelated recorded evidence {i}", status=Status.fyi))
    hub.ack_inbox(agents["delegate"], {"audit": last.seq})
    return request, answer


def test_answer_behind_cursor_is_directly_readable_after_seat_context_loss(room):
    hub, agents = room
    request, answer = reviewed_request(hub, agents)
    # Replay the failed action: fetching the request is not opening its answer.
    assert answer.id not in {m.id for m in hub.read_message(agents["delegate"], "audit", request.id)}
    # Reconstruct the service using only durable hub state, no conversation.
    resumed = HubService(hub.db, rate_per_minute=100000)
    brief = resumed.briefing(agents["delegate"])
    debt = next(r for r in brief["sections"]["to_consume"] if r["id"] == request.id)
    assert (debt["answer_id"], debt["answer_seq"], debt["answered_by"]) == (answer.id, answer.seq, "reviewer")
    assert debt["read"]["tool"] == "read_message"
    result = resumed.read_message(agents["delegate"], **debt["read"]["arguments"])
    assert answer.body in [m.body for m in result]


def test_trimming_large_debt_preserves_answer_read_target(room, monkeypatch):
    hub, agents = room
    request, answer = reviewed_request(hub, agents)
    real_owed = hub.owed
    def oversized(agent):
        owed = real_owed(agent)
        for debt in owed.to_consume:
            debt.title = "Review context " * 500
        return owed
    monkeypatch.setattr(hub, "owed", oversized)
    brief = hub.briefing(agents["delegate"])
    debt = next(r for r in brief["sections"]["to_consume"] if r["id"] == request.id)
    assert debt["answer_id"] == answer.id
    result = hub.read_message(agents["delegate"], **debt["read"]["arguments"])
    assert answer.id in {m.id for m in result}
    assert len(json.dumps(brief, ensure_ascii=False).encode()) <= 12000
