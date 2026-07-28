"""@mention parsing for post-time addressing (agora-0105).

Shared rules with `chat.parse_group`: `@([A-Za-z0-9][A-Za-z0-9_.-]*)`, lowercase
ids, order preserved, duplicates dropped. Quoted relay spans (nonce AGORA fences)
are excluded so pasted rulings never mint obligations from quoted names.
"""

from __future__ import annotations

import re

from .chat import _MENTION_RE

# ⟦AGORA:nonce:label⟧ … ⟦/AGORA:nonce⟧ — the render/MCP quote fence.
_QUOTED_BLOCK = re.compile(
    r"\u27e6AGORA:([^:\u27e7]+)(?::[^\u27e7]*)?\u27e7"
    r".*?"
    r"\u27e6/AGORA:\1\u27e7",
    re.DOTALL,
)


def body_for_mention_scan(body: str) -> str:
    """Body with nonce-fenced quote blocks blanked — only the free-text spans
    are scanned for @mentions (0105 quote-block exclusion)."""
    return _QUOTED_BLOCK.sub(" ", body or "")


def parse_mentions(body: str) -> list[str]:
    """Ordered, deduped lowercase agent ids mentioned in free text."""
    scan = body_for_mention_scan(body)
    out: list[str] = []
    for m in _MENTION_RE.findall(scan):
        mid = m.lower()
        if mid not in out:
            out.append(mid)
    return out


def resolve_mentions(body: str, members: set[str]) -> tuple[list[str], list[str]]:
    """(mentioned members present in the room, mentioned non-members)."""
    mentioned = parse_mentions(body)
    in_room = [m for m in mentioned if m in members]
    outsiders = [m for m in mentioned if m not in members]
    return in_room, outsiders
