"""Shared Agora agent-id validation."""

from __future__ import annotations

import re

_AGENT_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")
_RESERVED_IDS = {"hub", "all"}


def validate_agent_id(agent_id: str) -> None:
    """Raise ValueError unless `agent_id` matches the hub's contract."""
    if not _AGENT_ID_RE.fullmatch(agent_id):
        raise ValueError("agent id must be lowercase ascii [a-z0-9_-], 1-64 chars, "
                         "no leading/trailing dash")
    if "--" in agent_id:
        raise ValueError("agent id may not contain '--' (reserved dm separator)")
    if agent_id in _RESERVED_IDS:
        raise ValueError(f"'{agent_id}' is a reserved id")
