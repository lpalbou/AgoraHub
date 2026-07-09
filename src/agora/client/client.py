"""AgoraClient: async client combining REST (control plane) and WebSocket (push).

Typical interleaving loop for a Python agent (v0.2 envelope model):

    client = AgoraClient("http://127.0.0.1:8765", api_key)
    await client.connect(channels=["design"])          # push -> client.inbox
    while working:
        ... do one unit of work ...
        for env in client.inbox.drain():               # triage headlines
            if env.body is not None or worth_reading(env):
                msgs = [env] if env.body else await client.read(env.channel, env.id)
                consider(msgs)
        await client.ack()                             # advance triage cursors
    news = await client.inbox.wait(timeout=60)         # idle: block until poked
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import websockets

from ..models import Envelope, Message, PostMessage, Status, Urgency
from .inbox import Inbox


class AgoraClient:
    def __init__(self, base_url: str, api_key: str, *, agent_id: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.agent_id = agent_id  # resolved on connect via /whoami if not given
        self.inbox = Inbox()
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(70.0),  # must exceed the /inbox long-poll cap (55s)
        )
        self._ws: websockets.ClientConnection | None = None
        self._listener: asyncio.Task | None = None
        self._seen: dict[str, int] = {}       # channel -> highest seq delivered locally
        self._pending_acks: dict[str, int] = {}
        self._desired: set[str] = set()       # channels to (re)subscribe on reconnect
        self._subscribed: set[str] = set()
        self._closing = False

    # -- control plane (REST) ---------------------------------------------------

    async def whoami(self) -> dict[str, Any]:
        return self._json(await self._http.get("/whoami"))

    async def create_channel(self, name: str, private: bool = True) -> dict[str, Any]:
        return self._json(await self._http.post("/channels", json={"name": name, "private": private}))

    async def create_invite(self, channel: str, agent_id: str | None = None,
                            ttl_seconds: float = 86400.0) -> str:
        response = self._json(await self._http.post(
            f"/channels/{channel}/invites",
            json={"agent_id": agent_id, "ttl_seconds": ttl_seconds},
        ))
        return response["invite_token"]

    async def join_channel(self, channel: str, invite_token: str | None = None) -> dict[str, Any]:
        return self._json(await self._http.post(
            f"/channels/{channel}/join", json={"invite_token": invite_token},
        ))

    async def list_channels(self) -> list[dict[str, Any]]:
        return self._json(await self._http.get("/channels"))

    async def history(self, channel: str, since: int = 0, limit: int = 200) -> list[Message]:
        rows = self._json(await self._http.get(
            f"/channels/{channel}/messages", params={"since": since, "limit": limit},
        ))
        return [Message(**row) for row in rows]

    async def post(self, channel: str, body: str, *, title: str = "",
                   status: Status = Status.fyi, urgency: Urgency = Urgency.inbox,
                   to: list[str] | None = None, critical: bool = False,
                   data: dict[str, Any] | None = None, reply_to: str | None = None,
                   asks: list[dict[str, Any]] | None = None,
                   answers: list[str] | None = None,
                   signature: str | None = None) -> Message:
        payload = PostMessage(body=body, title=title, status=status, urgency=urgency,
                              to=to or [], critical=critical, data=data, reply_to=reply_to,
                              asks=asks, answers=answers, signature=signature)
        row = self._json(await self._http.post(
            f"/channels/{channel}/messages", json=payload.model_dump(mode="json"),
        ))
        return Message(**row)

    async def read(self, channel: str, message_id: str) -> list[Message]:
        """Deliberate body fetch: the message plus unread reply-chain ancestors
        (oldest first). Records read receipts (un-pins criticals)."""
        rows = self._json(await self._http.get(f"/channels/{channel}/messages/{message_id}"))
        return [Message(**row) for row in rows]

    async def check_inbox(self, wait: float = 0.0) -> list[Envelope]:
        """REST inbox (works without a WebSocket): unread envelopes across all
        my channels — criticals pinned first, then escalated obligations."""
        rows = self._json(await self._http.get("/inbox", params={"wait": wait}))
        envelopes = [Envelope(**row) for row in rows]
        for envelope in envelopes:
            self._note_seen(envelope)
        return envelopes

    async def channel_info(self, channel: str) -> dict[str, Any]:
        """Channel metadata + members (with abouts): read before your first post."""
        return self._json(await self._http.get(f"/channels/{channel}/info"))

    async def set_about(self, about: str) -> None:
        """Update your self-description (scope, ownership, what to ask you about)."""
        self._json(await self._http.put("/me/about", json={"about": about}))

    async def open_dm(self, peer: str) -> dict[str, Any]:
        """Get-or-create the direct channel with `peer`; returns its info."""
        return self._json(await self._http.post(f"/dms/{peer}"))

    async def dm(self, peer: str, body: str, *, title: str = "",
                 status: Status = Status.fyi, urgency: Urgency = Urgency.inbox,
                 data: dict[str, Any] | None = None, reply_to: str | None = None) -> Message:
        """Send a direct 1:1 message (channel auto-created on first use)."""
        payload = PostMessage(body=body, title=title, status=status, urgency=urgency,
                              data=data, reply_to=reply_to)
        row = self._json(await self._http.post(
            f"/dms/{peer}/messages", json=payload.model_dump(mode="json"),
        ))
        return Message(**row)

    async def set_note(self, subject: str, note: str) -> None:
        """Private, subjective colleague note (advisory triage input)."""
        self._json(await self._http.put(f"/colleagues/{subject}", json={"note": note}))

    async def get_notes(self, subject: str | None = None) -> list[dict[str, Any]]:
        params = {"subject": subject} if subject else {}
        return self._json(await self._http.get("/colleagues", params=params))

    async def ack(self, cursors: dict[str, int] | None = None) -> None:
        """Advance read cursors (default: everything delivered so far)."""
        cursors = cursors or dict(self._pending_acks)
        if not cursors:
            return
        self._json(await self._http.post("/inbox/ack", json={"cursors": cursors}))
        for channel, seq in cursors.items():
            if self._pending_acks.get(channel, 0) <= seq:
                self._pending_acks.pop(channel, None)

    async def store_get(self, channel: str, key: str) -> dict[str, Any]:
        return self._json(await self._http.get(f"/channels/{channel}/store/{key}"))

    async def store_set(self, channel: str, key: str, value: Any,
                        expect_version: int | None = None) -> dict[str, Any]:
        return self._json(await self._http.put(
            f"/channels/{channel}/store/{key}",
            json={"value": value, "expect_version": expect_version},
        ))

    async def store_keys(self, channel: str) -> list[dict[str, Any]]:
        return self._json(await self._http.get(f"/channels/{channel}/store"))

    # -- per-channel virtual filesystem (shared editable "book", any machine) ------

    async def fs_list(self, channel: str, prefix: str = "") -> list[dict[str, Any]]:
        return self._json(await self._http.get(f"/channels/{channel}/fs",
                                               params={"prefix": prefix}))

    async def fs_read(self, channel: str, path: str) -> dict[str, Any]:
        return self._json(await self._http.get(f"/channels/{channel}/fs/{path}"))

    async def fs_write(self, channel: str, path: str, content: str, *,
                       mime: str = "text/markdown",
                       expect_version: int | None = None) -> dict[str, Any]:
        return self._json(await self._http.put(
            f"/channels/{channel}/fs/{path}",
            json={"content": content, "mime": mime, "expect_version": expect_version},
        ))

    async def fs_delete(self, channel: str, path: str, *,
                        expect_version: int | None = None) -> dict[str, Any]:
        params = {} if expect_version is None else {"expect_version": expect_version}
        return self._json(await self._http.request(
            "DELETE", f"/channels/{channel}/fs/{path}", params=params))

    async def fs_history(self, channel: str, path: str, *,
                        since_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return self._json(await self._http.get(
            f"/channels/{channel}/fshist/{path}",
            params={"since_seq": since_seq, "limit": limit}))

    async def ledger(self, channel: str, *, verify: bool = True) -> dict[str, Any]:
        """The channel's verbatim ledger: full ordered transcript + hash-chain
        head (the durable common record of a room/session)."""
        return self._json(await self._http.get(
            f"/channels/{channel}/ledger", params={"verify": verify}))

    async def set_presence(self, state: str) -> None:
        self._json(await self._http.put("/presence", json={"state": state}))

    # -- push plane (WebSocket) ----------------------------------------------------

    async def connect(self, channels: list[str], since: dict[str, int] | None = None) -> None:
        """Open the push connection; new messages land in `self.inbox`.

        Survives drops: the listener reconnects with exponential backoff and
        re-subscribes to all desired channels from the client's own `_seen`
        cursors, so a hub restart or network blip resumes push with at-least-
        once catch-up rather than silently going deaf (v0.3 H2)."""
        if self.agent_id is None:
            self.agent_id = (await self.whoami())["id"]
        self._desired: set[str] = set(channels)
        self._subscribed: set[str] = set()
        self._closing = False
        for chan, seq in (since or {}).items():  # seed cursors so catch-up is bounded
            self._seen.setdefault(chan, seq)
        await self._open_ws()
        self._listener = asyncio.create_task(self._run())
        # Cold-start catch-up: on a fresh process the local high-water is empty,
        # so the WS subscribe requests no backlog and anything posted while this
        # client was down would never be pushed. A one-shot REST inbox sweep
        # recovers that gap window; the accept-gate dedups against live frames.
        # This is what makes AgentRunner and any long-lived client gap-free
        # across restarts and network flaps (previously only `agora watch` had it).
        await self._catch_up()

    async def _catch_up(self) -> None:
        try:
            rows = self._json(await self._http.get("/inbox"))
            # The hub sorts /inbox by criticality, but _accept dedups by a
            # per-channel seq HIGH-WATER: accepting seq 8 before seq 7 would
            # silently drop 7 forever (and then ack past it). Re-sort into
            # per-channel seq order before accepting. (Audit finding C1.)
            for row in sorted(rows, key=lambda r: (r["channel"], r["seq"])):
                self._accept(Envelope(**row))
        except Exception:
            # Best-effort, INCLUDING schema drift in Envelope parsing: an
            # exception escaping here would kill the reconnect loop and leave
            # the client silently deaf (audit H1). Next sweep covers the gap.
            return

    def _ws_url(self) -> str:
        # Map scheme explicitly: https->wss, http->ws. The old blanket
        # replace("http","ws") turned "https://" into "wsss://" (invalid),
        # silently breaking push for any TLS-terminated remote hub.
        base = self.base_url
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):]
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):]
        return base

    async def _open_ws(self) -> None:
        # The bearer key travels in the Authorization header (not the query
        # string) so it does not leak into proxy/access logs on remote links.
        ws_url = self._ws_url() + "/ws"
        self._ws = await websockets.connect(
            ws_url, additional_headers={"Authorization": f"Bearer {self.api_key}"},
        )
        self._subscribed = set()
        await self.subscribe(list(self._desired), since=dict(self._seen))

    async def subscribe(self, channels: list[str], since: dict[str, int] | None = None) -> None:
        """Subscribe additional channels on the live connection (e.g. a DM
        channel that appeared after connect). Idempotent; safe to call anytime."""
        self._desired.update(channels)
        if self._ws is None:
            return  # will be subscribed on next (re)connect
        new = [c for c in channels if c not in self._subscribed]
        if not new:
            return
        try:
            await self._ws.send(json.dumps(
                {"type": "subscribe", "channels": new, "since": since or dict(self._seen)}
            ))
            self._subscribed.update(new)
        except websockets.ConnectionClosed:
            pass  # the reconnect loop will resubscribe from _desired

    async def _run(self) -> None:
        backoff = 0.5
        while not self._closing:
            try:
                await self._listen_once()
                backoff = 0.5  # clean EOF: reset before reconnecting
            except (websockets.ConnectionClosed, OSError):
                pass
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            try:
                await self._open_ws()
                # Reconnect catch-up: WS re-subscribe only replays channels in
                # `_desired`, but the outage may have delivered into channels
                # this client never subscribed to (e.g. a DM opened while we
                # were down). The REST inbox sweep covers ALL memberships and
                # the accept-gate dedups overlap with the WS backlog.
                await self._catch_up()
            except (OSError, websockets.WebSocketException):
                pass  # keep retrying with growing backoff

    async def _listen_once(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            try:
                frame = json.loads(raw)
                if frame.get("type") == "envelope":
                    self._accept(Envelope(**frame["envelope"]))
            except (ValueError, TypeError):
                # A malformed frame (schema drift, hub/client version skew)
                # must not kill the listener task — that would leave the
                # client looking connected but permanently deaf (audit H1).
                continue

    def _accept(self, envelope: Envelope) -> None:
        """Single delivery gate for both the live listener and the connect-time
        catch-up sweep: dedup by per-channel seq (at-least-once), advance the
        local high-water, and deliver anything not sent by us. Synchronous, so
        concurrent live + sweep delivery cannot race on `_seen`."""
        if envelope.seq <= self._seen.get(envelope.channel, 0):
            return
        self._note_seen(envelope)
        if envelope.sender != self.agent_id:
            self.inbox.deliver(envelope)

    def _note_seen(self, item: Message | Envelope) -> None:
        if item.seq > self._seen.get(item.channel, 0):
            self._seen[item.channel] = item.seq
            self._pending_acks[item.channel] = item.seq

    @property
    def cursors(self) -> dict[str, int]:
        return dict(self._seen)

    async def close(self) -> None:
        self._closing = True
        if self._listener:
            # Await the cancellation: the reconnect loop may be inside
            # _open_ws() right now — cancelling without awaiting could let it
            # finish creating a socket nobody ever closes (a zombie connection
            # the hub counts as presence until process death; audit bug).
            self._listener.cancel()
            try:
                await self._listener
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            await self._ws.close()
        await self._http.aclose()

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise AgoraError(response.status_code, detail)
        return response.json()


class AgoraError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"[{status_code}] {detail}")
        self.status_code = status_code
        self.detail = detail
