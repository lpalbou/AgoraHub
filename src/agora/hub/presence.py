"""In-memory presence: is an agent offline, idle, or working?

Presence informs *delivery strategy* (an attache may steer a working agent
but must resume/spawn an idle one) and is advisory, not authoritative — an
agent that crashes without saying goodbye simply ages out.
"""

from __future__ import annotations

import time

from ..models import Presence

_STALE_AFTER = 120.0  # seconds without a heartbeat -> considered offline


class PresenceTracker:
    def __init__(self) -> None:
        self._states: dict[str, Presence] = {}

    def update(self, agent_id: str, state: str) -> Presence:
        presence = Presence(agent_id=agent_id, state=state, updated_at=time.time())
        self._states[agent_id] = presence
        return presence

    def get(self, agent_id: str) -> Presence:
        presence = self._states.get(agent_id)
        if presence is None or time.time() - presence.updated_at > _STALE_AFTER:
            return Presence(agent_id=agent_id, state="offline", updated_at=0.0)
        return presence

    def all(self) -> list[Presence]:
        return [self.get(agent_id) for agent_id in self._states]
