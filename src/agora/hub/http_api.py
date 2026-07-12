"""REST surface of the hub.

Everything an agent can do is available over plain HTTP so that the simplest
possible client (curl, an MCP tool, a cron job) can participate. The
WebSocket endpoint (ws.py) adds low-latency push on top of the same service.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel

from ..db import StoreConflict
from ..models import AgentInfo, PostMessage
from .service import HubError, HubService


def get_service(request: Request) -> HubService:
    return request.app.state.service


def get_admin_key(request: Request) -> str:
    return request.app.state.admin_key


def bearer_token(authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    return authorization.removeprefix("Bearer ")


def current_agent(
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
) -> AgentInfo:
    try:
        return service.authenticate(token)
    except HubError as e:
        raise HTTPException(e.status_code, e.detail) from e


router = APIRouter()


def _run(fn, *args, **kwargs):
    """Translate service errors into HTTP errors in one place."""
    try:
        return fn(*args, **kwargs)
    except StoreConflict as e:
        raise HTTPException(409, f"store version conflict: current version is {e.current_version}")
    except HubError as e:
        raise HTTPException(e.status_code, e.detail)


# -- admin ----------------------------------------------------------------------

class RegisterAgent(BaseModel):
    id: str
    name: str = ""
    about: str = ""         # self-description: scope, ownership, what to ask this agent
    operator: bool = False  # may post critical broadcasts; admin-granted only


@router.post("/agents")
def register_agent(
    payload: RegisterAgent,
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> dict[str, Any]:
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "agent registration requires the admin key")
    info, api_key = _run(service.register_agent, payload.id, payload.name,
                         payload.operator, payload.about)
    # The plaintext key is returned exactly once; only its hash is stored.
    return {"agent": info.model_dump(), "api_key": api_key}


# -- join tokens (scoped onboarding; the admin key never leaves the hub) --------

class CreateJoinToken(BaseModel):
    agent_id: str | None = None   # None = the redeemer chooses (--any-id mints)
    about: str = ""               # default self-description for the joiner
    channels: list[str] = []      # PUBLIC channels to auto-join on redemption
    ttl_seconds: float = 86400.0  # 24h default, 30d cap
    max_uses: int = 1             # single-use default; up to 100 for fleets


class JoinRequest(BaseModel):
    token: str
    agent_id: str | None = None   # required iff the token pins no id
    about: str = ""


@router.post("/join-tokens")
def create_join_token(
    payload: CreateJoinToken,
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> dict[str, Any]:
    """Mint a join token (operator surface — same gate as registration).
    The plaintext token appears exactly once, in this response."""
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "join-token minting requires the admin key")
    return _run(service.create_join_token, agent_id=payload.agent_id,
                about=payload.about, channels=payload.channels,
                ttl_seconds=payload.ttl_seconds, max_uses=payload.max_uses)


@router.get("/join-tokens")
def list_join_tokens(
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> list[dict[str, Any]]:
    """The mint/redeem audit trail (no secrets): who was invited, by whom,
    redeemed by whom, what remains live."""
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "listing join tokens requires the admin key")
    return service.list_join_tokens()


@router.delete("/join-tokens/{token_id}")
def revoke_join_token(
    token_id: str,
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> dict[str, Any]:
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "revoking a join token requires the admin key")
    _run(service.revoke_join_token, token_id)
    return {"token_id": token_id, "revoked": True}


@router.post("/join")
def join(
    payload: JoinRequest,
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Redeem a join token. Deliberately UNauthenticated: the token IS the
    credential (k8s bootstrap-token / tailscale authkey model). Registration
    is forced operator=False; distinct 403 details name what went wrong
    (expired / already used / revoked / locked to '<id>'); a 409 id collision
    does NOT consume the token, so the joiner can retry with a free id."""
    info, api_key, joined = _run(service.redeem_join_token, payload.token,
                                 payload.agent_id, payload.about)
    # Same one-time-plaintext contract as /agents.
    return {"agent": info.model_dump(), "api_key": api_key,
            "channels_joined": joined}


