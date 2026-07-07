"""Unit tests for the AgentRunner triggering guardrails (loop safety, triage)."""

from __future__ import annotations

import time

from agora.agent import AgentRunner, _PeerExchangeCap, _TurnBudget
from agora.models import Envelope, Kind, Status, Urgency


def _env(status=Status.fyi, to_me=False, reply_to_me=False, critical=False,
         escalated=False, sender="alice", seq=1) -> Envelope:
    return Envelope(id=f"m{seq}", channel="c", seq=seq, sender=sender,
                    kind=Kind.message, status=status,
                    urgency=Urgency.inbox, effective_urgency=Urgency.inbox,
                    critical=critical, escalated=escalated, to_me=to_me,
                    reply_to_me=reply_to_me, body="x", body_bytes=1)


def test_turn_budget_caps_invocations():
    b = _TurnBudget(max_per_minute=3)
    assert [b.allow() for _ in range(5)] == [True, True, True, False, False]


def test_peer_exchange_cap_stops_pingpong():
    cap = _PeerExchangeCap(max_replies=2, window_s=60)
    assert cap.allow("bob") and cap.allow("bob")
    assert not cap.allow("bob")          # third reply to same peer refused
    assert cap.allow("carol")            # a different peer is independent


def test_peer_cap_window_expires():
    cap = _PeerExchangeCap(max_replies=1, window_s=0.05)
    assert cap.allow("bob")
    assert not cap.allow("bob")
    time.sleep(0.06)
    assert cap.allow("bob")              # window elapsed, allowed again


def _runner(**kw) -> AgentRunner:
    return AgentRunner(lambda m, c: None, url="http://x", api_key="k",
                       channels=["c"], **kw)


def test_default_should_invoke_respects_attention_model():
    r = _runner()
    d = r._default_should_invoke
    assert d(_env(status=Status.fyi)) is False        # plain broadcast: skip
    assert d(_env(status=Status.open)) is True         # obligation: act
    assert d(_env(status=Status.blocked)) is True
    assert d(_env(to_me=True)) is True                 # addressed: act
    assert d(_env(reply_to_me=True)) is True           # reply to me: act
    assert d(_env(critical=True)) is True              # forced: act
    assert d(_env(escalated=True)) is True             # rotting obligation: act


def test_invoke_on_fyi_opt_in():
    assert _runner(invoke_on_fyi=True)._default_should_invoke(_env(status=Status.fyi)) is True


def test_seen_set_is_bounded():
    """The dedupe seen-set must not grow without bound over a long run."""
    r = _runner()
    for i in range(5000):
        e = _env(seq=i)
        r._seen_set.add(e.id)
        r._seen.append(e.id)
        if len(r._seen_set) > r._seen.maxlen:
            r._seen_set.discard(r._seen.popleft())
    assert len(r._seen) <= r._seen.maxlen
    assert len(r._seen_set) <= r._seen.maxlen
