"""The semantic layer's vector store (agora-0137) — a STANDALONE sqlite
file beside the hub db, owned by the embedder thread.

Why its own file (ops cycle 2/3, all measured):
- Erasing a model's vectors leaves a ~46MB freelist hole; the 1.3s VACUUM
  that reclaims it must never stall the hub db's writer (the 0136 lock
  convoy is live history). Here the embedder may VACUUM its OWN file.
- ATTACH was rejected: sqlite gives no WAL cross-file atomicity, so a
  crash between files would need reasoning we cannot pin. Instead the
  SERVING JOIN provides purge safety: a vector row only ever serves when
  a search_docs row with the SAME (kind, channel, ref) AND the SAME
  text_hash exists in the hub db at query time — a vector whose doc is
  gone or EDITED can never rank (the match-then-redact oracle stays
  closed even though the files are independent).
- vectors.db is DISPOSABLE by design: `agora backup` never snapshots it;
  after `agora restore` the standing reconcile re-derives every vector
  from the restored corpus. Losing this file costs one backfill.

Storage contract (retrieval cycle-2/3 riders, binding):
- Key (kind, channel, ref, chunk, model): the bare (ref, model) key
  collides TODAY (same fs path in two channels) — cross-channel vector
  contamination, found before it shipped.
- chunk INT (0 = whole doc); every chunk row carries the WHOLE-input
  text_hash so an edit invalidates a doc's vector set atomically —
  per-chunk hashes would let one query rank old and new prose of the
  same doc side by side.
- Vectors pack little-endian float32 EXPLICITLY (a matrix rebuilt on a
  different-endian host must read the same numbers), dim is checked on
  every read, NaN/inf are clamped at write time (one poisoned vector
  silently corrupts every cosine it touches).

numpy rides the [semantic] extra: only the HUB process needs it (seats
never compute cosines). Import is lazy; callers translate ImportError
into the honest `degraded(numpy-missing)` state, never a crash.
"""

from __future__ import annotations

import math
import sqlite3
import struct
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    kind       TEXT NOT NULL,
    channel    TEXT NOT NULL DEFAULT '',   -- '' for kind=agent (roster-scoped)
    ref        TEXT NOT NULL,
    chunk      INTEGER NOT NULL DEFAULT 0, -- 0 = whole doc
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL,              -- little-endian float32 * dim
    text_hash  TEXT NOT NULL,              -- WHOLE-input hash (all chunks alike)
    updated_at REAL NOT NULL,
    PRIMARY KEY (kind, channel, ref, chunk, model)
);
CREATE INDEX IF NOT EXISTS idx_vectors_model ON vectors (model);
CREATE TABLE IF NOT EXISTS vmeta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def pack_vector(values: list[float]) -> bytes:
    """Explicit little-endian float32, NaN/inf clamped to 0.0 at write —
    a poisoned component corrupts every cosine it touches (retrieval P3)."""
    cleaned = [v if math.isfinite(v) else 0.0 for v in values]
    return struct.pack(f"<{len(cleaned)}f", *cleaned)