@router.get("/whoami")
def whoami(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Identity + the hub rules. Rules ride whoami because it is the one call
    every agent's session-start convention already makes — delivery lands
    exactly at the boundary the hub cannot otherwise see (new session,
    post-compaction), with zero extra round-trips."""
    return {**agent.model_dump(), "hub_rules": service.hub_rules()}


class SetHubRules(BaseModel):
    text: str


@router.get("/admin/rules")
def get_hub_rules(
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> dict[str, Any]:
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "reading hub rules via admin requires the admin key")
    return service.hub_rules()


@router.put("/admin/rules")
def set_hub_rules(
    payload: SetHubRules,
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> dict[str, Any]:
    """Replace the hub rules (operator surface). Every agent sees the new
    text + version at its next /whoami — no workspace re-setup anywhere."""
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "setting hub rules requires the admin key")
    result = _run(service.set_hub_rules, payload.text)
    return {"version": result["version"]}


@router.get("/admin/status")
def admin_status(
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> list[dict[str, Any]]:
    """One row per agent: presence, unread, oldest pending obligation. The
    'is anyone dark with work pending?' question as a single query — this IS
    the dead-agent alarm, surfaced in `agora status` (no extra subsystem)."""
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "status overview requires the admin key")
    return service.agent_status_overview()


class SetAbout(BaseModel):
    about: str


@router.put("/me/about")
def set_about(
    payload: SetAbout,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    return _run(service.set_about, agent, payload.about).model_dump()


# -- channels ----------------------------------------------------------------------

class CreateChannel(BaseModel):
    name: str
    private: bool = True


class CreateInvite(BaseModel):
    agent_id: str | None = None   # None = anyone with the token may join
    ttl_seconds: float = 86400.0


class JoinChannel(BaseModel):
    invite_token: str | None = None


@router.get("/channels")
def list_channels(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    return service.db.list_channels(agent.id)


@router.post("/channels")
def create_channel(
    payload: CreateChannel,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    return _run(service.create_channel, agent, payload.name, payload.private)


@router.post("/channels/{channel}/invites")
def create_invite(
    channel: str,
    payload: CreateInvite,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    token = _run(service.create_invite, agent, channel, payload.agent_id, payload.ttl_seconds)
    return {"invite_token": token, "channel": channel, "agent_id": payload.agent_id}


@router.post("/channels/{channel}/join")
def join_channel(
    channel: str,
    payload: JoinChannel,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    return _run(service.join_channel, agent, channel, payload.invite_token)


@router.post("/channels/{channel}/leave")
def leave_channel(
    channel: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    _run(service.leave_channel, agent, channel)
    return {"channel": channel, "left": True}


@router.get("/channels/{channel}/members")
def list_members(
    channel: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    _run(service.require_membership, channel, agent.id)
    return [m.model_dump() for m in service.db.list_members(channel)]


# -- messages ----------------------------------------------------------------------

@router.get("/channels/{channel}/messages")
def get_messages(
    channel: str,
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    messages = _run(service.get_messages, agent, channel, since, limit)
    return [m.model_dump() for m in messages]


@router.post("/channels/{channel}/messages")
def post_message(
    channel: str,
    payload: PostMessage,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    return _run(service.post_message, agent, channel, payload).model_dump()


@router.get("/channels/{channel}/messages/{message_id}")
def read_message(
    channel: str,
    message_id: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    """Deliberate body fetch: returns the message plus unread reply-chain
    ancestors (oldest first) and records read receipts (un-pins criticals)."""
    return [m.model_dump() for m in _run(service.read_message, agent, channel, message_id)]


@router.get("/channels/{channel}/info")
def channel_info(
    channel: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    return _run(service.channel_info, agent, channel)


@router.get("/channels/{channel}/digest")
def channel_digest(
    channel: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """The room's history folded into actionable knowledge: open questions
    (with pending ask texts), decided items, and the store's `decision:*`
    record — computed from message structure alone."""
    return _run(service.channel_digest, agent, channel)


# -- inbox (the trigger surface: long-poll for unread across all my channels) --------

@router.get("/inbox")
async def inbox(
    wait: float = Query(default=0.0, ge=0.0, le=55.0),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    if wait > 0:
        messages = await service.wait_inbox(agent, wait)
    else:
        messages = service.inbox(agent)
    return [m.model_dump() for m in messages]


class AckInbox(BaseModel):
    cursors: dict[str, int]  # channel -> highest seq read


@router.post("/inbox/ack")
def ack_inbox(
    payload: AckInbox,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    _run(service.ack_inbox, agent, payload.cursors)
    return {"acked": payload.cursors}


# -- per-channel store ------------------------------------------------------------

class StoreSet(BaseModel):
    value: Any
    expect_version: int | None = None  # CAS: 0 = "must not exist yet"


@router.get("/channels/{channel}/store")
def store_keys(
    channel: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    return _run(service.store_keys, agent, channel)


@router.get("/channels/{channel}/store/{key}")
def store_get(
    channel: str,
    key: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    return _run(service.store_get, agent, channel, key).model_dump()


@router.put("/channels/{channel}/store/{key}")
def store_set(
    channel: str,
    key: str,
    payload: StoreSet,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    entry = _run(service.store_set, agent, channel, key, payload.value, payload.expect_version)
    return entry.model_dump()


# -- per-channel virtual filesystem ----------------------------------------------

class FsWrite(BaseModel):
    content: str
    mime: str = "text/markdown"
    description: str = ""              # one line: what this file IS (shown in listings)
    expect_version: int | None = None  # CAS: 0 = "must not exist yet"


@router.get("/channels/{channel}/fs")
def fs_list(
    channel: str,
    prefix: str = Query(default=""),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    return _run(service.fs_list, agent, channel, prefix)


@router.get("/channels/{channel}/fs/{path:path}")
def fs_read(
    channel: str,
    path: str,
    version: int | None = None,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Head by default; `?version=N` returns that archived version verbatim
    (original author + date) — every write archives its content."""
    return _run(service.fs_read, agent, channel, path, version).model_dump()


@router.get("/channels/{channel}/ledger")
def channel_ledger(
    channel: str,
    verify: bool = Query(default=True),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """The channel's verbatim ledger (full ordered transcript + hash-chain head)."""
    return _run(service.channel_ledger, agent, channel, verify=verify)


@router.get("/channels/{channel}/fshist/{path:path}")
def fs_history(
    channel: str,
    path: str,
    since_seq: int = Query(default=0),
    limit: int = Query(default=200),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    return [m.model_dump() for m in _run(service.fs_history, agent, channel, path,
                                         since_seq, limit)]


@router.put("/channels/{channel}/fs/{path:path}")
def fs_write(
    channel: str,
    path: str,
    payload: FsWrite,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    return _run(service.fs_write, agent, channel, path, payload.content,
                payload.mime, payload.expect_version,
                payload.description).model_dump()


@router.delete("/channels/{channel}/fs/{path:path}")
def fs_delete(
    channel: str,
    path: str,
    expect_version: int | None = Query(default=None),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, bool]:
    return {"deleted": _run(service.fs_delete, agent, channel, path, expect_version)}


# -- direct (1:1) channels -------------------------------------------------------------

@router.post("/dms/{peer}")
def open_dm(
    peer: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Get-or-create the direct channel with `peer` (idempotent)."""
    return _run(service.open_dm, agent, peer)


@router.post("/dms/{peer}/messages")
def post_dm(
    peer: str,
    payload: PostMessage,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Send a direct message (opens the channel on first use; addressed to peer)."""
    return _run(service.post_dm, agent, peer, payload).model_dump()


# -- colleague notes (private, subjective, free-text) --------------------------------

class SetNote(BaseModel):
    note: str


@router.put("/colleagues/{subject}")
def set_note(
    subject: str,
    payload: SetNote,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    return _run(service.set_note, agent, subject, payload.note).model_dump()


@router.get("/colleagues")
def get_notes(
    subject: str | None = Query(default=None),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    return _run(service.get_notes, agent, subject)


# -- presence ----------------------------------------------------------------------

class SetPresence(BaseModel):
    state: str  # "idle" | "working"


@router.put("/presence")
def set_presence(
    payload: SetPresence,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    if payload.state not in ("idle", "working"):
        raise HTTPException(400, "state must be 'idle' or 'working'")
    return service.presence.update(agent.id, payload.state).model_dump()


@router.get("/presence")
def list_presence(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    """Who is reachable right now? One row per agent the caller shares a
    channel with (same visibility rule as the single-agent endpoint) — so
    'is anyone listening?' is a query, not an experiment (field-requested,
    observer retro)."""
    return [p.model_dump() for p in _run(service.list_presence, agent)]


@router.get("/presence/{agent_id}")
def get_presence(
    agent_id: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    return _run(service.get_presence, agent, agent_id).model_dump()
