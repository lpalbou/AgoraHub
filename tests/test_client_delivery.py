"""Client-side delivery gate: the catch-up sweep must never lose messages.

Audit finding C1: the hub sorts /inbox by criticality (critical first), but
AgoraClient._accept dedups by a per-channel seq HIGH-WATER. Accepting seq 8
(critical) before seq 7 (plain) would set the high-water to 8 and silently
drop 7 forever — then ack past it. The fix sorts sweep rows into per-channel
seq order before accepting; these tests pin that behavior.
"""

from __future__ import annotations

import asyncio

from agora.client.client import AgoraClient


def _row(channel: str, seq: int, *, critical: bool = False) -> dict:
    return {
        "id": f"{channel}-{seq}", "channel": channel, "seq": seq,
        "sender": "alice", "kind": "message", "status": "fyi",
        "urgency": "inbox", "effective_urgency": "inbox",
        "critical": critical, "escalated": False, "to_me": False,
        "reply_to_me": False, "title": "", "body": "x", "body_bytes": 1,
    }


class _StubHTTP:
    """Minimal httpx.AsyncClient stand-in returning a canned /inbox payload."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def get(self, path: str, **kw):
        class _Resp:
            status_code = 200
            def __init__(self, rows): self._rows = rows
            def json(self): return self._rows
        return _Resp(self._rows)


def _client_with_inbox(rows: list[dict]) -> AgoraClient:
    client = AgoraClient("http://test", "key")
    client.agent_id = "bob"
    client._http = _StubHTTP(rows)  # type: ignore[assignment]
    return client


def test_catch_up_survives_criticality_ordering():
    """A critical seq 8 listed before a plain seq 7 (hub inbox order) must not
    suppress seq 7: both are delivered, in per-channel seq order."""
    client = _client_with_inbox([
        _row("design", 8, critical=True),   # hub sorts criticals first
        _row("design", 7),
    ])
    asyncio.run(client._catch_up())
    delivered = client.inbox.drain()
    assert [e.seq for e in delivered] == [7, 8]
    assert client.cursors == {"design": 8}


def test_catch_up_is_best_effort_on_malformed_rows():
    """Schema drift in one row must not kill the sweep task (audit H1): the
    reconnect loop that calls this would die and leave the client deaf."""
    client = _client_with_inbox([{"garbage": True}])
    asyncio.run(client._catch_up())  # must not raise
    assert client.inbox.drain() == []


def test_ack_requires_explicit_cursors():
    """0011: the zero-arg blanket ack acked everything DELIVERED, not
    everything HANDLED — a crash between delivery and handling silently
    buried messages. Bare ack() must now refuse loudly; ack(None) too."""
    import pytest

    client = AgoraClient("http://test", "key")
    with pytest.raises(TypeError):
        asyncio.run(client.ack())  # missing required argument
    with pytest.raises(TypeError, match="ack_all_delivered"):
        asyncio.run(client.ack(None))  # old misuse: teaching error


def test_ack_all_delivered_sends_pending_and_clears():
    """The blanket form survives under its honest name: it posts exactly the
    pending high-water cursors and clears them; empty pending = no call."""
    posted: list[dict] = []

    class _HTTP:
        async def post(self, path, json=None, **kw):
            posted.append({"path": path, "json": json})
            class _Resp:
                status_code = 200
                def json(self): return {}
            return _Resp()

    client = AgoraClient("http://test", "key")
    client._http = _HTTP()  # type: ignore[assignment]
    asyncio.run(client.ack_all_delivered())  # nothing pending: no HTTP call
    assert posted == []
    client._pending_acks = {"design": 7}
    asyncio.run(client.ack_all_delivered())
    assert posted == [{"path": "/inbox/ack", "json": {"cursors": {"design": 7}}}]
    assert client._pending_acks == {}


def test_connect_is_bounded_separately_from_the_long_poll_read():
    """CONNECT and READ are different failures and one number cannot serve
    both (2026-08-25, `claim:pristine-head-has-two-reds`).

    The read timeout is 70s on purpose — it must clear the /inbox long-poll
    cap of 55s. Applying that same 70s to CONNECT is what made a host that is
    up but not ACCEPTING (firewall drop, laptop asleep, wrong port) cost the
    caller a full OS TCP-retransmit give-up per attempt instead of failing
    fast. Measured on Darwin against a bound-but-unlistened port: **25.94s
    for one `list_channels()`**, which is why `agora listen --once --max-wait
    1.5` returned at 31.3s and `tests/test_listen.py::
    test_ws_hub_unreachable_once_max_wait_ends_hub_unreachable_exit_0` was red
    on a pristine HEAD checkout.

    Asserted on the CONFIGURATION rather than by timing a real black-holing
    socket, because that behaviour is not portable: a bound-but-unlistened
    port RSTs instantly on Linux and black-holes on Darwin, so a timing test
    would pass vacuously on the platform CI runs. Every seat's reception and
    every CLI call goes through this client.

    Mutant: collapse it back to `httpx.Timeout(70.0)` — `connect` becomes
    70.0 and both assertions fire.
    """
    client = AgoraClient("http://127.0.0.1:1", "k", agent_id="bob")
    try:
        timeout = client._http.timeout
        assert timeout.read == 70.0, "the long-poll read budget moved"
        assert timeout.connect is not None and timeout.connect <= 15.0, \
            f"connect is unbounded or too slow to honour a wake window: {timeout.connect}"
        assert timeout.connect < timeout.read, \
            "a connect that outlives the read budget cannot fail fast"
    finally:
        asyncio.run(client._http.aclose())
