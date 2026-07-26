"""OpenAI-compatible embeddings client + circuit breaker (agora-0137).

The embedder thread and the query path both come through here; the
breaker is SHARED so a flapping endpoint (LM Studio on the operator's
laptop — it quits, sleeps, and restarts as a matter of course) is
discovered once, not per caller. Two hard rules from the ops cycles:

- The QUERY path never waits on a broken endpoint: budget 2 s, and when
  the breaker is open the call refuses instantly (the caller serves
  lexical + notice). Fill calls get 30 s (big batches on a busy laptop).
- Backoff 1 s → 300 s doubling; ONE probe is allowed through at each
  half-open window (the cold probe happens here, never on a user query).

Query-side instruction (retrieval cycles, FROZEN): the Qwen3-Embedding
card default, verbatim. Measured n=22: instructed 0.601 vs raw 0.474
mean recall@25 — and a well-meaning custom instruction HALVED recall
(0.183) in cycle 1. Documents are embedded raw, never instructed.
This string is a constant on purpose; do not make it configurable.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

QUERY_INSTRUCTION = ("Instruct: Given a web search query, retrieve "
                     "relevant passages that answer the query\nQuery: ")

FILL_TIMEOUT = 30.0     # embed calls from the embedder thread
QUERY_TIMEOUT = 2.0     # embed calls on the search path (UX/ops budget)
BREAKER_MIN = 1.0
BREAKER_MAX = 300.0

#: The canary probe (ops cycle 2 P1): LM Studio can serve DIFFERENT
#: weights under the SAME model name — hashes match, nothing re-embeds,
#: and the index silently mixes geometries. The fingerprint of this
#: frozen string's embedding is stored at first fill and re-checked at
#: boot, breaker-close and flip; a mismatch refuses `ready`.
CANARY_TEXT = "agora canary v1: the quick brown fox indexes nine channels"


class BreakerOpen(Exception):
    """Refused without a network attempt — the endpoint is resting."""


class EmbedClient:
    def __init__(self, url: str, model: str, api_key: str = "") -> None:
        self.url = url.rstrip("/")
        self.model = model
        self._headers = ({"Authorization": f"Bearer {api_key}"}
                         if api_key else {})
        self._lock = threading.Lock()
        self._fail_at: float = 0.0      # last failure time
        self._backoff: float = 0.0      # 0 = closed
        self._probe_at: float = 0.0     # when the next probe is allowed

    # -- breaker ------------------------------------------------------------

    def breaker_state(self) -> dict[str, Any]:
        with self._lock:
            if self._backoff == 0.0:
                return {"state": "closed"}
            now = time.time()
            return {"state": "open", "backoff_seconds": self._backoff,
                    "retry_in": max(0.0, self._probe_at - now)}

    def _allow(self, *, is_probe_ok: bool) -> None:
        with self._lock:
            if self._backoff == 0.0:
                return
            now = time.time()
            if is_probe_ok and now >= self._probe_at:
                # half-open: this ONE call proceeds as the probe; push the
                # next window now so concurrent callers don't stampede.
                self._probe_at = now + self._backoff
                return
            raise BreakerOpen(
                f"embedding endpoint resting (retry in "
                f"{max(0.0, self._probe_at - now):.0f}s)")

    def _record(self, ok: bool) -> bool:
        """Returns True when this success CLOSED an open breaker (the
        canary re-check hook fires exactly then)."""
        with self._lock:
            if ok:
                closed = self._backoff != 0.0
                self._backoff = 0.0
                return closed
            self._fail_at = time.time()
            self._backoff = (min(self._backoff * 2, BREAKER_MAX)
                             if self._backoff else BREAKER_MIN)
            self._probe_at = self._fail_at + self._backoff
            return False

    # -- calls ---------------------------------------------------------------

    def embed(self, texts: list[str], *, timeout: float = FILL_TIMEOUT,
              probe: bool = True) -> tuple[list[list[float]], bool]:
        """Embed raw document texts. Returns (vectors, breaker_closed_now).
        Raises BreakerOpen without touching the network when resting, and
        httpx/ValueError on real failures (recorded)."""
        self._allow(is_probe_ok=probe)
        try:
            resp = httpx.post(
                f"{self.url}/embeddings",
                json={"model": self.model, "input": texts},
                headers=self._headers, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()
            rows = sorted(payload["data"], key=lambda d: d.get("index", 0))
            vectors = [r["embedding"] for r in rows]
            if len(vectors) != len(texts):
                raise ValueError(
                    f"endpoint returned {len(vectors)} embeddings for "
                    f"{len(texts)} inputs")
        except Exception:
            self._record(False)
            raise
        closed = self._record(True)
        return vectors, closed

    def embed_query(self, query: str) -> list[float]:
        """One instructed query embedding under the 2 s budget; never a
        probe (a user query must not be the breaker's test balloon)."""
        vectors, _ = self.embed([QUERY_INSTRUCTION + query],
                                timeout=QUERY_TIMEOUT, probe=False)
        return vectors[0]

    def canary_fingerprint(self) -> str:
        """The frozen probe's embedding, quantized then hashed — stable
        across restarts of the SAME weights, different for different
        weights behind the same model name."""
        import hashlib
        import struct
        vec, _ = self.embed([CANARY_TEXT], timeout=FILL_TIMEOUT)
        quantized = struct.pack(f"<{len(vec[0])}e",
                                *[round(v, 3) for v in vec[0]])
        return hashlib.sha256(quantized).hexdigest()[:32]
