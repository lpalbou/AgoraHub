"""Per-agent token-bucket rate limiting.

This is a *safety* mechanism, not a fairness one: its job is to arrest
runaway agent-to-agent loops (two agents triggering each other forever),
which the design review ranked as the top operational risk of any
message-triggered agent system. Costs are bounded even if etiquette fails.
"""

from __future__ import annotations

import time


class RateLimiter:
    def __init__(self, rate_per_minute: float = 60.0, burst: float = 20.0) -> None:
        self._rate = rate_per_minute / 60.0
        self._burst = burst
        self._buckets: dict[str, tuple[float, float]] = {}  # agent -> (tokens, last_ts)

    def acquire(self, agent_id: str) -> float:
        """Take one token. Returns 0.0 on success, else the seconds until the
        next token — a computable pause instead of an opaque refusal, so a
        compliant bulk sender (imports, bridges, fan-outs) can sleep exactly
        that long rather than dying mid-run."""
        now = time.time()
        tokens, last = self._buckets.get(agent_id, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        if tokens < 1.0:
            self._buckets[agent_id] = (tokens, now)
            return (1.0 - tokens) / self._rate
        self._buckets[agent_id] = (tokens - 1.0, now)
        return 0.0

    def allow(self, agent_id: str) -> bool:
        return self.acquire(agent_id) == 0.0
