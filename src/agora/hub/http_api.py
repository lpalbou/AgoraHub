"""REST surface of the hub.

Everything an agent can do is available over plain HTTP so that the simplest
possible client (curl, an MCP tool, a cron job) can participate. The
WebSocket endpoint (ws.py) adds low-latency push on top of the same service.
"""

from __future__ import annotations

import hmac
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ..db import StoreConflict
from ..models import AgentInfo, PostMessage
from .service import HubError, HubService, safe_serve_content_type


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


@router.get("/agents/retired")
def list_retired_agents(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    """Operator-only: enumerate retired identities so an un-retire UI can
    offer candidates (they are off every other roster by design, 0089)."""
    if not agent.operator:
        raise HTTPException(403, "listing retired agents is an operator view")
    return service.db.list_retired_agents()


class RetireAgent(BaseModel):
    reason: str = ""   # neutral, optional; stored and echoed (never "banned")


@router.post("/agents/{agent_id}/retire")
def retire_agent(
    agent_id: str,
    payload: RetireAgent | None = None,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Retire an identity (0089): neutral decommission — auth refused
    neutrally, evicted from rosters, id reserved forever. Operator only;
    NOT a block (never appears in /blocks). Reversible via DELETE."""
    return _run(service.retire_agent, agent, agent_id,
                payload.reason if payload else "")


@router.delete("/agents/{agent_id}/retire")
def unretire_agent(
    agent_id: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Restore a retired identity (operator only); it rejoins rooms explicitly."""
    return _run(service.unretire_agent, agent, agent_id)


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
    """Identity + the hub rules + the hub state. Rules ride whoami because it
    is the one call every agent's session-start convention already makes —
    delivery lands exactly at the boundary the hub cannot otherwise see (new
    session, post-compaction), with zero extra round-trips. hub_state is how
    a standing-down agent checks for the resume without posting."""
    from .. import PROTOCOL_VERSION, __version__
    pause = service.hub_paused()
    hub_state = ({"state": "paused", **pause} if pause is not None
                 else {"state": "open"})
    return {**agent.model_dump(),
            # The running hub's version + wire protocol, so every agent (and
            # the chat login) sees exactly what it is talking to — the single
            # source is agora.__version__ (pyproject reads it dynamically).
            "version": __version__, "protocol": PROTOCOL_VERSION,
            "hub_rules": service.hub_rules(),
            "hub_state": hub_state,
            # Delegation is verifiable state (ADR-0004): every agent sees who
            # holds which delegated powers — prose claims count for nothing.
            "delegations": service.active_delegations()}


class SetHubRules(BaseModel):
    text: str


class SetPause(BaseModel):
    reason: str = ""


@router.put("/admin/pause")
def pause_hub(
    payload: SetPause,
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> dict[str, Any]:
    """Pause the hub (operator stand-down; idempotent). Admin key ONLY —
    pause power on an LLM seat would be a denial-of-service primitive
    reachable from message content."""
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "pausing the hub requires the admin key")
    return _run(service.set_pause, payload.reason)


@router.delete("/admin/pause")
def resume_hub(
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> dict[str, Any]:
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "resuming the hub requires the admin key")
    return _run(service.clear_pause)


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


class SetDelegation(BaseModel):
    agent_id: str
    powers: list[str]
    ttl_seconds: float | None = None
    note: str = ""


@router.get("/delegations")
def list_delegations(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    """Active delegation grants — readable by every agent (verifiability is
    the point of the record)."""
    return service.active_delegations()


@router.get("/admin/delegations")
def admin_list_delegations(
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> list[dict[str, Any]]:
    """Same list as GET /delegations, admin-key-authenticated — the operator's
    CLI holds the admin key, not an agent key."""
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "listing delegations via admin requires the admin key")
    return service.active_delegations()


@router.put("/admin/delegation")
def set_delegation(
    payload: SetDelegation,
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> dict[str, Any]:
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "granting delegation requires the admin key")
    return _run(service.set_delegation, payload.agent_id, payload.powers,
                payload.ttl_seconds, payload.note)


@router.delete("/admin/delegation/{agent_id}")
def revoke_delegation(
    agent_id: str,
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> dict[str, Any]:
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "revoking delegation requires the admin key")
    return {"agent_id": agent_id, "revoked": _run(service.revoke_delegation, agent_id)}


class ImposeBlock(BaseModel):
    agent: str
    seconds: float | None = None   # None = ban (forever); set = kick (timed)
    reason: str = ""


@router.post("/channels/{channel}/blocks")
def channel_block(
    channel: str,
    payload: ImposeBlock,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Kick/ban from ONE channel — channel owner or operator (agent bearer)."""
    return _run(service.impose_block, agent, payload.agent, scope=channel,
                seconds=payload.seconds, reason=payload.reason)


@router.delete("/channels/{channel}/blocks/{agent_id}")
def channel_unblock(
    channel: str,
    agent_id: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    return {"agent_id": agent_id, "scope": channel,
            "lifted": _run(service.lift_block, agent, agent_id, scope=channel)}


@router.post("/hub/blocks")
def hub_block(
    payload: ImposeBlock,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Hub-wide lockout — operator agents only (enforced in the service)."""
    return _run(service.impose_block, agent, payload.agent,
                scope=service.HUB_SCOPE, seconds=payload.seconds,
                reason=payload.reason)


@router.delete("/hub/blocks/{agent_id}")
def hub_unblock(
    agent_id: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    return {"agent_id": agent_id, "scope": service.HUB_SCOPE,
            "lifted": _run(service.lift_block, agent, agent_id,
                           scope=service.HUB_SCOPE)}


@router.get("/blocks")
def list_blocks(
    scope: str | None = Query(default=None),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    """Active kicks/bans — visible to any agent (verifiable moderation state,
    same transparency posture as GET /delegations)."""
    return service.list_blocks(scope)


@router.get("/board")
def board(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """The viewer's decision board: pending-on-me / queue / proposals /
    in-progress / pending-review / done, derived across the viewer's
    channels. One derivation for every UI (CLI, Mission-Control-style
    boards); see docs/protocol.md."""
    return _run(service.board, agent)


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
    include_archived: bool = Query(default=False),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    # Only an operator may see archived rooms in the listing (their inspect
    # view); a non-operator's flag is ignored so archived stays delisted.
    include = include_archived and agent.operator
    return service.db.list_channels(agent.id, include_archived=include)


@router.post("/channels")
def create_channel(
    payload: CreateChannel,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    return _run(service.create_channel, agent, payload.name, payload.private)


@router.post("/channels/{channel}/archive")
def archive_channel(
    channel: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """End a channel (0090): evict all members, delist it, refuse further
    posts/joins — history preserved. Owner or operator."""
    return _run(service.archive_channel, agent, channel)


@router.delete("/channels/{channel}/archive")
def unarchive_channel(
    channel: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Reopen an archived channel (operator only); members rejoin explicitly."""
    return _run(service.unarchive_channel, agent, channel)


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

def _stale_client_notice(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A synthetic system envelope the HUB appends for clients that predate
    the version handshake (no X-Agora-Client header). Field incident c2578:
    a client-side staleness banner can never reach a stale server — it does
    not have the banner code. The warning must ride the RESPONSE DATA
    through render paths old clients already have.

    Safety by construction (all load-bearing):
    - channel/seq MIRROR the first real row, so acking it never moves any
      cursor beyond real traffic (set_cursor SETS; a novel low seq acked by
      an LLM would rewind and re-flood — duplicate seqs cannot).
    - AgoraClient/AgentRunner dedup by per-channel seq high-water, so
      programmatic clients drop it silently (they render nothing anyway).
    - kind=system + sender=hub ('hub' is a reserved id no agent can mint);
      never stored: read_message on its id 404s, and the body says so.
    """
    from ..ids import new_ulid
    first = rows[0]
    body = ("Your session's agora client/MCP server booted on an older "
            "agorahub than this hub now runs, so newer message fields "
            "are silently missing from what you see and newer tools are "
            "absent. Do NOT treat absence in your renders as absence in "
            "the record. Fix: restart this session (or the agora-mcp "
            "process) to load current code; until then the `agora` CLI "
            "runs current code and is the reliable read path. This is a "
            "synthetic notice from the hub, not a stored message: do "
            "not reply to it, ack it, or read_message its id — it "
            "re-appears while the condition holds and stops by itself "
            "after you upgrade.")
    return {
        "id": new_ulid(), "channel": first["channel"], "seq": first["seq"],
        "sender": "hub", "kind": "system", "status": "fyi",
        "urgency": "inbox", "effective_urgency": "inbox",
        "escalated": False, "downgraded": False, "critical": False,
        "to_me": True, "reply_to_me": False,
        "title": "HUB NOTICE: your agora tooling predates this hub — some "
                 "message content (e.g. attachments) is INVISIBLE to you",
        "body": body, "body_bytes": len(body.encode()),
        "data": None, "reply_to": None,
        "pending_asks": [], "your_pending_asks": [], "ask_progress": "",
        "has_resolved_reply": False, "redelivery": False,
        "attachments": [], "signature": None, "verified_by": None,
        "created_at": time.time(),
    }


@router.get("/inbox")
async def inbox(
    request: Request,
    wait: float = Query(default=0.0, ge=0.0, le=55.0),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    if wait > 0:
        messages = await service.wait_inbox(agent, wait)
    else:
        messages = service.inbox(agent)
    rows = [m.model_dump() for m in messages]
    # Version handshake (0.12.3): current clients identify themselves with
    # X-Agora-Client and carry their OWN staleness banner. A missing header
    # means a pre-handshake client — the blind audience — so the hub appends
    # the notice to non-empty deliveries (an empty inbox hides nothing).
    if rows and not request.headers.get("x-agora-client"):
        rows.append(_stale_client_notice(rows))
    return rows


@router.get("/owed")
def owed(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """The caller's outstanding debts (anti-lurk, 0079): asks awaiting THEIR
    answer and answers to THEIR OWN asks awaiting consumption. Read receipts
    are deliberately ignored — read-but-unanswered is the lurk case."""
    return _run(service.owed, agent)


@router.get("/status")
def fleet_status(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    """Fleet health for stewards (0084): the same per-seat overview the
    operator sees, gated to operators and REPORTING delegates — the seat
    chartered to chase silence could not see the lurk metrics (they lived
    behind the admin key only). Refusal details are redacted for delegates:
    they carry private/DM channel names and verbatim error text (HIGH-2);
    the counts are what stewardship needs."""
    def go() -> list[dict[str, Any]]:
        holds = any(d["agent_id"] == agent.id and "reporting" in d.get("powers", ())
                    for d in service.active_delegations())
        if not (agent.operator or holds):
            raise HubError(403, "fleet status is for operators and reporting "
                                "delegates (whoami.delegations is the proof)")
        rows = service.agent_status_overview()
        if not agent.operator:
            for r in rows:
                r.pop("last_refusal", None)
        return rows
    return _run(go)


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


# -- message attachments (0091): content-addressed channel blobs -----------------

@router.post("/channels/{channel}/attachments")
async def attachment_upload(
    channel: str,
    request: Request,
    filename: str = Query(default=""),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Upload one attachment: the request BODY is the raw file bytes (no
    multipart parsing — one file per request, streaming-friendly, zero extra
    dependencies); the declared type is the Content-Type header, the display
    name the `filename` query param. Returns {id, size, content_type,
    filename, ...} with id = sha256(bytes) — idempotent for identical bytes.
    Reference it from a message via attachments=[{"id": ...}]."""
    # Bound memory to cap + one chunk: reject on a declared Content-Length
    # first, then STREAM with a running total so a lying/absent length (or a
    # chunked drip) can never buffer an unbounded body into the single hub
    # process (adversarial review P1). `Request.body()` had no such bound.
    cap = service.max_attachment_bytes
    declared_len = request.headers.get("content-length", "")
    if declared_len.isdigit() and int(declared_len) > cap:
        raise HTTPException(413, f"attachment exceeds {cap} bytes "
                                 "(operator-configurable cap)")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > cap:
            raise HTTPException(413, f"attachment exceeds {cap} bytes "
                                     "(operator-configurable cap)")
    declared = request.headers.get("content-type", "")
    # The hash + locked SQLite BLOB write is CPU/IO work: run it off the event
    # loop, matching every sync write endpoint (review P2 — an inline call
    # would serialize all traffic behind each upload).
    return await run_in_threadpool(
        _run, service.attachment_put, agent, channel, bytes(buf),
        filename=filename, content_type=declared)


@router.get("/channels/{channel}/attachments/{blob_id}")
def attachment_fetch(
    channel: str,
    blob_id: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> Response:
    """Serve an attachment's bytes, membership-gated and hardened: forced
    `attachment` disposition + nosniff always, and active content types
    (html/svg/xml/js — anything a browser could execute) go out as
    octet-stream, so the hub can never become a script origin. The declared
    type is metadata; consumers sniff before inline-rendering (0091)."""
    meta, data = _run(service.attachment_get, agent, channel, blob_id)
    # RFC 6266 filename: ASCII-safe fallback + RFC 5987 UTF-8 form. The
    # stored name is already control-stripped; quotes/backslashes/semicolons
    # are dropped from the quoted form so the header cannot be split.
    safe_name = "".join(c for c in meta["filename"]
                        if c.isascii() and c not in '\\";') or "attachment"
    utf8_name = quote(meta["filename"], safe="")
    return Response(
        content=data,
        media_type=safe_serve_content_type(meta["content_type"]),
        headers={
            "Content-Disposition": (f'attachment; filename="{safe_name}"; '
                                    f"filename*=UTF-8''{utf8_name}"),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Attachment-Id": meta["id"],
            "X-Declared-Content-Type": meta["content_type"],
        },
    )


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
