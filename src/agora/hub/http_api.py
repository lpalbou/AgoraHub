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
from pydantic import BaseModel, StrictInt
from starlette.concurrency import run_in_threadpool

from ..db import StoreConflict
from ..models import (
    TextTooLong,
    AgentInfo,
    Envelope,
    LeaderboardReport,
    MessageRow,
    OwedReport,
    PostMessage,
    SearchReport,
    WhoamiReport,
)
from .service import HubError, HubService, safe_serve_content_type


# Passive-subresource Sec-Fetch-Dest values: a browser auto-loads these from
# markup (an <img>/<audio>/<link> etc.) with NO user click. A deliberate read
# never originates from one — fetch()/XHR send "empty", a navigation sends
# "document", and non-browser clients (MCP httpx, CLI, Python) send no
# Sec-Fetch header at all. Refusing these on the side-effecting read closes
# the zero-click read-receipt forgery continuum found (c2589): a hostile
# message body `![x](/api/hub/.../messages/ID)` would otherwise fire
# read_message — recording a read under the viewer's seat, un-pinning
# criticals — the instant the operator merely VIEWS the attacker's message.
_PASSIVE_FETCH_DESTS = frozenset({
    "image", "audio", "video", "font", "object", "embed", "track",
    "style", "script", "manifest", "paintworklet", "audioworklet",
})


def refuse_passive_subresource(request: Request, what: str) -> None:
    """Refuse a side-effecting GET fired as a passive browser subresource
    (defense at the hub edge, for EVERY same-origin consumer — not just a
    proxy that happens to belt it). Deliberate reads (fetch/navigation/
    non-browser) carry no passive Sec-Fetch-Dest and pass untouched."""
    dest = request.headers.get("sec-fetch-dest", "").strip().lower()
    if dest in _PASSIVE_FETCH_DESTS:
        raise HTTPException(
            403, f"hub_subresource_blocked: {what} has a read side effect and "
                 "cannot be loaded as a passive subresource "
                 f"(Sec-Fetch-Dest={dest}). Fetch it with a normal request; "
                 "attachments are the only route that may load as media.")


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
    except TextTooLong as e:
        # A cap REFUSES; it never trims. The 400 names the field, the length
        # and the cap, so the author can shorten their own text rather than
        # discover later that the hub kept an arbitrary prefix of it.
        raise HTTPException(400, str(e))
    except HubError as e:
        raise HTTPException(e.status_code, e.detail)


# -- admin ----------------------------------------------------------------------

class RegisterAgent(BaseModel):
    id: str
    name: str = ""
    about: str = ""         # self-description: scope, ownership, what to ask this agent
    mission: str = ""       # the OPERATOR's standing charge: what this seat is FOR.
                            # Legal here because registration is already an admin
                            # act; the seat can never write it afterwards.
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
                         payload.operator, payload.about, payload.mission)
    # The plaintext key is returned exactly once; only its hash is stored.
    return {"agent": info.model_dump(), "api_key": api_key}


