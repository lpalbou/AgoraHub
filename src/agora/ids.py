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


def new_token(prefix: str) -> str:
    """Opaque bearer secret, e.g. api keys and invite tokens."""
    return f"{prefix}_{os.urandom(24).hex()}"
