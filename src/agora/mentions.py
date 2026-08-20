"""@mention parsing for post-time addressing (agora-0105).

Shared rules with `chat.parse_group`: `@([A-Za-z0-9][A-Za-z0-9_.-]*)`, lowercase
ids, order preserved, duplicates dropped. Quoted relay spans (nonce AGORA fences)
are excluded so pasted rulings never mint obligations from quoted names.

vfs references vs mentions — seat-identity precedence (operator ruling):
`@folder/file.md` and `@channel:folder/file.md` are vfs references, but a
token that exactly matches a REGISTERED seat id is a mention, always —
regardless of what follows it (`@laurent: hi` obliges laurent). Only tokens
that match no registered seat can be vfs references. The registry lives
hub-side, so the parser reports each candidate's shape (`path_like`) and
`resolve_mentions` applies the ruling; registry-free callers get the safe
subset from `parse_mentions` (path-like candidates dropped).
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


def parse_mention_candidates(body: str) -> list[tuple[str, bool]]:
    """Ordered, deduped ``(id, path_like)`` mention candidates in free text.

    ``path_like`` is True when every occurrence of the token is immediately
    followed by ``/`` or ``:`` — the vfs-reference shape (``@folder/file.md``
    in this channel's virtual file system, ``@channel:folder/file.md`` in
    another channel's). One plain occurrence anywhere clears the flag: the
    author demonstrably meant the seat. The parser reports shape only;
    whether a path-like candidate is a mention is decided by the caller
    against the seat registry (`resolve_mentions`).
    """
    scan = body_for_mention_scan(body)
    out: dict[str, bool] = {}
    for m in _MENTION_RE.finditer(scan):
        end = m.end()
        path_like = end < len(scan) and scan[end] in "/:"
        mid = m.group(1).lower()
        out[mid] = out.get(mid, True) and path_like
    return list(out.items())


def parse_mentions(body: str) -> list[str]:
    """Ordered, deduped lowercase agent ids of the plain mentions — the
    registry-free safe subset: path-like candidates are dropped, so a vfs
    reference can never mint a seat out of text alone."""
    return [mid for mid, path_like in parse_mention_candidates(body)
            if not path_like]


def resolve_mentions(body: str, members: set[str],
                     registered: set[str]) -> tuple[list[str], list[str]]:
    """(mentioned members present in the room, mentioned outsiders).

    Seat-identity precedence (operator ruling): a candidate that exactly
    matches a registered seat id is a mention, always — even written
    ``@seat/...`` or ``@seat:...``, the seat wins over the path reading.
    A path-like candidate matching no registered seat is a vfs reference:
    no mention, no obligation, and no outsider warning. Plain candidates
    keep their long-standing behavior: members oblige, the rest warn.
    """
    in_room: list[str] = []
    outsiders: list[str] = []
    for mid, path_like in parse_mention_candidates(body):
        if mid in members:
            in_room.append(mid)
        elif path_like and mid not in registered:
            continue        # vfs reference: not a seat, not noise
        else:
            outsiders.append(mid)
    return in_room, outsiders