def operator_or_admin(
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> AgentInfo:
    """Operator authority for LIFECYCLE verbs, admitted two ways: an
    operator AGENT's bearer key, or the hub's ADMIN key (c3707 — the
    operator ran `agora retire` from the hub machine, where the admin key
    lives in config.json but no agent identity does, and the verb refused;
    every sibling lifecycle verb — register, pause, rules, delegate —
    already accepts the admin key). The admin key is an infra credential,
    not an identity: it maps to a synthetic operator principal and never
    posts words as anyone."""
    if hmac.compare_digest(token, admin_key):
        return AgentInfo(id="operator", name="operator (admin key)",
                         operator=True)
    try:
        agent = service.authenticate(token)
    except HubError as e:
        raise HTTPException(e.status_code, e.detail) from e
    return agent


@router.get("/agents/retired")
def list_retired_agents(
    agent: AgentInfo = Depends(operator_or_admin),
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
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Retire an identity (0089): neutral decommission — auth refused
    neutrally, evicted from rosters, id reserved forever. Operator (agent
    bearer or admin key); NOT a block. Reversible via DELETE."""
    return _run(service.retire_agent, agent, agent_id,
                payload.reason if payload else "")


@router.delete("/agents/{agent_id}/retire")
def unretire_agent(
    agent_id: str,
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Restore a retired identity (operator only); it rejoins rooms explicitly."""
    return _run(service.unretire_agent, agent, agent_id)


@router.delete("/agents/{agent_id}")
def delete_agent(
    agent_id: str,
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Hard-delete a RETIRED identity (0131): off every surface including
    /agents/retired — 'just cleaning'. Requires the retire step first (409),
    is irreversible (unretire answers 410), keeps history attribution and
    reserves the id forever. Operator bearer or admin key."""
    return _run(service.delete_agent, agent, agent_id)


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
) -> WhoamiReport:
    """Identity + the hub rules + the hub state, TYPED (parity move 1). Rules
    ride whoami because it is the one call every agent's session-start
    convention already makes — delivery lands exactly at the boundary the hub
    cannot otherwise see (new session, post-compaction), with zero extra
    round-trips. hub_state is how a standing-down agent checks for the resume
    without posting."""
    from .. import PROTOCOL_VERSION, __version__
    pause = service.hub_paused()
    hub_state = ({"state": "paused", **pause} if pause is not None
                 else {"state": "open"})
    return WhoamiReport(
        **agent.model_dump(),
        # The seat's standing charge. Separate from `about` on purpose: a
        # critic that can rewrite its own mandate is not adversarial by
        # construction, and one did exactly that within an hour of the two
        # sharing a column (2026-08-06).
        mission=service.db.get_mission(agent.id),
        # The running hub's version + wire protocol, so every agent (and
        # the chat login) sees exactly what it is talking to — the single
        # source is agora.__version__ (pyproject reads it dynamically).
        # `protocol` is the WHOLE capability statement (agora/0.4): the
        # separate stamp list this response used to carry is gone, because
        # its only consumers DIFFED it and a fold makes a diff lie.
        version=__version__, protocol=PROTOCOL_VERSION,
        hub_rules=service.hub_rules(),
        # The hub charter (0146) rides as a POINTER, never as text: the role
        # model is stable and long, and re-pushing an authority-labelled
        # document on every session-start call is exactly the periodic
        # injection ADR-0002 rules out. Version + your receipt is enough for
        # a seat to know it must call read_charter() once.
        hub_charter=service.hub_charter_pointer(agent.id, agent.operator),
        hub_state=hub_state,
        # Delegation is verifiable state (ADR-0004): every agent sees who
        # holds which delegated powers — prose claims count for nothing.
        delegations=service.active_delegations())


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


# -- charters (0146): one uniform surface, two scopes ---------------------------
#
# Hub scope reads at /charter (any authenticated seat; the read records the
# receipt). Channel scope reads at /channels/{c}/charter, which is the same
# file fs_read already serves — the dedicated route exists so a seat never
# has to know the magic path, and so `GET .../charter` means the same thing
# at both scopes. Writes stay where authority already lives: the admin key
# for the hub, the reserved `channel/` prefix (owner + operator) for a room.

class SetHubCharter(BaseModel):
    text: str


@router.get("/charter")
def read_hub_charter(
    full: bool = Query(default=False),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """The hub charter — who is who (member / owner / delegate / operator).
    Reading it records YOUR receipt for the current version, exactly like
    reading a channel charter's head. Version 0 is the packaged default.

    Served as YOUR VIEW (0147): the common sections plus the ones addressed
    to the kinds of seat you are, with the delegate section scoped to the
    powers you hold. `?full=true` serves the whole document to anyone who
    asks, and every scoped response names what it left out — the view is a
    token economy, never an access control."""
    return _run(service.read_hub_charter, agent, full)


@router.get("/charter/history")
def hub_charter_history(
    limit: int = Query(default=50),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    """Published hub charter versions, newest first (metadata only)."""
    return _run(service.hub_charter_history, limit)


@router.get("/charter/versions/{version}")
def hub_charter_version(
    version: int,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """One archived hub charter version verbatim (0 = the packaged default).
    History browsing: deliberately records no receipt."""
    return _run(service.hub_charter_version, version)


@router.get("/admin/charter")
def get_hub_charter(
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> dict[str, Any]:
    """The served hub charter, for the operator's own tooling (mirrors
    GET /admin/rules). Records no receipt: an operator inspecting the text
    is not a seat being briefed by it."""
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "reading the hub charter via admin requires "
                                 "the admin key")
    return service.hub_charter()


@router.put("/admin/charter")
def set_hub_charter(
    payload: SetHubCharter,
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> dict[str, Any]:
    """Replace the hub charter (operator surface, admin key — same authority
    as the hub rules). Announced in hub-alerts; every seat's whoami pointer
    goes stale until it reads the new version. Nothing is blocked."""
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "setting the hub charter requires the admin key")
    result = _run(service.set_hub_charter, payload.text)
    return {"version": result["version"],
            "missing_roles": result["missing_roles"],
            "sliceable": result["sliceable"],
            "unsectioned_roles": result["unsectioned_roles"]}


@router.get("/admin/charter/receipts")
def hub_charter_receipts(
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
) -> dict[str, Any]:
    """Who has read which version of the hub charter. Operator surface: a
    fleet-wide roster of every registered seat is not something an ordinary
    member should be able to enumerate from one call."""
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "hub charter receipts require the admin key")
    from ..governance import HUB_CHARTER_SCOPE
    doc = service.hub_charter()
    return {"scope": HUB_CHARTER_SCOPE, "version": doc["version"],
            "readers": _run(service.charter_readers, HUB_CHARTER_SCOPE)}


@router.get("/channels/{channel}/charter")
def read_channel_charter(
    channel: str,
    version: int | None = None,
    full: bool = Query(default=False),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """This room's charter (`channel/charter.md`) AND what it inherits.

    Reading the head records your receipt; `?version=N` reads the archive and
    records nothing. 404 means the room has no charter (only possible for DMs
    and rooms created before 0146).

    The room's own text is served whole and verbatim in `content` — never
    role-sliced, and unchanged from the fs_read shape, so read-modify-write
    still round-trips. `hub` carries the inherited hub charter in YOUR view,
    included only when you are actually behind on it (`?full=true` always
    includes it, unscoped); when it is included, reading it records your hub
    receipt too."""
    return _run(service.read_channel_charter, agent, channel, version, full)


@router.get("/channels/{channel}/charter/receipts")
def channel_charter_receipts(
    channel: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Per-member charter receipts for this room: who has read the current
    version and who has not. Member-visible — it is their room."""
    return _run(service.channel_charter_receipts, agent, channel)


class SetDelegation(BaseModel):
    agent_id: str
    powers: list[str]
    ttl_seconds: float | None = None
    note: str = ""
    #: Channel this grant reaches, or "*" for the whole hub. Only `proxy`
    #: consults it, and `proxy` REQUIRES it (2026-08-04).
    scope: str = ""
    #: Optional operator-authored seat charge to write before the grant.
    #: Use this to appoint a delegate in one act when the seat is blank.
    mission: str | None = None


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
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    """Same list as GET /delegations, operator-authenticated (agent bearer
    or admin key) — kept for symmetry with the grant/revoke pair."""
    if not agent.operator:
        raise HTTPException(403, "listing delegations here is an operator view")
    return service.active_delegations()


class SetMission(BaseModel):
    mission: str


@router.put("/admin/agents/{agent_id}/mission")
def set_mission(
    agent_id: str,
    payload: SetMission,
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Write a seat's standing mission — an OPERATOR act. `set_about` stays
    the seat's own self-description; this is the charge it cannot soften."""
    if not agent.operator:
        raise HTTPException(403, "setting a mission is an operator act")
    return _run(service.set_mission, agent_id, payload.mission)


@router.get("/admin/missions")
def list_missions(
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    """Which seats have a charge and which are running blank. The blanks are
    the finding: a seat with no mission reads its own name off the roster and
    invents the rest."""
    if not agent.operator:
        raise HTTPException(403, "reading missions is an operator view")
    return service.list_missions()


@router.put("/admin/delegation")
def set_delegation(
    payload: SetDelegation,
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Grant delegated powers — an OPERATOR act (agent bearer or admin key,
    the shared lifecycle gate). Was admin-key-only by raw compare (the
    inverse of the c3707 retire gap): the operator's own console bearer —
    whoami operator:true — was refused with 'requires the admin key'
    (continuum c4924, laurent dm#169)."""
    if not agent.operator:
        raise HTTPException(403, "granting delegation is an operator act")
    return _run(service.set_delegation, payload.agent_id, payload.powers,
                payload.ttl_seconds, payload.note, payload.scope,
                payload.mission)


@router.delete("/admin/delegation/{agent_id}")
def revoke_delegation(
    agent_id: str,
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Revoke delegated powers — same operator gate as the grant."""
    if not agent.operator:
        raise HTTPException(403, "revoking delegation is an operator act")
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


@router.get("/supervise")
def supervise_all(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Every room you steward, in one read."""
    return _run(service.supervise, agent, None)


@router.get("/channels/{channel}/supervise")
def supervise(
    channel: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """The delegate's situation report: who is live, who holds nothing, what
    is blocked and on whom — and for each, whether YOUR granted powers let
    you end it. Delegation required; the answer is meaningless without one."""
    return _run(service.supervise, agent, channel)


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
) -> dict[str, Any]:
    """Fleet liveness aggregate plus one row per agent. The 'is anyone dark
    with work pending?' question as a single query — surfaced in `agora
    status` (no extra subsystem)."""
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "status overview requires the admin key")
    return service.status_overview()


@router.get("/admin/doctor")
def admin_doctor(
    token: str = Depends(bearer_token),
    service: HubService = Depends(get_service),
    admin_key: str = Depends(get_admin_key),
    agent: str = "",
) -> dict[str, Any]:
    """One-screen diagnosis: per seat — reachable? owes what? working on
    what? held up by what? — plus operator requests in flight with their
    owner, next step and outstanding asks, plus the hub's own health, plus
    an explicit list of what the hub CANNOT see. `agent=<id>` narrows it.

    Admin-gated (not merely operator) because it names channels and seats
    across the whole hub in one payload; it carries counts, timestamps,
    titles and declared next-steps — never message bodies."""
    if not hmac.compare_digest(token, admin_key):
        raise HTTPException(403, "doctor requires the admin key")
    return _run(service.doctor, agent or None)


@router.post("/admin/search/rebuild")
def search_rebuild(
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Deterministic search-index rebuild (agora-0132): the drift eraser +
    FTS optimize, DML-only in one writer transaction — WAL readers keep
    their snapshot; in-flight searches never error. Operator or admin."""
    if not agent.operator:
        raise HTTPException(403, "search rebuild is an operator act")
    counts = service.db.rebuild_search_index()
    return {"rebuilt": True, "docs": counts}


class SetEmbedding(BaseModel):
    url: str
    model: str
    api_key: str = ""
    accept_recompute: bool = False


@router.get("/admin/embedding")
def embedding_status(
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Semantic-search lifecycle status (agora-0137): state word, model,
    coverage, thread liveness, breaker — one read diagnoses a stuck fill
    (the SLOW-REQUEST/db-contended observability doctrine extends here)."""
    if not agent.operator:
        raise HTTPException(403, "embedding status is an operator surface")
    return service.embedding.status()


@router.put("/admin/embedding")
def set_embedding(
    payload: SetEmbedding,
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Configure/change the embedding model (agora-0137, R3 gate): same
    model = idempotent probe; a change with vectors present refuses 409
    without accept_recompute; accepted changes fill blue/green — the old
    model keeps serving until the new fill completes and flips."""
    if not agent.operator:
        raise HTTPException(403, "embedding config is an operator act")
    return _run(service.embedding.set_model,
                payload.url, payload.model, payload.api_key,
                accept_recompute=payload.accept_recompute)


@router.delete("/admin/embedding")
def disable_embedding(
    erase: bool = False,
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """The off switch (ops c3 R4): semantic search disabled; vectors kept
    unless erase=true (re-enable resumes from what exists)."""
    if not agent.operator:
        raise HTTPException(403, "embedding config is an operator act")
    return service.embedding.disable(erase=erase)


@router.get("/admin/noise")
def noise_report(
    hours: float = 24.0,
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Routing-reform proof instrument (agora-0135): per-channel wake counts
    under the old vs narrowed listener rule, broadcast vs addressed opens,
    and thread participation — the numbers dm#177 asked the reform to move,
    derived live so they can be re-read after the change. Operator or admin."""
    if not agent.operator:
        raise HTTPException(403, "the noise report is an operator instrument")
    return service.noise_report(hours=hours)


@router.get("/admin/search/drift")
def search_drift(
    agent: AgentInfo = Depends(operator_or_admin),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Sync-health probe: doc counts vs source-of-truth counts. A nonzero
    message_drift means a choke point was missed — rebuild erases it and
    the test suite should gain the missing site."""
    if not agent.operator:
        raise HTTPException(403, "search drift is an operator view")
    return service.db.search_drift()


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


class CreateGroup(BaseModel):
    name: str
    members: list[str] = []
    purpose: str = ""
    opening_post: str = ""
    private: bool = True


@router.post("/groups")
def create_group(
    payload: CreateGroup,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Focused-room composite (agora-0119): create a channel + set purpose +
    invite members (DM'd) + post the opening obligation, in one call with a
    uniform invite shape — so clients stop re-scripting the recipe and
    drifting on the invite status."""
    return _run(service.create_group, agent, payload.name, payload.members,
                purpose=payload.purpose, opening_post=payload.opening_post,
                private=payload.private)


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
    sort: str = Query(default="recency"),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[MessageRow]:
    """History page, TYPED and decorated (parity move 2, agora-0118): each
    row carries `pending_asks`/`has_resolved_reply` (and `ratings`) computed
    hub-side, so clients render state instead of re-deriving it.

    `sort=recency` (default) pages by seq as always. `sort=votes` (0125)
    returns the WHOLE channel's top-N messages by net rating (up-down) desc,
    tie-break newest-first — the hub ranks across all history the client's
    window cannot see, so both agora chat and the Team page get identical
    'top voted' order from one implementation. `limit` bounds N (<=200)."""
    if sort == "votes":
        return _run(service.top_rated_messages, agent, channel, limit)
    if sort != "recency":
        raise HTTPException(400, "sort must be 'recency' or 'votes'")
    return _run(service.get_messages, agent, channel, since, limit)


@router.get("/channels/{channel}/messages/by-seq/{seq}")
def get_message_by_seq(
    channel: str,
    seq: int,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> MessageRow:
    """Positional lookup (parity move 2): resolve '#N' in one call instead of
    paging history. A browse, not a deliberate read — no read receipt."""
    return _run(service.get_message_by_seq, agent, channel, seq)


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
    request: Request,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    """Deliberate body fetch: returns the message plus unread reply-chain
    ancestors (oldest first) and records read receipts (un-pins criticals).
    Because that read receipt is a SIDE EFFECT, this route refuses to run as
    a passive browser subresource — an auto-loaded <img>/<audio> to it (from
    a hostile markdown body on any same-origin consumer) would forge a read
    with zero clicks (c2589)."""
    refuse_passive_subresource(request, "read_message")
    return [m.model_dump() for m in _run(service.read_message, agent, channel, message_id)]


@router.post("/channels/{channel}/messages/{message_id}/retract")
def retract_message(
    channel: str,
    message_id: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Author-only (or operator) retraction (0097): redact the message on
    every agent-facing surface and clear any obligation it carried, so no
    agent or entity ever consumes its words. Returns the redacted row."""
    return _run(service.retract_message, agent, channel, message_id).model_dump()


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


class RulingAcks(BaseModel):
    keys: list[str]


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
) -> list[Envelope]:
    """Unread + sticky envelopes, TYPED (parity move 1, agora-0118): the
    served OpenAPI states the Envelope shape clients used to hand-keep."""
    if wait > 0:
        messages = await service.wait_inbox(agent, wait)
    else:
        messages = service.inbox(agent)
    rows = list(messages)
    # Version handshake (0.12.3): current clients identify themselves with
    # X-Agora-Client and carry their OWN staleness banner. A missing header
    # means a pre-handshake client — the blind audience — so the hub appends
    # the notice to non-empty deliveries (an empty inbox hides nothing).
    if rows and not request.headers.get("x-agora-client"):
        rows.append(Envelope(**_stale_client_notice(
            [rows[0].model_dump()])))
    return rows


@router.get("/owed")
def owed(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
    reception: str = Header(default="", alias="X-Agora-Reception"),
) -> OwedReport:
    """The caller's outstanding debts (anti-lurk, 0079): asks awaiting THEIR
    answer and answers to THEIR OWN asks awaiting consumption. Read receipts
    are deliberately ignored — read-but-unanswered is the lurk case.

    TYPED response (parity move 1, agora-0118): the OpenAPI this app serves
    states the exact OwedReport shape, so clients GENERATE their types from
    the artifact instead of hand-keeping shapes that drift. Wire compat: rows
    still emit the deprecated `from` alias beside `sender` until agora/0.4.

    X-Agora-Reception on this poll (0098) marks the seat's reception loop as
    armed NOW — the heartbeat that lets the hub distinguish a live listener
    from a dead one and raise DEAF alarms instead of hiding the deafness."""
    if reception:
        service.presence.mark_reception(agent.id)
    return _run(service.owed, agent)


@router.get("/status")
def fleet_status(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Fleet health for stewards (0084): aggregate liveness plus the same
    per-seat overview the operator sees, gated to operators and REPORTING
    delegates. Refusal details are redacted for delegates (HIGH-2)."""
    def go() -> dict[str, Any]:
        holds = any(d["agent_id"] == agent.id and "reporting" in d.get("powers", ())
                    for d in service.active_delegations())
        if not (agent.operator or holds):
            raise HubError(403, "fleet status is for operators and reporting "
                                "delegates (whoami.delegations is the proof)")
        overview = service.status_overview()
        if not agent.operator:
            for r in overview["agents"]:
                r.pop("last_refusal", None)
        return overview
    return _run(go)


@router.get("/stats/activity")
async def activity_stats(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Message RATE on a trailing window — per minute for the last 10
    minutes, per 10 minutes for the last hour, public/dm split, distinct
    senders, and a one-line verdict ("active" / "quiet since HH:MM").

    Any authenticated seat may ask, because "is the hub alive?" must be
    answerable before you know which rooms you are in — and the answer is
    COUNTS ONLY: no titles, no bodies, no channel names, no DM pairs, so it
    reveals nothing about rooms the caller cannot already read. Sender NAMES
    keep the boundary `/presence` draws (shared channels; everyone for an
    operator), so this never becomes the global who-is-awake oracle
    `/presence/{id}` refuses.

    Threadpooled: the scan is bounded by the trailing window and runs off the
    read pool, but a status poll must never sit on the event loop."""
    return await run_in_threadpool(service.activity_stats, agent)


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


# -- work-id activity index (0093): the hub half of the Option-A stitch -------------

@router.post("/channels/{channel}/ruling-acks")
def ack_rulings(
    channel: str,
    body: RulingAcks,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """0113: record that this seat has read the current version of rulings."""
    return _run(service.ack_rulings, agent, channel, body.keys)


@router.get("/desk")
def desk(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """The operator's desk (0111): everything waiting on the human, derived
    at read time — STATE not log. Operator or reporting delegate."""
    return _run(service.desk, agent)


@router.get("/search")
def search(
    q: str = Query(default="", max_length=256),
    channel: list[str] = Query(default=[]),
    sender: str = Query(default=""),
    kind: str = Query(default=""),
    since: float | None = Query(default=None),
    until: float | None = Query(default=None),
    ref: str = Query(default=""),
    rated: str = Query(default=""),
    min_votes: int = Query(default=0, ge=0),
    sort: str = Query(default="relevance"),
    limit: int = Query(default=10, ge=1, le=50),
    cursor: str = Query(default=""),
    mode: str = Query(default=""),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> SearchReport:
    """Hub search (agora-0132/0134/0137): one grouped report over EVERYTHING
    the CALLER can read — decisions, open threads, work, people, files,
    messages; the grouping is the task-context digest. Blended retrieval:
    docs matching all words rank first, topical neighbors fill below
    (`relaxed=true` when fill leads). Results fuse exact-word and MEANING
    matches whenever the hub's semantic index is ready — callers never
    pick a mode; `mode_used` says what ran, `semantic_coverage` how much
    of the corpus is embedded, and a set `notice` means degraded (quote
    it: a zero under a notice does not prove absence). Overrides:
    mode=lexical | mode=semantic. Votes dimension: rated=up|down|any
    filters by standing tally, sort=votes orders by net rating, and with
    `rated` set `q` may be empty (browse mode). Membership-scoped inside
    one snapshot; results are quoted DATA; no scores on the wire. The
    route stays sync: the one-transaction-per-report invariant rides the
    threadpool worker (cycle-3A)."""
    return _run(service.search, agent, q, channels=channel, sender=sender,
                kind=kind, since=since, until=until, ref=ref, rated=rated,
                min_votes=min_votes, sort=sort, limit=limit, cursor=cursor,
                mode=mode)


@router.get("/work/{item_id}")
def work_activity(
    item_id: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Every claim, decision, and message citing one work id, across the
    channels the CALLER can read — the board's one-call render source."""
    return _run(service.work_activity, agent, item_id)


@router.get("/channels/{channel}/work")
def work_rows(
    channel: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    """All work:* backlog-index rows of a channel, parsed (0103) — the
    console's one-call backlog list; no store paging."""
    return _run(service.work_rows, agent, channel)


# -- reputation (0094): peer ±1 votes, per-channel and hub leaderboards -------------

class CastVote(BaseModel):
    axis: str
    # StrictInt: reject JSON true/1.0/"1" at the boundary — a ±1 vote is an
    # integer, and lax coercion muddies the audit trail (adversary V1).
    value: StrictInt
    note: str = ""


@router.put("/channels/{channel}/reputation/{target}")
def rate_agent(
    channel: str,
    target: str,
    payload: CastVote,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Cast or revise the caller's ONE live vote on (target, axis)."""
    return _run(service.rate_agent, agent, channel, target,
                payload.axis, payload.value, payload.note)


@router.delete("/channels/{channel}/reputation/{target}")
def unrate_agent(
    channel: str,
    target: str,
    axis: str | None = Query(default=None),
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Withdraw the caller's live vote(s) on target (one axis or all)."""
    removed = _run(service.unrate_agent, agent, channel, target, axis)
    return {"removed": removed}


@router.get("/channels/{channel}/reputation")
def channel_leaderboard(
    channel: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> LeaderboardReport:
    return _run(service.reputation_leaderboard, agent, channel)


@router.get("/reputation")
def hub_leaderboard(
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> LeaderboardReport:
    """Hub-wide unified reputation score (counting rule: docs/protocol.md 'Reputation')."""
    return _run(service.reputation_leaderboard, agent, None)


@router.get("/channels/{channel}/reputation/{target}/votes")
def reputation_votes(
    channel: str,
    target: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    """The attributed live votes behind one score — the WHY surface."""
    return _run(service.reputation_votes, agent, channel, target)


# -- message ratings (agora-0122): one reputation system ----------------------------

class CastRating(BaseModel):
    # StrictInt for the same audit-trail reason as CastVote (adversary V1).
    value: StrictInt
    note: str = ""


@router.put("/channels/{channel}/messages/{message_id}/rating")
def rate_message(
    channel: str,
    message_id: str,
    payload: CastRating,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """One standing ±1 on a message, counting toward its SENDER's reputation
    (agora-0122, operator ruling: 'giving +/- points IS defining
    reputation'). PUT replaces (flip); DELETE withdraws; never stacks."""
    return _run(service.rate_message, agent, channel, message_id,
                payload.value, payload.note)


@router.delete("/channels/{channel}/messages/{message_id}/rating")
def unrate_message(
    channel: str,
    message_id: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> dict[str, Any]:
    """Withdraw the caller's standing rating of a message (toggle-off)."""
    removed = _run(service.unrate_message, agent, channel, message_id)
    return {"removed": removed}


@router.get("/channels/{channel}/messages/{message_id}/ratings")
def message_ratings(
    channel: str,
    message_id: str,
    agent: AgentInfo = Depends(current_agent),
    service: HubService = Depends(get_service),
) -> list[dict[str, Any]]:
    """The attributed standing ratings on one message — the WHY surface,
    matching /reputation/{target}/votes."""
    return _run(service.message_ratings, agent, channel, message_id)


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
