"""Hub search index (agora-0132): the FTS5 shadow corpus and its sync.

Design settled by 3 adversary cycles (untracked/search-spec-v2.md + the
cycle-3 amendments; measured findings in untracked/adversary-search-c2-data.md):

- ONE shadow table `search_docs(kind, channel, ref, title, text)` +
  external-content FTS5 (`porter unicode61 tokenchars '-_'`). The shadow
  table exists because the corpus spans messages/store/agents (no single
  content= mapping), because `messages` has a TEXT PK whose implicit
  rowid VACUUM may renumber, and because retraction purge is only clean
  when the index rows are ours to delete wholesale.
- Sync happens INSIDE the writer's transaction at every choke point
  (db.py calls the helpers here while holding its lock, before commit).
  Retraction/fs-delete DELETE their doc: a content-derived discovery
  surface must never find what position-addressed reads tombstone —
  match-then-redact is forbidden (the match itself is an oracle).
- Author-embedded sentinel bytes (\\u0001/\\u0002) are stripped at ingest
  so a crafted body can never forge highlight boundaries in consumers.
- The corpus is a WHITELIST, default-closed: store prefixes route
  through _STORE_KINDS; unknown prefixes are never indexed (a future
  namespace must opt in). Colleague notes, blobs, fs history versions,
  the ledger, hub rules: never indexed.
- Read side: a small pool of read-only WAL connections (mode=ro +
  query_only ON) so search never serializes against posts. The pool is
  opened lazily AFTER the writer connection exists (R2: mode=ro cannot
  open a WAL db whose sidecars are absent) and closed BEFORE the
  writer's shutdown checkpoint (R3: an open read txn pins WAL frames).
  For :memory: databases (tests) there is no shareable file — the pool
  degrades to the writer connection under the writer lock, which is
  serialized and therefore trivially snapshot-consistent.
"""

from __future__ import annotations

import contextlib
import json
import queue
import sqlite3
import threading
from typing import Any, Iterator

# Ingest caps: bound pathological documents without losing real prose
# (live corpus max body ~59.5KB measured; 64KB keeps everything today).
MAX_DOC_TEXT = 64 * 1024
SNIPPET_SENTINELS = ("\u0001", "\u0002")

