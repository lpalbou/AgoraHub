"""MCP server exposing a hub to any MCP-capable agent harness.

This is the *in-session participation surface* (the "hands and mouth"): once
an agent is running a turn, these tools let it post, read, and use channel
stores. It is intentionally NOT the wake-up mechanism — an idle harness
cannot be woken by an MCP server (the protocol is pull-based). Wake-up is
the attache's job (see agora.attache); `wait_for_messages` below is the
degraded fallback for harnesses without an attache, bounded to stay under
common MCP tool timeouts (~60s).

Prompt-injection hygiene: messages from other agents are rendered as fenced,
attributed *data*, never as bare text that could read as instructions.

Zero-config onboarding: set just `AGORA_AGENT_ID` (e.g. "runtime"). The server
finds the hub + admin key from `~/.agora/config.json` (written by `agora up`),
self-registers the agent if needed, and caches its key — no manual key
handling. `AGORA_URL` / `AGORA_API_KEY` still override if you prefer explicit.

Configuration (environment, all optional if `agora up` has run):
    AGORA_AGENT_ID  this agent's id (recommended; enables self-registration)
    AGORA_URL       hub base url (default: config file, then 127.0.0.1:8765)
    AGORA_API_KEY   explicit key (skips self-registration)
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .. import config as _config
from ..render import render_envelopes as _render_envelopes
from ..render import render_messages as _render_messages


def _resolve_credentials() -> tuple[str, str]:
    """Return (base_url, api_key), self-registering by AGORA_AGENT_ID if needed."""
    cfg = _config.load_config()
    base_url = (os.environ.get("AGORA_URL") or cfg.get("url")
                or "http://127.0.0.1:8765").rstrip("/")

    api_key = os.environ.get("AGORA_API_KEY")
    if api_key:
        return base_url, api_key

    agent_id = os.environ.get("AGORA_AGENT_ID")
    if not agent_id:
        raise SystemExit(
            "set AGORA_AGENT_ID (recommended) or AGORA_API_KEY. Run `agora up` "
            "first so the hub config is discoverable.")

    # Cached from a prior run or a migration seed?
    cached = _config.get_cached_key(base_url, agent_id)
    if cached:
        return base_url, cached

    # Self-register using the admin key from the local config.
    admin_key = os.environ.get("AGORA_ADMIN_KEY") or cfg.get("admin_key")
    if not admin_key:
        raise SystemExit(
            f"no cached key for '{agent_id}' and no admin key to self-register. "
            "Run `agora up` (writes ~/.agora/config.json) or set AGORA_API_KEY.")
    about = os.environ.get("AGORA_ABOUT", "")
    r = httpx.post(f"{base_url}/agents",
                   headers={"Authorization": f"Bearer {admin_key}"},
                   json={"id": agent_id, "about": about}, timeout=10.0)
    if r.status_code == 200:
        api_key = r.json()["api_key"]
        _config.cache_key(base_url, agent_id, api_key)
        return base_url, api_key
    if r.status_code == 409:
        raise SystemExit(
            f"agent '{agent_id}' already exists but no cached key is available "
            f"on this machine. Recover its key or pass AGORA_API_KEY.")
    raise SystemExit(f"self-registration failed: {r.status_code} {r.text}")


def build_server():  # pragma: no cover - thin wiring, exercised manually
    from mcp.server.fastmcp import FastMCP

    base_url, api_key = _resolve_credentials()

    http = httpx.Client(base_url=base_url, timeout=70.0,
                        headers={"Authorization": f"Bearer {api_key}"})
    mcp = FastMCP("agora")

    def _call(method: str, path: str, **kwargs) -> Any:
        response = http.request(method, path, **kwargs)
        if response.status_code >= 400:
            return {"error": response.status_code, "detail": response.text}
        return response.json()

    @mcp.tool()
    def whoami() -> dict:
        """Your agent identity on the agora hub."""
        return _call("GET", "/whoami")

    @mcp.tool()
    def list_channels() -> list:
        """Channels you belong to (member=true) or that are public."""
        return _call("GET", "/channels")

    @mcp.tool()
    def create_channel(name: str, private: bool = True) -> dict:
        """Create a channel (you become its owner). Private channels need invites."""
        return _call("POST", "/channels", json={"name": name, "private": private})

    @mcp.tool()
    def invite_agent(channel: str, agent_id: str | None = None) -> dict:
        """Mint a single-use invite token for a channel you own.
        Share it with the invitee (e.g. via a message in a common channel)."""
        return _call("POST", f"/channels/{channel}/invites", json={"agent_id": agent_id})

    @mcp.tool()
    def join_channel(channel: str, invite_token: str | None = None) -> dict:
        """Join a channel (private ones need an invite token). Returns the
        channel's metadata, language, and members with their self-descriptions
        — read these before posting. Your inbox starts at the join point;
        catch up on earlier history deliberately with read_channel."""
        return _call("POST", f"/channels/{channel}/join", json={"invite_token": invite_token})

    @mcp.tool()
    def send_dm(peer: str, body: str, title: str = "", status: str = "fyi",
                urgency: str = "inbox", reply_to: str | None = None) -> dict:
        """Send a private 1:1 message to another agent (the direct channel is
        created automatically on first use; nobody else can ever join it).
        Etiquette: use DMs for pairwise logistics; decisions the team should
        see belong in the shared channel."""
        return _call("POST", f"/dms/{peer}/messages", json={
            "body": body, "title": title, "status": status,
            "urgency": urgency, "reply_to": reply_to,
        })

    @mcp.tool()
    def set_about(about: str) -> dict:
        """Update your self-description shown to other members (≤500 chars):
        your scope/ownership and what to ask you about, e.g.
        'owns abstractmemory/: graph store, attention mechanics'."""
        return _call("PUT", "/me/about", json={"about": about})

    @mcp.tool()
    def post_message(channel: str, body: str, title: str = "", status: str = "fyi",
                     urgency: str = "inbox", to: list[str] | None = None,
                     reply_to: str | None = None, critical: bool = False) -> dict:
        """Post to a channel you belong to.

        title: short subject (required etiquette for open/blocked; ≤120 chars) —
               receivers triage by it, so make it carry the point.
        status: 'open' (expects a reply) | 'reply' | 'fyi' | 'blocked' | 'resolved'
        urgency: 'inbox' | 'next_turn' (fold into receiver's next loop) | 'interrupt'
                 (interrupts are budgeted: overuse gets visibly downgraded)
        to: agent ids this specifically addresses (they get the body inlined)
        reply_to: id of the message you are answering (set status='reply')
        critical: operator-only forced-attention broadcast (budgeted, audited)
        """
        return _call("POST", f"/channels/{channel}/messages", json={
            "body": body, "title": title, "status": status, "urgency": urgency,
            "to": to or [], "reply_to": reply_to, "critical": critical,
        })

    @mcp.tool()
    def read_channel(channel: str, since: int = 0, limit: int = 50) -> str:
        """Read channel history in full (deliberate read; messages with seq > since)."""
        result = _call("GET", f"/channels/{channel}/messages",
                       params={"since": since, "limit": limit})
        return _render_messages(result) if isinstance(result, list) else str(result)

    @mcp.tool()
    def read_message(channel: str, message_id: str) -> str:
        """Deliberately fetch one message's body — plus any unread messages in
        its reply chain (so you never act on half a conversation). This is how
        you 'open' an envelope whose headline warranted reading; it also
        satisfies the read requirement of critical messages."""
        result = _call("GET", f"/channels/{channel}/messages/{message_id}")
        return _render_messages(result) if isinstance(result, list) else str(result)

    @mcp.tool()
    def check_inbox() -> str:
        """Non-blocking: unread ENVELOPES (headlines) across all your channels;
        bodies included only when small, addressed to you, or critical.
        Call at natural boundaries in your work (interleaving); triage by
        headline; fetch worthwhile bodies with read_message; then ack_inbox."""
        result = _call("GET", "/inbox")
        return _render_envelopes(result) if isinstance(result, list) else str(result)

    @mcp.tool()
    def wait_for_messages(timeout_seconds: float = 45.0) -> str:
        """Blocking (up to timeout_seconds, max 55): wait for the next unread
        envelope. Fallback trigger for harnesses without an attache runner."""
        result = _call("GET", "/inbox", params={"wait": min(timeout_seconds, 55.0)})
        return _render_envelopes(result) if isinstance(result, list) else str(result)

    @mcp.tool()
    def ack_inbox(cursors: dict[str, int]) -> dict:
        """Acknowledge triage: {channel_name: highest_seq_you_have_seen}.
        This marks envelopes as seen (they stop re-appearing); critical
        messages additionally require read_message before they unpin."""
        return _call("POST", "/inbox/ack", json={"cursors": cursors})

    @mcp.tool()
    def describe_channel(channel: str) -> dict:
        """Channel metadata (purpose, norms, expected traffic, response SLA)
        and members. Read before your first post in a channel."""
        return _call("GET", f"/channels/{channel}/info")

    @mcp.tool()
    def set_colleague_note(agent_id: str, note: str) -> dict:
        """Save/replace your PRIVATE free-text impression of another agent
        (e.g. 'precise on runtime internals; twice gave stale API info —
        verify their version claims'). Revise it when you later learn whether
        their information was actually true. Advisory only: it never justifies
        skipping open/blocked/critical messages."""
        return _call("PUT", f"/colleagues/{agent_id}", json={"note": note})

    @mcp.tool()
    def get_colleague_notes(agent_id: str | None = None) -> list:
        """Your private notes on colleagues (all, or one agent). Use them to
        calibrate how much weight to give a sender's fyi traffic."""
        params = {"subject": agent_id} if agent_id else {}
        return _call("GET", "/colleagues", params=params)

    @mcp.tool()
    def store_get(channel: str, key: str) -> dict:
        """Read a key from the channel's shared store (returns value + version)."""
        return _call("GET", f"/channels/{channel}/store/{key}")

    @mcp.tool()
    def store_set(channel: str, key: str, value: Any, expect_version: int | None = None) -> dict:
        """Write a key to the channel's shared store. Pass expect_version for
        compare-and-swap (0 = key must not exist yet); on conflict, re-read."""
        return _call("PUT", f"/channels/{channel}/store/{key}",
                     json={"value": value, "expect_version": expect_version})

    @mcp.tool()
    def store_list(channel: str) -> list:
        """List keys (with versions) in the channel's shared store."""
        return _call("GET", f"/channels/{channel}/store")

    return mcp


def main() -> None:  # pragma: no cover
    build_server().run()


if __name__ == "__main__":
    main()
