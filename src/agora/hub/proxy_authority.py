"""Explicit operator availability for acting under a scoped proxy grant.

Availability is a declaration, never inferred from missing heartbeats. The
existing grant remains a separate prerequisite; this module creates no role
or operational permission. HubService supplies db and the raw has_proxy check.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from ..models import AgentInfo


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class ProxyAuthorityMixin:
    AVAILABILITY_PREFIX = "availability:"

    def set_availability(self, agent: AgentInfo,
                         away_until: float | None) -> dict[str, Any]:
        """An authenticated operator declares their own absence or return.

        None means explicitly present. A finite future Unix timestamp is an
        expiring away lease. No caller may declare someone else absent.
        """
        # Imported at call time because HubService inherits this mixin.
        from .service import HubError

        if not agent.operator or not self.db.agent_is_operator(agent.id):
            raise HubError(403, "only an operator may declare their own availability")
        now = time.time()
        if away_until is not None:
            if not _finite_number(away_until) or away_until <= now:
                raise HubError(400, "away_until must be a finite future Unix timestamp, "
                                    "or null to declare yourself present")
            away_until = float(away_until)
        value = {"principal": agent.id,
                 "state": "present" if away_until is None else "away",
                 "away_until": away_until, "declared_by": agent.id,
                 "declared_at": now}
        self.db.meta_set(self.AVAILABILITY_PREFIX + agent.id,
                         json.dumps(value, separators=(",", ":"), allow_nan=False))
        return value

    def availability_for(self, principal: str) -> dict[str, Any]:
        """Return present/away/unknown; an expired declaration means unknown.

        Re-read durable state for each decision. Missing or malformed records
        confer no authority. Storage failures propagate rather than authorize.
        """
        unknown = {"principal": principal, "state": "unknown",
                   "away_until": None, "declared_by": None, "declared_at": None}
        if not isinstance(principal, str) or not self.db.agent_is_operator(principal):
            return unknown
        raw = self.db.meta_get(self.AVAILABILITY_PREFIX + principal)
        if raw is None:
            return unknown
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return unknown
        if (not isinstance(value, dict) or not set(unknown).issubset(value)
                or value.get("principal") != principal
                or value.get("declared_by") != principal):
            return unknown
        declared = value.get("declared_at")
        if not _finite_number(declared):
            return unknown
        state, until = value.get("state"), value.get("away_until")
        if state == "present" and until is None:
            return {key: value[key] for key in unknown}
        if (state != "away" or not _finite_number(until)
                or until <= declared):
            return unknown
        out = {key: value[key] for key in unknown}
        if until <= time.time():
            out["state"] = "unknown"
        return out

    def proxy_allowed(self, agent_id: str, channel: str,
                      principal: str) -> bool:
        """May this member decide for THIS explicitly absent operator now?

        A raw proxy grant is necessary but insufficient. Fresh grant lookup
        prevents the existing one-second display cache extending authority
        beyond revocation/expiry. Normal operational powers are unchanged.
        """
        if (not isinstance(agent_id, str) or not isinstance(channel, str)
                or not isinstance(principal, str) or agent_id == principal
                or not self.db.agent_is_operator(principal)
                or not self.db.is_member(channel, agent_id)
                or not self.has_proxy(agent_id, channel)):
            return False
        if not any(grant["agent_id"] == agent_id
                   and "proxy" in grant["powers"]
                   and grant.get("scope") in (channel, "*")
                   for grant in self.db.delegations_active()):
            return False
        return self.availability_for(principal)["state"] == "away"
