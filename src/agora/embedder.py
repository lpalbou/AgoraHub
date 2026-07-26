"""The embedding pipeline's pure core (agora-0137): chunking and the
standing reconcile. The thread, client and lifecycle wrap these; keeping
the diff computation pure is what makes crash-recovery testable — the
work set is DERIVED state, never a stored queue (ops cycle 2: any stored
queue desyncs from truth exactly when a SIGKILL interrupts it; hashes
stored WITH the vectors are crash-safe by construction).

Chunking (retrieval cycle-2 sweep, measured): docs > CHUNK_THRESHOLD
chars split into CHUNK_SIZE windows with CHUNK_OVERLAP; the winning
config put a 59.5k-char log message's tail query at rank 1 vs 19 for
1800/300. Windows are code-point based (retrieval P3: byte windows can
split a codepoint; Python str slicing is code-point safe by nature).
Every chunk carries the WHOLE-input hash — edits invalidate atomically.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

CHUNK_THRESHOLD = 2000
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200

#: `ready` (ops c3 R2): coverage ≥ this fraction of docs chunk-complete.
#: The SAME constant caps the blue/green flip criterion — a strict 100%
#: reading would re-open a dark window on any straggler doc.
READY_COVERAGE = 0.99
EMBED_BATCH = 32          # texts per endpoint call (fill path)
SWEEP_IDLE_SECONDS = 20.0  # reconcile cadence when nothing to do


def chunk_text(title: str, text: str) -> list[str]:
    """The embed inputs for one doc: title rides EVERY chunk (362
    title-only docs measured; and a chunk without its title loses the
    topical anchor). Returns [single] for small docs."""
    title = title or ""
    text = text or ""
    whole = f"{title}\n{text}" if title else text
    if len(whole) <= CHUNK_THRESHOLD:
        return [whole]
    step = CHUNK_SIZE - CHUNK_OVERLAP
    chunks = []
    for start in range(0, len(text), step):
        window = text[start:start + CHUNK_SIZE]
        if not window:
            break
        chunks.append(f"{title}\n{window}" if title else window)
        if start + CHUNK_SIZE >= len(text):
            break
    return chunks or [whole[:CHUNK_SIZE]]


def reconcile(docs: dict[tuple[str, str, str], dict[str, Any]],
              vec_hashes: dict[tuple[str, str, str], str],
              vec_chunk_counts: dict[tuple[str, str, str], int],
              *, active_model: str,
              models_present: set[str],
              pending_model: str | None = None) -> dict[str, Any]:
    """The standing 4-prong sweep (ops cycle 3: attached to rebuild() it
    never fires after `agora restore`; standing, it heals ANY divergence
    between the corpus and the vector store, whatever caused it).

    Inputs are snapshots: `docs` from search_docs (key -> {text_hash,
    title, text}), `vec_hashes`/`vec_chunk_counts` from the vector store
    for ONE model. Returns the work: to_embed (docs whose vectors are
    missing, stale, or chunk-incomplete), to_delete_docs (orphans), and
    models_to_drop (leftovers outside the active/pending pair).
    """
    to_embed: list[tuple[str, str, str]] = []
    for key, doc in docs.items():
        have_hash = vec_hashes.get(key)
        if have_hash != doc["text_hash"]:
            to_embed.append(key)          # prong 1 (missing) + 3 (stale)
            continue
        expected = len(chunk_text(doc["title"], doc["text"]))
        if vec_chunk_counts.get(key, 0) != expected:
            to_embed.append(key)          # coverage = ALL chunks present
    orphans = [key for key in vec_hashes if key not in docs]   # prong 2
    keep = {active_model} | ({pending_model} if pending_model else set())
    drops = sorted(models_present - keep)                      # prong 4
    return {"to_embed": to_embed, "to_delete_docs": orphans,
            "models_to_drop": drops}


class Embedder:
    """The one writer of vectors.db: a background thread running the
    standing reconcile against the corpus and draining its derived work
    set through the embed client. Never touches the hub db's writer lock
    — corpus snapshots come off the read-only pool (`read_docs`).

    Lifecycle states (served via `status()`, mirrored to hub meta by the
    service): disabled | filling | ready | degraded(<reason>). A frozen
    `filling` is impossible by construction: every loop iteration either
    progresses, sleeps idle (ready), or lands in degraded with a reason
    and a live thread heartbeat (ops c2: the wedge that lies).
    """

    def __init__(self, store, client, read_docs: Callable[[], dict],
                 *, models_meta: Callable[[], tuple[str, str | None]],
                 on_fill_complete: Callable[[str], None] | None = None) -> None:
        self._store = store
        self._client = client
        self._read_docs = read_docs          # () -> {key: {text_hash,title,text}}
        self._models_meta = models_meta      # () -> (active, pending|None)
        self._on_fill_complete = on_fill_complete or (lambda model: None)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._log = logging.getLogger("agora.embedder")
        # Heartbeat + progress, readable without any lock discipline
        # (single-writer fields, torn reads harmless for a status string).
        self.last_beat: float = 0.0
        self.last_error: str = ""
        self.embedded_total: int = 0

    # -- controls -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="agora-embedder")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def nudge(self) -> None:
        """New corpus writes call this (cheap): shortens the idle sleep."""
        self._wake.set()

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- status ----------------------------------------------------------------

    def coverage(self, model: str) -> float:
        docs = self._read_docs()
        if not docs:
            return 1.0
        counts = self._store.chunk_counts(model)
        hashes = self._store.hashes_for_model(model)
        complete = 0
        for key, doc in docs.items():
            if (hashes.get(key) == doc["text_hash"]
                    and counts.get(key, 0) == len(
                        chunk_text(doc["title"], doc["text"]))):
                complete += 1
        return complete / len(docs)

    # -- the loop ----------------------------------------------------------------

    def _run(self) -> None:
        from .embed_client import BreakerOpen
        while not self._stop.is_set():
            self.last_beat = time.time()
            try:
                active, pending = self._models_meta()
                target = pending or active
                if not target:
                    self._sleep_idle()
                    continue
                docs = self._read_docs()
                work = reconcile(
                    docs, self._store.hashes_for_model(target),
                    self._store.chunk_counts(target),
                    active_model=active or target,
                    models_present=self._store.models_present(),
                    pending_model=pending)
                for key in work["to_delete_docs"]:
                    self._store.delete_doc(*key)
                for model in work["models_to_drop"]:
                    n = self._store.delete_model(model)
                    self._log.info("dropped %d rows of retired model %s",
                                   n, model)
                    self._store.vacuum()
                if not work["to_embed"]:
                    if pending:
                        # Fill complete for the pending model: hand the
                        # flip decision back to the service (it owns meta
                        # ordering: flip commits BEFORE old rows drop).
                        self._on_fill_complete(pending)
                    self._sleep_idle()
                    continue
                self._drain(target, docs, work["to_embed"])
            except BreakerOpen:
                self._sleep_idle()
            except Exception as e:      # noqa: BLE001 — the exception policy:
                # degraded-with-reason, never a silent dead thread (ops c2).
                self.last_error = f"{type(e).__name__}: {e}"
                self._log.exception("embedder degraded (will retry)")
                self._sleep_idle()

    def _drain(self, model: str, docs: dict, keys: list) -> None:
        """Embed a bounded slice per iteration (newest first — fresh
        traffic is what queries want; retrieval P3), one batched
        endpoint call + one batched vector commit at a time."""
        from .embed_client import BreakerOpen
        keys = sorted(keys, key=lambda k: docs[k].get("created_at", 0.0),
                      reverse=True)
        batch: list[dict[str, Any]] = []
        texts: list[str] = []
        for key in keys:
            if self._stop.is_set():
                return
            doc = docs[key]
            for ix, chunk in enumerate(chunk_text(doc["title"], doc["text"])):
                texts.append(chunk)
                batch.append({"kind": key[0], "channel": key[1],
                              "ref": key[2], "chunk": ix,
                              "text_hash": doc["text_hash"]})
                if len(texts) >= EMBED_BATCH:
                    if not self._flush(model, batch, texts):
                        return
                    batch, texts = [], []
        if texts:
            self._flush(model, batch, texts)

    def _flush(self, model: str, batch: list[dict], texts: list[str]) -> bool:
        from .embed_client import BreakerOpen
        try:
            vectors, _ = self._client.embed(texts)
        except BreakerOpen:
            return False
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"
            return False
        for row, vec in zip(batch, vectors):
            row["values"] = vec
        self._store.put_batch(model, batch)
        self.embedded_total += len(batch)
        self.last_error = ""
        return True

    def _sleep_idle(self) -> None:
        self._wake.wait(timeout=SWEEP_IDLE_SECONDS)
        self._wake.clear()
