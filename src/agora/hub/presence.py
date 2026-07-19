"""In-memory presence: is an agent offline, idle, or working?

Presence tells peers and the operator whether an agent is reachable NOW
(`who_is_reachable`, `agora status`): don't block on a quick reply from an
offline agent; an idle-but-alive one hears its listener. It is advisory,
not authoritative — an agent that crashes without saying goodbye simply
ages out.

Liveness is CONNECTION-derived, not heartbeat-derived: while an agent holds
at least one live WebSocket, it is present (its last declared state stands),
because a socket the hub can push to *is* reachability — no client-side
heartbeat protocol to forget. The staleness window only applies to agents
with no live connection. A peer that vanished without FIN (power loss, NAT
drop) keeps its refcount until the server's WS keepalive gives up — cmd_up
pins ws_ping_interval/ws_ping_timeout so that window is bounded (~40s).

THREAD-SAFETY INVARIANT: connect()/disconnect() (read-modify-write on the
refcount) run ONLY on the serving event loop (the WS endpoint). update()/
get() may run on threadpool workers (REST handlers) but are single
GIL-atomic dict operations. Do not call connect/disconnect from REST paths.
"""

from __future__ import annotations

import time

from ..models import Presence

_STALE_AFTER = 120.0    # no connection + no update for this long -> offline
_ACTIVE_WINDOW = 600.0  # REST activity within this window -> "active"
# Reception truth (0098): a seat's listener arms every ~250s (max-wait 240 +
# sleep 5 + startup), so ~3.5 missed cycles with no arm = the loop is dead,
# not merely slow. This is the line between "armed" and "DEAF".
_RECEPTION_STALE = 900.0


class PresenceTracker:
    def __init__(self) -> None:
        self._states: dict[str, Presence] = {}
        self._connections: dict[str, int] = {}  # agent_id -> live WS count
        self._last_seen: dict[str, float] = {}  # any authenticated activity
        # Reception heartbeat (0098): distinct from _last_seen. Set ONLY when
        # a seat's reception loop announces itself (the X-Agora-Reception
        # header on its every-arm /owed poll). This is what separates "the
        # listener is alive and arming" from "some stray call touched the
        # hub" — the exact conflation that hid uic/camera/framework deafness.
        self._last_reception: dict[str, float] = {}

    def touch(self, agent_id: str) -> None:
        """Record authenticated activity (REST or WS). An IDE tab working via
        MCP makes only REST calls — without this it reads 'offline' while
        visibly working (single GIL-atomic assignment; threadpool-safe)."""
        self._last_seen[agent_id] = time.time()

    def mark_reception(self, agent_id: str) -> None:
        """Record that this seat's RECEPTION loop just armed (0098). Called
        from the /owed handler when the request carries X-Agora-Reception —
        the listener already polls /owed at every arm, so this rides an
        existing call with zero new traffic."""
        self._last_reception[agent_id] = time.time()

    def reception(self, agent_id: str) -> tuple[str, float | None]:
        """(state, age_seconds) of a seat's reception loop. 'armed' = arming
        within the window; 'stale' = it WAS arming and stopped (the DEAF
        signal — only meaningful because we once saw it arm); 'unknown' =
        never announced (a seat that drives reception another way, or predates
        this feature) — deliberately never alarmed, absence is not death."""
        last = self._last_reception.get(agent_id)
        if last is None:
            return "unknown", None
        age = time.time() - last
        return ("armed" if age <= _RECEPTION_STALE else "stale"), age

    def connect(self, agent_id: str) -> None:
        """A push connection opened: the agent is reachable until it closes.
        The connection itself is a real presence event happening NOW, so a
        stored goodbye ("offline" from a previous life) is replaced with a
        fresh idle — otherwise a reconnecting agent reads "idle (updated 38m
        ago)" seconds after it connected (field bug, 2026-07-09)."""
        self._connections[agent_id] = self._connections.get(agent_id, 0) + 1
        presence = self._states.get(agent_id)
        if presence is None or presence.state == "offline":
            self._states[agent_id] = Presence(
                agent_id=agent_id, state="idle", updated_at=time.time())

    def disconnect(self, agent_id: str) -> None:
        remaining = self._connections.get(agent_id, 0) - 1
        if remaining > 0:
            self._connections[agent_id] = remaining
        else:
            self._connections.pop(agent_id, None)
            # Timestamp the goodbye so the staleness window starts now.
            self.update(agent_id, "offline")

    def update(self, agent_id: str, state: str) -> Presence:
        presence = Presence(agent_id=agent_id, state=state, updated_at=time.time())
        self._states[agent_id] = presence
        return presence

    def get(self, agent_id: str) -> Presence:
        presence = self._states.get(agent_id)
        if self._connections.get(agent_id):
            # Live socket = present. Report the declared state (idle/working)
            # with its REAL timestamp — fabricating time.time() here would let
            # a zombie socket (peer died without FIN, bounded by the WS
            # keepalive window) read as "updated just now" (audit M4).
            state = presence.state if presence and presence.state != "offline" else "idle"
            ts = presence.updated_at if presence else time.time()
            return Presence(agent_id=agent_id, state=state, updated_at=ts)
        # No push connection, but recently seen doing authenticated work
        # (an MCP/REST-only tab): "active" — reachable at its next turn
        # boundary, not by push. Distinct from truly dark.
        last_seen = self._last_seen.get(agent_id, 0.0)
        if time.time() - last_seen <= _ACTIVE_WINDOW:
            return Presence(agent_id=agent_id, state="active", updated_at=last_seen)
        if presence is None or time.time() - presence.updated_at > _STALE_AFTER:
            return Presence(agent_id=agent_id, state="offline", updated_at=0.0)
        return presence