class VectorStore:
    """All access single-threaded through the embedder thread plus
    read-only matrix snapshots for the query path. The matrix cache is
    the load-bearing piece: reading ~70MB of blobs per query would dwarf
    the 1.3ms cosine, so the full (refs, hashes, matrix) for the active
    model lives in RAM and is invalidated by any write."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()
        self._cache: dict[str, Any] | None = None   # one model's matrix
        self._np = None

    # -- numpy seam ---------------------------------------------------------

    def _numpy(self):
        """Lazy import; ImportError is the caller's honest-degrade signal."""
        if self._np is None:
            import numpy  # noqa: PLC0415 — the [semantic] extra's only import site
            self._np = numpy
        return self._np

    @staticmethod
    def numpy_available() -> bool:
        try:
            import numpy  # noqa: F401
            return True
        except ImportError:
            return False

    # -- meta ----------------------------------------------------------------

    def meta_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM vmeta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO vmeta (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
            self._conn.commit()

    # -- writes (embedder thread) ---------------------------------------------

    def put_batch(self, model: str, rows: list[dict[str, Any]]) -> None:
        """One batched commit (~35ms worst measured on the hub db's class of
        hardware). Row shape: kind, channel, ref, chunk, text_hash,
        values (list[float])."""
        if not rows:
            return
        with self._lock:
            for r in rows:
                vec = pack_vector(r["values"])
                self._conn.execute(
                    "INSERT INTO vectors (kind, channel, ref, chunk, model,"
                    " dim, vec, text_hash, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(kind, channel, ref, chunk, model)"
                    " DO UPDATE SET dim=excluded.dim, vec=excluded.vec,"
                    " text_hash=excluded.text_hash, updated_at=excluded.updated_at",
                    (r["kind"], r.get("channel") or "", r["ref"],
                     int(r.get("chunk", 0)), model, len(r["values"]), vec,
                     r["text_hash"], time.time()))
            self._conn.commit()
            self._cache = None

    def delete_doc(self, kind: str, channel: str | None, ref: str) -> None:
        """All chunks, all models — the doc is gone from the corpus."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM vectors WHERE kind=? AND channel=? AND ref=?",
                (kind, channel or "", ref))
            self._conn.commit()
            self._cache = None

    def delete_model(self, model: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM vectors WHERE model = ?", (model,))
            self._conn.commit()
            self._cache = None
            return cur.rowcount

    def vacuum(self) -> None:
        """Reclaim after a model drop — OUR file, never the hub db's
        writer (the whole reason this file exists apart)."""
        with self._lock:
            self._conn.execute("VACUUM")

    # -- reads ----------------------------------------------------------------

    def counts(self, model: str) -> dict[str, int]:
        with self._lock:
            docs = self._conn.execute(
                "SELECT COUNT(DISTINCT kind || ':' || channel || ':' || ref)"
                " AS n FROM vectors WHERE model = ?", (model,)).fetchone()["n"]
            rows = self._conn.execute(
                "SELECT COUNT(*) AS n FROM vectors WHERE model = ?",
                (model,)).fetchone()["n"]
        return {"docs": docs, "rows": rows}

    def hashes_for_model(self, model: str) -> dict[tuple[str, str, str], str]:
        """(kind, channel, ref) -> text_hash for the WHOLE doc set of one
        model — the reconcile's diff input. A doc with N chunks appears
        once (all chunks share the whole-input hash by contract; rows
        violating that are treated as stale)."""
        out: dict[tuple[str, str, str], str] = {}
        stale: set[tuple[str, str, str]] = set()
        with self._lock:
            for r in self._conn.execute(
                    "SELECT kind, channel, ref, text_hash FROM vectors"
                    " WHERE model = ?", (model,)):
                key = (r["kind"], r["channel"], r["ref"])
                if key in out and out[key] != r["text_hash"]:
                    stale.add(key)   # mixed hashes = mid-edit crash artifact
                out[key] = r["text_hash"]
        for key in stale:
            out[key] = ""            # forces re-embed via hash mismatch
        return out

    def chunk_counts(self, model: str) -> dict[tuple[str, str, str], int]:
        out: dict[tuple[str, str, str], int] = {}
        with self._lock:
            for r in self._conn.execute(
                    "SELECT kind, channel, ref, COUNT(*) AS n FROM vectors"
                    " WHERE model = ? GROUP BY kind, channel, ref", (model,)):
                out[(r["kind"], r["channel"], r["ref"])] = r["n"]
        return out

    def matrix(self, model: str, expect_dim: int) -> dict[str, Any]:
        """The query path's snapshot: refs + hashes + a normalized numpy
        matrix for ONE model (the one-model-per-query invariant lives here:
        the WHERE clause admits nothing else). Cached until any write.
        Rows with a wrong dim are SKIPPED loudly in the count, never
        silently mis-shaped (retrieval P3)."""
        np = self._numpy()
        with self._lock:
            cache = self._cache
            if cache is not None and cache["model"] == model:
                return cache
            keys: list[tuple[str, str, str, int]] = []
            hashes: list[str] = []
            bufs: list[bytes] = []
            skipped = 0
            for r in self._conn.execute(
                    "SELECT kind, channel, ref, chunk, dim, vec, text_hash"
                    " FROM vectors WHERE model = ?", (model,)):
                if r["dim"] != expect_dim or len(r["vec"]) != 4 * expect_dim:
                    skipped += 1
                    continue
                keys.append((r["kind"], r["channel"], r["ref"], r["chunk"]))
                hashes.append(r["text_hash"])
                bufs.append(r["vec"])
            if bufs:
                mat = np.frombuffer(b"".join(bufs), dtype="<f4").reshape(
                    len(bufs), expect_dim).astype(np.float32)
                norms = np.linalg.norm(mat, axis=1, keepdims=True)
                norms[norms == 0.0] = 1.0
                mat = mat / norms
            else:
                mat = np.zeros((0, expect_dim), dtype=np.float32)
            cache = {"model": model, "keys": keys, "hashes": hashes,
                     "mat": mat, "skipped_dim": skipped}
            self._cache = cache
            return cache

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()
