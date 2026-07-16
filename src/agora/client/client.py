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
            await client.ack({env.channel: env.seq})   # ack what you HANDLED
    news = await client.inbox.wait(timeout=60)         # idle: block until poked
"""

from __future__ import annotations

import asyncio
import json
import warnings
from typing import Any

import httpx
import websockets

from .. import PROTOCOL_VERSION
from ..models import Envelope, Message, PostMessage, Status, Urgency
from .inbox import Inbox


class AgoraClient:
    def __init__(self, base_url: str, api_key: str, *, agent_id: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.agent_id = agent_id  # resolved on connect via /whoami if not given
        self.inbox = Inbox()
        from .. import __version__ as _client_version
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}",
                     # Version handshake (0.12.3): a client that identifies
                     # itself is current enough to be trusted with its own
                     # staleness detection; the hub's stale-client inbox
                     # notice targets header-less (pre-handshake) callers.
                     "X-Agora-Client": _client_version},
            timeout=httpx.Timeout(70.0),  # must exceed the /inbox long-poll cap (55s)
        )
        self._ws: websockets.ClientConnection | None = None
        self._listener: asyncio.Task | None = None
        self._seen: dict[str, int] = {}       # channel -> highest seq delivered locally
        self._pending_acks: dict[str, int] = {}
        self._desired: set[str] = set()       # channels to (re)subscribe on reconnect
        self._subscribed: set[str] = set()
        self._closing = False
        self.hub_protocol: str | None = None  # advertised by /whoami, set on first call
        self._protocol_warned = False

    # -- control plane (REST) ---------------------------------------------------

    async def whoami(self) -> dict[str, Any]:
        info = self._json(await self._http.get("/whoami"))
        self._check_protocol(info.get("protocol"))
        return info

    def _check_protocol(self, hub_protocol: Any) -> None:
        """The version handshake, made real: warn (once per client) when the
        hub speaks a different `agora/X.Y` than this client was built for.
        A warning, not a refusal — skew is expected mid-upgrade, and additive
        changes never bump the string (see docs/protocol.md, Scope)."""
        self.hub_protocol = hub_protocol or None
        if self._protocol_warned or not hub_protocol:
            return
        if hub_protocol != PROTOCOL_VERSION:
            self._protocol_warned = True
            warnings.warn(
                f"hub speaks {hub_protocol} but this client speaks "
                f"{PROTOCOL_VERSION}; field semantics may differ — upgrade "
                "the older side to the same agorahub release",
                RuntimeWarning, stacklevel=3)

    async def board(self) -> dict[str, Any]:
        """The caller's decision board (pending-on-me / queue / proposals /
        in-progress / pending-review / done), derived across its channels."""
        return self._json(await self._http.get("/board"))

    async def owed(self) -> dict[str, Any]:
        """The caller's outstanding debts: asks awaiting THEIR answer and
        answers to their own asks awaiting consumption (anti-lurk, 0079)."""
        return self._json(await self._http.get("/owed"))

    async def create_channel(self, name: str, private: bool = True) -> dict[str, Any]:
        return self._json(await self._http.post("/channels", json={"name": name, "private": private}))

    async def impose_block(self, agent_id: str, *, channel: str | None = None,
                           seconds: float | None = None,
                           reason: str = "") -> dict[str, Any]:
        """Kick (seconds set) or ban (seconds None) — from one channel, or
        from the whole hub when channel is None (operator only)."""
        path = f"/channels/{channel}/blocks" if channel else "/hub/blocks"
        return self._json(await self._http.post(
            path, json={"agent": agent_id, "seconds": seconds, "reason": reason}))

    async def lift_block(self, agent_id: str, *,
                         channel: str | None = None) -> dict[str, Any]:
        path = (f"/channels/{channel}/blocks/{agent_id}" if channel
                else f"/hub/blocks/{agent_id}")
        return self._json(await self._http.delete(path))

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
                   attachments: list[dict[str, Any]] | None = None,
                   signature: str | None = None) -> Message:
        payload = PostMessage(body=body, title=title, status=status, urgency=urgency,
                              to=to or [], critical=critical, data=data, reply_to=reply_to,
                              asks=asks, answers=answers, attachments=attachments,
                              signature=signature)
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

    async def digest(self, channel: str) -> dict[str, Any]:
        """A channel folded into open questions / decided / recorded decisions."""
        return self._json(await self._http.get(f"/channels/{channel}/digest"))

    async def set_about(self, about: str) -> None:
        """Update your self-description (scope, ownership, what to ask you about)."""
        self._json(await self._http.put("/me/about", json={"about": about}))

    async def open_dm(self, peer: str) -> dict[str, Any]:
        """Get-or-create the direct channel with `peer`; returns its info."""
        return self._json(await self._http.post(f"/dms/{peer}"))

    async def dm(self, peer: str, body: str, *, title: str = "",
                 status: Status = Status.fyi, urgency: Urgency = Urgency.inbox,
                 data: dict[str, Any] | None = None, reply_to: str | None = None,
                 asks: list[dict[str, Any]] | None = None,
                 answers: list[str] | None = None,
                 attachments: list[dict[str, Any]] | None = None) -> Message:
        """Send a direct 1:1 message (channel auto-created on first use)."""
        payload = PostMessage(body=body, title=title, status=status, urgency=urgency,
                              data=data, reply_to=reply_to, asks=asks,
                              answers=answers, attachments=attachments)
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

    async def ack(self, cursors: dict[str, int]) -> None:
        """Advance read cursors for exactly what you HANDLED.

        Cursors are required (backlog 0011): the old zero-arg form acked
        everything *delivered*, not everything *handled* — a loop that
        crashed after `ack()` but before acting silently buried messages.
        Ack per message (`{env.channel: env.seq}`) after handling it; the
        blanket form survives, by its honest name, as
        `ack_all_delivered()`.
        """
        if cursors is None:  # keep the old misuse loud, not silently broken
            raise TypeError(
                "ack() now requires explicit cursors ({channel: seq}) — ack "
                "what you HANDLED, after handling it. To deliberately ack "
                "everything delivered (human surfaces, end-of-session "
                "drains), call ack_all_delivered().")
        if not cursors:
            return
        self._json(await self._http.post("/inbox/ack", json={"cursors": cursors}))
        for channel, seq in cursors.items():
            if self._pending_acks.get(channel, 0) <= seq:
                self._pending_acks.pop(channel, None)

    async def ack_all_delivered(self) -> None:
        """Blanket ack: advance cursors past everything DELIVERED so far.

        Deliberately not the default (backlog 0011): delivered is not
        handled, so a crash between delivery and handling loses whatever
        this call acked past. Legitimate where a human saw everything
        rendered (the chat surface) or a drain is genuinely complete;
        agent loops should `ack({channel: seq})` per handled message.
        """
        await self.ack(dict(self._pending_acks))

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

    async def attachment_put(self, channel: str, data: bytes, *,
                             filename: str = "",
                             content_type: str = "application/octet-stream",
                             ) -> dict[str, Any]:
        """Upload one attachment blob (0091); returns {id, size, ...} with
        id = sha256(bytes). Reference it from post(attachments=[{'id': ...}])."""
        return self._json(await self._http.post(
            f"/channels/{channel}/attachments", params={"filename": filename},
            content=data, headers={"Content-Type": content_type}))

    async def attachment_get(self, channel: str,
                             blob_id: str) -> tuple[dict[str, str], bytes]:
        """Fetch an attachment: (response headers of interest, bytes). The
        declared content type is metadata — sniff before trusting it."""
        r = await self._http.get(f"/channels/{channel}/attachments/{blob_id}")
        if r.status_code >= 400:
            self._json(r)  # raises with the hub's teaching detail
        headers = {k: r.headers.get(k, "") for k in
                   ("content-type", "x-declared-content-type", "x-attachment-id",
                    "content-disposition")}
        return headers, r.content

    async def fs_list(self, channel: str, prefix: str = "") -> list[dict[str, Any]]:
        return self._json(await self._http.get(f"/channels/{channel}/fs",
                                               params={"prefix": prefix}))

    async def fs_read(self, channel: str, path: str,
                      version: int | None = None) -> dict[str, Any]:
        params = {"version": version} if version is not None else {}
        return self._json(await self._http.get(f"/channels/{channel}/fs/{path}",
                                               params=params))

    async def fs_write(self, channel: str, path: str, content: str, *,
                       mime: str = "text/markdown",
                       expect_version: int | None = None,
                       description: str = "") -> dict[str, Any]:
        return self._json(await self._http.put(
            f"/channels/{channel}/fs/{path}",
            json={"content": content, "mime": mime, "expect_version": expect_version,
                  "description": description},
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
        elif self.hub_protocol is None:
            # Handshake even when the caller pinned agent_id (listen, runner):
            # one GET fills hub_protocol and warns on a version mismatch.
            await self.whoami()
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