# Store prefixes that enter the corpus, and the section kind each maps to.
# channel:* rows are deliberately absent (channel meta is roster furniture,
# and the section enum cannot hold them — cycle-3A ruling).
_STORE_KINDS = {"decision:": "decision", "claim:": "claim", "work:": "work"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS search_docs (
    doc_id     INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,      -- message|decision|claim|work|file|agent
    channel    TEXT,               -- NULL only for kind=agent (roster-scoped)
    ref        TEXT NOT NULL,      -- message id | store key | fs key | agent id
    title      TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_search_docs_key
    ON search_docs (kind, COALESCE(channel,''), ref);
CREATE INDEX IF NOT EXISTS idx_search_docs_channel ON search_docs (channel);
"""

# The FTS virtual table is created separately because executescript on the
# main SCHEMA runs before we know FTS5 is available; the migration path in
# db.py calls ensure_fts() which raises a clear error if the sqlite build
# lacks FTS5 (verified compiled-in on this machine's pythons, cycle-3C).
FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5("
    " title, text,"
    " content='search_docs', content_rowid='doc_id',"
    " tokenize=\"porter unicode61 tokenchars '-_'\")"
)


def _clean(s: str) -> str:
    """Ingest sanitation: strip highlight-sentinel bytes and NULs, cap size."""
    if not s:
        return ""
    for ch in SNIPPET_SENTINELS + ("\x00",):
        if ch in s:
            s = s.replace(ch, "")
    return s[:MAX_DOC_TEXT]


def extract_text(value: Any, *, _depth: int = 0) -> str:
    """Extracted human text from a JSON-ish value: string LEAVES only,
    never key names (F4: raw-JSON indexing made field names like `status`
    matchable and rendered snippets as brace-soup)."""
    if _depth > 6:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(t for v in value.values()
                        if (t := extract_text(v, _depth=_depth + 1)))
    if isinstance(value, (list, tuple)):
        return " ".join(t for v in value
                        if (t := extract_text(v, _depth=_depth + 1)))
    return ""


def message_doc(channel: str, msg_id: str, title: str, body: str,
                data: dict[str, Any] | None) -> tuple[str, str]:
    """(title, text) for a message doc. Ask texts are appended: 99.1% of
    asks are NOT in bodies (measured), so title+body alone is blind to
    exactly the sentences that create obligations."""
    parts = [body or ""]
    if isinstance(data, dict):
        asks = data.get("asks")
        if isinstance(asks, list):
            for a in asks:
                if isinstance(a, dict) and isinstance(a.get("text"), str):
                    parts.append(a["text"])
    return _clean(title or ""), _clean("\n".join(p for p in parts if p))


def store_kind(key: str) -> str | None:
    """The section kind for a store key, or None if the key is not in the
    whitelisted corpus (default-closed; fs/ rides fs_put, never store_set)."""
    for prefix, kind in _STORE_KINDS.items():
        if key.startswith(prefix):
            return kind
    return None


def store_doc(key: str, value: Any) -> tuple[str, str]:
    return _clean(key), _clean(extract_text(value))


def fs_doc(key: str, value: Any) -> tuple[str, str]:
    # key is "fs/<path>"; title is the path humans know.
    return _clean(key[3:] if key.startswith("fs/") else key), _clean(extract_text(value))


def agent_doc(agent_id: str, name: str, about: str) -> tuple[str, str]:
    return _clean(name or agent_id), _clean(about or "")


# -- sync primitives (call while HOLDING the writer lock, pre-commit) ----------

def put_doc(conn: sqlite3.Connection, kind: str, channel: str | None,
            ref: str, title: str, text: str, created_at: float) -> None:
    """Upsert one doc + its FTS row. External-content FTS5 requires feeding
    the OLD values on delete, so upsert = delete-old + insert-new."""
    del_doc(conn, kind, channel, ref)
    cur = conn.execute(
        "INSERT INTO search_docs (kind, channel, ref, title, text, created_at)"
        " VALUES (?,?,?,?,?,?)", (kind, channel, ref, title, text, created_at))
    conn.execute(
        "INSERT INTO search_fts(rowid, title, text) VALUES (?,?,?)",
        (cur.lastrowid, title, text))


def del_doc(conn: sqlite3.Connection, kind: str, channel: str | None,
            ref: str) -> None:
    row = conn.execute(
        "SELECT doc_id, title, text FROM search_docs"
        " WHERE kind = ? AND COALESCE(channel,'') = ? AND ref = ?",
        (kind, channel or "", ref)).fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT INTO search_fts(search_fts, rowid, title, text)"
        " VALUES ('delete', ?, ?, ?)", (row[0], row[1], row[2]))
    conn.execute("DELETE FROM search_docs WHERE doc_id = ?", (row[0],))


def ensure_fts(conn: sqlite3.Connection) -> None:
    """Create the FTS table; a build without FTS5 fails HERE with a clear
    message instead of mysteriously at first search."""
    try:
        conn.execute(FTS_DDL)
    except sqlite3.OperationalError as e:  # pragma: no cover - build-dependent
        raise RuntimeError(
            "this Python's sqlite3 lacks the FTS5 extension, which agora's "
            "hub search requires (it ships compiled-in with CPython's "
            "standard builds)") from e


def rebuild(conn: sqlite3.Connection) -> dict[str, int]:
    """Deterministic full rebuild from the source tables, DML-only (never
    DROP/CREATE: WAL readers keep their snapshot; the next BEGIN sees the
    rebuilt index). Runs inside ONE caller-managed transaction. Ends with
    an FTS 'rebuild' (regenerates from the content table) + 'optimize'
    (external-content deletes GROW the index — measured +786KB per 2k
    deletes; optimize reclaimed 3.1MB)."""
    conn.execute("DELETE FROM search_docs")
    counts = {"message": 0, "decision": 0, "claim": 0, "work": 0,
              "file": 0, "agent": 0}

    for r in conn.execute(
            "SELECT id, channel, title, body, data, created_at FROM messages"
            " WHERE retracted_at IS NULL"):
        data = json.loads(r["data"]) if r["data"] else None
        title, text = message_doc(r["channel"], r["id"], r["title"], r["body"], data)
        conn.execute(
            "INSERT INTO search_docs (kind, channel, ref, title, text, created_at)"
            " VALUES ('message',?,?,?,?,?)",
            (r["channel"], r["id"], title, text, r["created_at"]))
        counts["message"] += 1

    for r in conn.execute("SELECT channel, key, value, updated_at FROM store"):
        key = r["key"]
        if key.startswith("fs/"):
            value = json.loads(r["value"])
            if isinstance(value, dict) and value.get("deleted"):
                continue  # tombstoned head: out of the corpus
            title, text = fs_doc(key, value)
            conn.execute(
                "INSERT INTO search_docs (kind, channel, ref, title, text, created_at)"
                " VALUES ('file',?,?,?,?,?)",
                (r["channel"], key, title, text, r["updated_at"]))
            counts["file"] += 1
            continue
        kind = store_kind(key)
        if kind is None:
            continue
        title, text = store_doc(key, json.loads(r["value"]))
        conn.execute(
            "INSERT INTO search_docs (kind, channel, ref, title, text, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (kind, r["channel"], key, title, text, r["updated_at"]))
        counts[kind] += 1

    for r in conn.execute(
            "SELECT id, name, about, created_at FROM agents"
            " WHERE retired_at IS NULL AND deleted_at IS NULL AND about != ''"):
        title, text = agent_doc(r["id"], r["name"], r["about"])
        conn.execute(
            "INSERT INTO search_docs (kind, channel, ref, title, text, created_at)"
            " VALUES ('agent',NULL,?,?,?,?)",
            (r["id"], title, text, r["created_at"]))
        counts["agent"] += 1

    conn.execute("INSERT INTO search_fts(search_fts) VALUES ('rebuild')")
    conn.execute("INSERT INTO search_fts(search_fts) VALUES ('optimize')")
    return counts


def drift_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Cheap sync-health probe for `agora status`: doc counts per kind vs
    the source-of-truth counts they should mirror (9.2ms measured)."""
    docs = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) AS n FROM search_docs GROUP BY kind")}
    live_msgs = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE retracted_at IS NULL").fetchone()["n"]
    return {"docs": docs, "expected_messages": live_msgs,
            "message_drift": live_msgs - docs.get("message", 0)}


# -- the read-only pool --------------------------------------------------------

class ReadPool:
    """2-4 read-only WAL connections for ms-class reads (search is the
    hub's first). Lazy open (R2), bounded checkout (doubles as the
    concurrency cap), closed before the writer's shutdown checkpoint (R3).
    Yields raw connections; the EXECUTOR owns BEGIN DEFERRED/COMMIT —
    one explicit transaction per report (R1: bare SELECTs in Python
    autocommit see different snapshots mid-report, measured)."""

    def __init__(self, path: str, size: int = 3) -> None:
        self._path = path
        self._size = size
        self._q: queue.Queue[sqlite3.Connection] = queue.Queue()
        self._opened = False
        self._lock = threading.Lock()

    def _open_all(self) -> None:
        for _ in range(self._size):
            conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True,
                                   check_same_thread=False, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._q.put(conn)
        self._opened = True

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if not self._opened:
                self._open_all()
        conn = self._q.get()
        try:
            yield conn
        finally:
            # Never leave a transaction open across checkouts (R3).
            with contextlib.suppress(sqlite3.Error):
                if conn.in_transaction:
                    conn.rollback()
            self._q.put(conn)

    def close(self) -> None:
        if not self._opened:
            return
        with contextlib.suppress(queue.Empty):
            while True:
                self._q.get_nowait().close()
        self._opened = False
