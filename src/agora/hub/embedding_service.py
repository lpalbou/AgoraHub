"""Embedding lifecycle manager (agora-0137 step 7): the one place that
owns the model-change state machine, the flip ordering, and the honest
status word.

State (all DERIVED or in durable meta — nothing hand-kept):
- hub meta `embedding_model`   : the ACTIVE model (serves queries).
- hub meta `embedding_pending` : a fill-in-progress model (blue/green).
- vectors.db vmeta `canary:<model>` : the frozen probe's fingerprint.
- config.json `embedding`      : {url, model, api_key} — the operator's
  DECLARATION. Boot precedence (ops c2 P1): meta WINS; a config.json
  model differing from meta is reported as a SEED MISMATCH in status,
  never auto-applied — boot must not impersonate a model change.

Model-change semantics: blue/green by default (adversary recommendation
C; operator ruling A/B/C pending — the machinery here IS C, and A/B are
restrictions of it, so his ruling picks behavior later without surgery):
accept → pending=new, embedder fills alongside → fill complete → canary
check → META FLIP COMMITS FIRST → old rows drop + own-file VACUUM.
`ready` = coverage ≥ READY_COVERAGE (99%) — the same constant that caps
the flip, so no post-flip dark window (ops c3 R2).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from ..embedder import READY_COVERAGE, Embedder

log = logging.getLogger("agora.embedding")


def _hub_error(status: int, detail: str):
    """Late import: HubError lives in service.py, which imports US —
    the function seam breaks the cycle without moving the class."""
    from .service import HubError
    return HubError(status, detail)


class EmbeddingManager:
    def __init__(self, db, *, db_path: str,
                 url: str = "", model: str = "", api_key: str = "") -> None:
        self.db = db
        self._seed = {"url": url, "model": model, "api_key": api_key}
        self._store = None
        self._client = None
        self._embedder: Embedder | None = None
        self._flip_lock = threading.Lock()
        # vectors.db lives BESIDE the hub db (its own file by design).
        base = db_path if db_path not in ("", ":memory:") else ""
        self._vec_path = (base + ".vectors") if base else ":memory:"
        self._numpy_missing = False
        self._boot()

    # -- boot -----------------------------------------------------------------

    def _boot(self) -> None:
        active = self.db.meta_get("embedding_model") or ""
        url = self.db.meta_get("embedding_url") or self._seed["url"]
        if not active and self._seed["model"] and self._seed["url"]:
            # First enable via config seed: adopt it as meta (there is
            # nothing to erase — adopting is not a model change).
            have = self.db.meta_get("embedding_model")
            if not have:
                self.db.meta_set("embedding_model", self._seed["model"])
                self.db.meta_set("embedding_url", self._seed["url"])
                active, url = self._seed["model"], self._seed["url"]
        if not active or not url:
            return                      # disabled: nothing configured
        try:
            from ..vector_store import VectorStore
            store = VectorStore(self._vec_path)
            if not store.numpy_available():
                self._numpy_missing = True
                log.warning("embedding configured but numpy is missing —"
                            " semantic search degraded; install"
                            " 'agorahub[mcp,semantic]'")
                return
        except Exception:
            log.exception("vector store unavailable")
            return
        from ..embed_client import EmbedClient
        self._store = store
        self._client = EmbedClient(url, active,
                                   api_key=self._seed["api_key"])
        self._embedder = Embedder(
            store, self._client,
            read_docs=self.db.search_docs_snapshot,
            models_meta=self._models_meta,
            on_fill_complete=self._maybe_flip)
        self._embedder.start()

    def _models_meta(self) -> tuple[str, str | None]:
        return (self.db.meta_get("embedding_model") or "",
                self.db.meta_get("embedding_pending") or None)

    # -- the flip (blue/green) ---------------------------------------------------

    def _maybe_flip(self, pending: str) -> None:
        """Fill complete for `pending`. Ordering is the contract (ops c2):
        canary check → META FLIP COMMITS → old rows drop → VACUUM. A crash
        between flip and drop leaves harmless leftovers prong 4 removes."""
        with self._flip_lock:
            active, still_pending = self._models_meta()
            if still_pending != pending or self._embedder is None:
                return
            cov = self._embedder.coverage(pending)
            floor = min(READY_COVERAGE, 1.0)
            if cov < floor:
                return                   # stragglers: keep filling
            try:
                fp = self._client.canary_fingerprint()
            except Exception:
                return                   # endpoint resting: flip waits
            known = self._store.meta_get(f"canary:{pending}")
            if known is None:
                self._store.meta_set(f"canary:{pending}", fp)
            elif known != fp:
                self.db.meta_set("embedding_error",
                                 f"canary mismatch for {pending} — same name,"
                                 " different weights; refusing to flip")
                return
            old = active
            self.db.meta_set("embedding_model", pending)
            self.db.meta_set("embedding_pending", "")
            self._client.model = pending
            log.info("embedding model flipped %s -> %s (coverage %.1f%%)",
                     old or "(none)", pending, cov * 100)
            if old and old != pending:
                self._store.delete_model(old)
                self._store.vacuum()

    # -- operator surface ---------------------------------------------------------

    def set_model(self, url: str, model: str, api_key: str = "",
                  accept_recompute: bool = False) -> dict[str, Any]:
        """The R3 gate. Same model+url = idempotent probe (UX c2). A real
        change with vectors present refuses without acceptance, stating
        the cost. Acceptance = blue/green pending fill, never a dark
        erase (pending the operator's A/B/C ruling; C machinery).
        Order matters: PROBE before ADOPT — a dead endpoint must never
        become the hub's durable choice."""
        if self._numpy_missing:
            raise _hub_error(409, "semantic search needs numpy — install"
                                  " with: uv tool install --force"
                                  " 'agorahub[mcp,semantic]' and restart"
                                  " the hub")
        active = self.db.meta_get("embedding_model") or ""
        cur_url = self.db.meta_get("embedding_url") or ""
        if model == active and url == cur_url and self._store is not None:
            probe = self._probe(url, model, api_key)
            return {"changed": False, "probe": probe,
                    "model": active, "state": self.state()}
        have = self._store.counts(active)["rows"] if (
            self._store is not None and active) else 0
        if have and not accept_recompute:
            docs = self._store.counts(active)["docs"]
            raise _hub_error(409,
                           f"changing the embedding model erases {docs} docs'"
                           f" vectors ({have} rows) and recomputes them"
                           " (~25 min on today's corpus). Pass"
                           " accept_recompute=true (CLI:"
                           " --accept-recompute) to proceed; the old model"
                           " keeps serving until the new fill completes.")
        probe = self._probe(url, model, api_key)
        if not probe["ok"]:
            raise _hub_error(502, f"embedding endpoint refused the probe:"
                                f" {probe['error']} — check `agora embedding"
                                " status`; /v1/models on the endpoint lists"
                                " what it serves")
        self.db.meta_set("embedding_url", url)
        self.db.meta_set("embedding_error", "")
        pending_fill = bool(active) and model != active
        if pending_fill:
            self.db.meta_set("embedding_pending", model)
        else:
            self.db.meta_set("embedding_model", model)   # first enable
        self._seed = {"url": url, "model": model, "api_key": api_key}
        if self._store is None:
            self._boot()                                  # build the infra
            if self._store is None:
                raise _hub_error(503, "vector store unavailable — see hub log")
        if self._client is not None:
            self._client.url = url.rstrip("/")
            if not pending_fill:
                self._client.model = model
        if self._embedder is not None:
            self._embedder.nudge()
        return {"changed": True, "model": model,
                "pending": pending_fill, "probe": probe,
                "docs_to_embed": len(self.db.search_docs_snapshot()),
                "state": self.state()}

    def _probe(self, url: str, model: str, api_key: str) -> dict[str, Any]:
        from ..embed_client import EmbedClient
        try:
            probe_client = EmbedClient(url, model, api_key=api_key)
            vec, _ = probe_client.embed(["probe"], timeout=10.0)
            return {"ok": True, "dim": len(vec[0])}
        except Exception as e:  # noqa: BLE001 — reported, never raised here
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def disable(self, *, erase: bool = False) -> dict[str, Any]:
        """The off switch (ops c3 R4): stop the thread, clear meta;
        --erase additionally drops all vectors (kept otherwise: re-enable
        resumes from what exists)."""
        if self._embedder is not None:
            self._embedder.stop()
            self._embedder = None
        erased = 0
        if erase and self._store is not None:
            for model in self._store.models_present():
                erased += self._store.delete_model(model)
            self._store.vacuum()
        self.db.meta_set("embedding_model", "")
        self.db.meta_set("embedding_pending", "")
        return {"disabled": True, "erased_rows": erased}

    # -- status ---------------------------------------------------------------------

    def state(self) -> str:
        """disabled | filling | ready | degraded(<reason>) — one word,
        derived, never hand-kept."""
        if self._numpy_missing:
            return "degraded(numpy-missing)"
        active, pending = self._models_meta()
        if not active:
            return "disabled"
        if self._store is None or self._embedder is None:
            return "degraded(store-unavailable)"
        if not self._embedder.alive():
            return "degraded(embedder-thread-dead)"
        err = self.db.meta_get("embedding_error") or ""
        if err:
            return f"degraded({err[:60]})"
        breaker = self._client.breaker_state()
        if breaker["state"] == "open":
            return f"degraded(endpoint-resting-{breaker['retry_in']:.0f}s)"
        cov = self._embedder.coverage(active)
        if cov >= READY_COVERAGE:
            return "ready"
        return "filling"

    def status(self) -> dict[str, Any]:
        active, pending = self._models_meta()
        out: dict[str, Any] = {
            "state": self.state(), "model": active or None,
            "pending_model": pending,
            "url": self.db.meta_get("embedding_url") or None,
            "computed_at": time.time(),
        }
        seed_model = self._seed.get("model")
        if seed_model and active and seed_model != active:
            out["seed_mismatch"] = (
                f"config.json says '{seed_model}' but the hub's durable"
                f" choice is '{active}' — meta wins at boot; use `agora"
                " embedding set` to actually change the model")
        if self._embedder is not None and self._store is not None:
            out["coverage"] = round(self._embedder.coverage(active), 4)
            if pending:
                out["pending_coverage"] = round(
                    self._embedder.coverage(pending), 4)
            out["rows"] = self._store.counts(active)["rows"]
            out["embedded_total_session"] = self._embedder.embedded_total
            out["thread_alive"] = self._embedder.alive()
            out["last_beat_age_s"] = (round(
                time.time() - self._embedder.last_beat, 1)
                if self._embedder.last_beat else None)
            out["last_error"] = self._embedder.last_error or None
            out["breaker"] = self._client.breaker_state()
            out["vectors_db"] = self._vec_path
        return out

    # -- query-path access --------------------------------------------------------

    def query_snapshot(self) -> dict[str, Any] | None:
        """(matrix snapshot, client) for the executor — None whenever
        semantic cannot honestly serve (the caller degrades to lexical)."""
        if self.state() != "ready" or self._store is None:
            return None
        active, _ = self._models_meta()
        try:
            probe = self._store.matrix(active, self._expected_dim())
        except ImportError:
            return None
        return probe

    def _expected_dim(self) -> int:
        row = self._store.meta_get("dim")
        if row:
            return int(row)
        # Derive once from any stored vector; remember it.
        with self._store._lock:
            r = self._store._conn.execute(
                "SELECT dim FROM vectors LIMIT 1").fetchone()
        dim = int(r["dim"]) if r else 0
        if dim:
            self._store.meta_set("dim", str(dim))
        return dim or 1

    def embed_query(self, query: str) -> list[float] | None:
        if self._client is None:
            return None
        from ..embed_client import BreakerOpen
        try:
            return self._client.embed_query(query)
        except (BreakerOpen, Exception):   # noqa: BLE001 — degrade honestly
            return None

    def nudge(self) -> None:
        if self._embedder is not None:
            self._embedder.nudge()

    def shutdown(self) -> None:
        if self._embedder is not None:
            self._embedder.stop()
        if self._store is not None:
            self._store.close()
