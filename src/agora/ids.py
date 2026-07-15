"""ULID generation (Crockford base32, lexicographically time-sortable).

Message ids must sort in creation order across the whole hub so that logs,
exports and debugging never need a secondary sort key. Canonical *channel*
ordering is still the hub-assigned per-channel `seq` (see docs/protocol.md);
the ULID is identity, not order authority.
"""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """Return a 26-char ULID: 48-bit ms timestamp + 80 bits of randomness."""
    ts_ms = int(time.time() * 1000)
    value = (ts_ms << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def ulid_timestamp(ulid: str) -> float | None:
    """Unix seconds encoded in a ULID's 48-bit prefix (first 10 chars), or
    None for anything that isn't a well-formed ULID. Lets consumers compute
    a message's age from its id alone — the 11-minute-latency incident
    (2026-07-15) was pure mis-attribution, undiagnosable at the wake surface
    because nothing there stated when the hub actually minted the message."""
    if not isinstance(ulid, str) or len(ulid) != 26:
        return None
    value = 0
    for ch in ulid[:10]:
        idx = _CROCKFORD.find(ch.upper())
        if idx < 0:
            return None
        value = (value << 5) | idx
    return value / 1000.0


def new_token(prefix: str) -> str:
    """Opaque bearer secret, e.g. api keys and invite tokens."""
    return f"{prefix}_{os.urandom(24).hex()}"
