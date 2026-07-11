"""SQLite persistence layer.

Single-writer, lock-guarded synchronous access. For the local-first
deployments this hub targets (a handful of agents on one machine or LAN),
SQLite operations are microseconds; serializing them behind one lock is
simpler and safer than async drivers. The wire protocol is backend-agnostic,
so this module can be swapped for NATS JetStream or a Rust hub later without
touching clients (see docs/KnowledgeBase.md).

Security note: api keys and invite tokens are stored hashed (sha256) — the
database file never contains usable bearer secrets.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from typing import Any

from .ids import new_ulid
from .models import AgentInfo, Channel, Member, Message, StoreEntry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    about       TEXT NOT NULL DEFAULT '',
    operator    INTEGER NOT NULL DEFAULT 0,
    key_hash    TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    name        TEXT PRIMARY KEY,
    private     INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    channel     TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'member',
    joined_at   REAL NOT NULL,
    PRIMARY KEY (channel, agent_id)
);
CREATE TABLE IF NOT EXISTS invites (
    token_hash  TEXT PRIMARY KEY,
    channel     TEXT NOT NULL,
    agent_id    TEXT,               -- NULL = any agent may redeem
    created_by  TEXT NOT NULL,
    expires_at  REAL NOT NULL,
    used_by     TEXT,
    used_at     REAL
);
-- Hub-membership join tokens (the invites discipline one level up): scoped,
-- expiring, revocable registration credentials so the admin key never has to
-- leave the hub machine. The public token_id enables list/revoke without ever
-- handling the secret; only sha256(secret) is stored.
CREATE TABLE IF NOT EXISTS join_tokens (
    token_id    TEXT PRIMARY KEY,
    secret_hash TEXT NOT NULL,
    agent_id    TEXT,               -- NULL = redeemer chooses the id
    about       TEXT NOT NULL DEFAULT '',
    channels    TEXT NOT NULL DEFAULT '[]',   -- JSON list: public auto-joins
    created_by  TEXT NOT NULL DEFAULT 'admin',
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    max_uses    INTEGER NOT NULL DEFAULT 1,
    uses        INTEGER NOT NULL DEFAULT 0,
    revoked_at  REAL,
    used_by     TEXT NOT NULL DEFAULT '[]'    -- JSON list: the audit trail
);
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    channel     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    sender      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    urgency     TEXT NOT NULL,
    critical    INTEGER NOT NULL DEFAULT 0,
    downgraded  INTEGER NOT NULL DEFAULT 0,
    to_agents   TEXT NOT NULL DEFAULT '[]',
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    data        TEXT,
    reply_to    TEXT,
    created_at  REAL NOT NULL,
    hash        TEXT,               -- per-channel hash chain (ledger/verbatim)
    UNIQUE (channel, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_channel_seq ON messages (channel, seq);
CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages (reply_to);
CREATE TABLE IF NOT EXISTS reads (
    message_id  TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    read_at     REAL NOT NULL,
    PRIMARY KEY (message_id, agent_id)
);
CREATE TABLE IF NOT EXISTS notes (
    observer    TEXT NOT NULL,
    subject     TEXT NOT NULL,
    note        TEXT NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (observer, subject)
);
CREATE TABLE IF NOT EXISTS store (
    channel     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    version     INTEGER NOT NULL,
    updated_by  TEXT NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (channel, key)
);
CREATE TABLE IF NOT EXISTS cursors (
    agent_id    TEXT NOT NULL,
    channel     TEXT NOT NULL,
    last_seq    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_id, channel)
);
-- Append-only archive of every fs version's CONTENT with provenance: the
-- store row holds only the head, so without this a v6 write destroys what
-- v1..v5 said. Deletes archive a NULL value (the tombstone itself has
-- provenance). Rows are never updated or removed.
CREATE TABLE IF NOT EXISTS fs_versions (
    channel     TEXT NOT NULL,
    key         TEXT NOT NULL,
    version     INTEGER NOT NULL,
    value       TEXT,               -- NULL = this version is a delete
    updated_by  TEXT NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (channel, key, version)
);
"""


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class StoreConflict(Exception):
    """Compare-and-swap failed: the entry changed since it was read."""

    def __init__(self, current_version: int) -> None:
        super().__init__(f"store version conflict (current={current_version})")
        self.current_version = current_version


class JoinTokenRefused(Exception):
    """Join-token redemption refused. Mirrors HubError's (status, detail)
    shape without importing the service layer: 403 for token-side problems
    (each with a DISTINCT detail so the joiner knows what to ask the operator
    for), 400 for a missing agent id, 409 for an id collision — which, by
    contract, has NOT consumed the token."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class Database:
    """All persistent state of a hub. Thread-safe via a single lock."""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # Migration: add the ledger hash column to a pre-existing messages
            # table (older DBs). New rows are chained from here; legacy rows keep
            # NULL hash and the chain simply starts at the first hashed message.
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(messages)")}
            if "hash" not in cols:
                self._conn.execute("ALTER TABLE messages ADD COLUMN hash TEXT")
            self._conn.commit()

    # -- agents ------------------------------------------------------------

    def _insert_agent_locked(self, agent_id: str, name: str, api_key: str,
                             operator: bool, about: str) -> AgentInfo:
        """The one agents INSERT, shared by plain registration and join-token
        redemption (which must run it inside ITS transaction). Caller holds
        self._lock and commits."""
        self._conn.execute(
            "INSERT INTO agents (id, name, about, operator, key_hash, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (agent_id, name, about, int(operator), hash_secret(api_key), time.time()),
        )
        return AgentInfo(id=agent_id, name=name, about=about, operator=operator)

    def register_agent(self, agent_id: str, name: str, api_key: str,
                       operator: bool = False, about: str = "") -> AgentInfo:
        with self._lock:
            info = self._insert_agent_locked(agent_id, name, api_key, operator, about)
            self._conn.commit()
        return info

    def agent_by_key(self, api_key: str) -> AgentInfo | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agents WHERE key_hash = ?", (hash_secret(api_key),)
            ).fetchone()
        if row is None:
            return None
        return AgentInfo(id=row["id"], name=row["name"], about=row["about"],
                         operator=bool(row["operator"]), created_at=row["created_at"])

    def set_about(self, agent_id: str, about: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE agents SET about = ? WHERE id = ?", (about, agent_id))
            self._conn.commit()

    def get_about(self, agent_id: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT about FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return row["about"] if row else ""

    def agent_exists(self, agent_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return row is not None

    def list_agent_ids(self) -> list[str]:
        """All registered agent ids (operator-scope surface only)."""
        with self._lock:
            rows = self._conn.execute("SELECT id FROM agents ORDER BY id").fetchall()
        return [r["id"] for r in rows]

    # -- channels + membership ----------------------------------------------

    def create_channel(self, name: str, private: bool, created_by: str,
                       add_owner: bool = True) -> Channel:
        """`add_owner=False` creates an ownerless channel (used for DMs: with no
        owner, invite minting and meta writes fail structurally)."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO channels (name, private, created_by, created_at) VALUES (?,?,?,?)",
                (name, int(private), created_by, now),
            )
            if add_owner:
                self._conn.execute(
                    "INSERT INTO members (channel, agent_id, role, joined_at) VALUES (?,?,?,?)",
                    (name, created_by, "owner", now),
                )
            self._conn.commit()
        return Channel(name=name, private=private, created_by=created_by, created_at=now)

    def ensure_channel(self, name: str, private: bool, created_by: str,
                       add_owner: bool = True) -> tuple[Channel, bool]:
        """Idempotent get-or-create (used for DMs). Returns (channel, created).

        Uses INSERT OR IGNORE so two agents opening the same direct channel
        concurrently cannot race into an IntegrityError/500 — the loser simply
        observes `created=False` and reuses the existing channel. Regular
        channel creation keeps the strict `create_channel` path (duplicate =
        error) so name collisions there still surface to the caller.
        """
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO channels (name, private, created_by, created_at)"
                " VALUES (?,?,?,?)",
                (name, int(private), created_by, now),
            )
            created = cur.rowcount > 0
            if created and add_owner:
                self._conn.execute(
                    "INSERT OR IGNORE INTO members (channel, agent_id, role, joined_at)"
                    " VALUES (?,?,?,?)",
                    (name, created_by, "owner", now),
                )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM channels WHERE name = ?", (name,)).fetchone()
        return Channel(
            name=row["name"], private=bool(row["private"]),
            created_by=row["created_by"], created_at=row["created_at"],
        ), created

    def get_channel(self, name: str) -> Channel | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM channels WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        return Channel(
            name=row["name"], private=bool(row["private"]),
            created_by=row["created_by"], created_at=row["created_at"],
        )

    def add_member(self, channel: str, agent_id: str, role: str = "member") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO members (channel, agent_id, role, joined_at) VALUES (?,?,?,?)",
                (channel, agent_id, role, time.time()),
            )
            self._conn.commit()

    def remove_member(self, channel: str, agent_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM members WHERE channel = ? AND agent_id = ?", (channel, agent_id)
            )
            self._conn.commit()

    def is_member(self, channel: str, agent_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM members WHERE channel = ? AND agent_id = ?", (channel, agent_id)
            ).fetchone()
        return row is not None

    def member_role(self, channel: str, agent_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT role FROM members WHERE channel = ? AND agent_id = ?", (channel, agent_id)
            ).fetchone()
        return row["role"] if row else None

    def list_members(self, channel: str) -> list[Member]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT m.*, COALESCE(a.about, '') AS about
                FROM members m LEFT JOIN agents a ON a.id = m.agent_id
                WHERE m.channel = ?
                """,
                (channel,),
            ).fetchall()
        return [
            Member(channel=r["channel"], agent_id=r["agent_id"], role=r["role"],
                   about=r["about"], joined_at=r["joined_at"])
            for r in rows
        ]

    def channels_of(self, agent_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel FROM members WHERE agent_id = ?", (agent_id,)
            ).fetchall()
        return [r["channel"] for r in rows]

    def list_channels(self, agent_id: str) -> list[dict[str, Any]]:
        """Channels visible to the agent: memberships plus public channels.
        Carries lightweight stats (member count, head seq, last activity) so a
        human-facing surface can show a room directory without N round-trips."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT c.name, c.private, c.created_by,
                       (m.agent_id IS NOT NULL) AS member,
                       (SELECT COUNT(*) FROM members mm
                        WHERE mm.channel = c.name) AS member_count,
                       COALESCE((SELECT MAX(seq) FROM messages ms
                                 WHERE ms.channel = c.name), 0) AS last_seq,
                       (SELECT created_at FROM messages ms
                        WHERE ms.channel = c.name
                        ORDER BY seq DESC LIMIT 1) AS last_at
                FROM channels c
                LEFT JOIN members m ON m.channel = c.name AND m.agent_id = ?
                WHERE c.private = 0 OR m.agent_id IS NOT NULL
                ORDER BY c.name
                """,
                (agent_id,),
            ).fetchall()
        return [
            {"name": r["name"], "private": bool(r["private"]),
             "created_by": r["created_by"], "member": bool(r["member"]),
             "member_count": r["member_count"], "last_seq": r["last_seq"],
             "last_at": r["last_at"]}
            for r in rows
        ]

    # -- invites -------------------------------------------------------------

    def create_invite(self, token: str, channel: str, agent_id: str | None,
                      created_by: str, ttl_seconds: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO invites (token_hash, channel, agent_id, created_by, expires_at)"
                " VALUES (?,?,?,?,?)",
                (hash_secret(token), channel, agent_id, created_by, time.time() + ttl_seconds),
            )
            self._conn.commit()

    def redeem_invite(self, token: str, agent_id: str) -> str | None:
        """Redeem a single-use invite. Returns the channel name, or None if invalid."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM invites WHERE token_hash = ?", (hash_secret(token),)
            ).fetchone()
            if row is None or row["used_at"] is not None or row["expires_at"] < now:
                return None
            if row["agent_id"] is not None and row["agent_id"] != agent_id:
                return None
            self._conn.execute(
                "UPDATE invites SET used_by = ?, used_at = ? WHERE token_hash = ?",
                (agent_id, now, row["token_hash"]),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO members (channel, agent_id, role, joined_at) VALUES (?,?,?,?)",
                (row["channel"], agent_id, "member", now),
            )
            self._conn.commit()
            return row["channel"]

    # -- join tokens (hub-membership credentials; invites discipline) ---------

    def create_join_token(self, token_id: str, secret: str, agent_id: str | None,
                          about: str, channels: list[str], created_by: str,
                          ttl_seconds: float, max_uses: int) -> dict[str, Any]:
        """Store a new join token (secret hashed, plaintext never lands).
        Expired rows are lazily purged on the way in. Returns the stored row's
        public fields; raises JoinTokenRefused(409) on a token_id collision
        (astronomically rare — the caller may simply re-mint)."""
        now = time.time()
        with self._lock:
            self._purge_expired_join_tokens_locked(now)
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO join_tokens (token_id, secret_hash, agent_id,"
                " about, channels, created_by, created_at, expires_at, max_uses)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (token_id, hash_secret(secret), agent_id, about,
                 json.dumps(channels), created_by, now, now + ttl_seconds, max_uses),
            )
            self._conn.commit()
        if cur.rowcount == 0:
            raise JoinTokenRefused(409, f"join token id '{token_id}' already exists")
        return {"token_id": token_id, "agent_id": agent_id, "about": about,
                "channels": channels, "created_by": created_by,
                "created_at": now, "expires_at": now + ttl_seconds,
                "max_uses": max_uses}

    def redeem_join_token(self, token_id: str, secret: str, agent_id: str | None,
                          name: str, api_key: str, about: str) -> tuple[AgentInfo, list[str]]:
        """Validate + register + consume as ONE locked transaction, so a token
        is consumed exactly when a registration succeeds:

        - every raise happens BEFORE any write, so a refused redemption (403 on
          expired/revoked/exhausted/id-lock, 409 on an existing agent id)
          leaves the token untouched — the 409 loser retries with a free id;
        - two racers on a single-use token serialize on the db lock: the loser
          sees uses == max_uses and gets the clean 'already used' 403.

        Returns (agent, channels) where channels are the token's preset
        channel names (the service decides which are joinable)."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM join_tokens WHERE token_id = ?", (token_id,)
            ).fetchone()
            # Constant-time compare, same posture as the admin-key gate.
            if row is None or not hmac.compare_digest(row["secret_hash"],
                                                      hash_secret(secret)):
                raise JoinTokenRefused(403, "invalid join token")
            if row["revoked_at"] is not None:
                raise JoinTokenRefused(403, "join token revoked")
            if row["expires_at"] < now:
                raise JoinTokenRefused(403, "join token expired")
            if row["uses"] >= row["max_uses"]:
                raise JoinTokenRefused(403, "join token already used")
            pinned = row["agent_id"]
            if pinned and agent_id and agent_id != pinned:
                raise JoinTokenRefused(403, f"join token is locked to '{pinned}'")
            effective = agent_id or pinned
            if not effective:
                raise JoinTokenRefused(
                    400, "this join token pins no agent id; supply one")
            if self._conn.execute("SELECT 1 FROM agents WHERE id = ?",
                                  (effective,)).fetchone():
                raise JoinTokenRefused(409, f"agent '{effective}' already exists")
            info = self._insert_agent_locked(effective, name, api_key,
                                             operator=False,  # forced server-side
                                             about=about or row["about"])
            used_by = json.loads(row["used_by"] or "[]") + [effective]
            self._conn.execute(
                "UPDATE join_tokens SET uses = uses + 1, used_by = ?"
                " WHERE token_id = ?",
                (json.dumps(used_by), token_id),
            )
            self._conn.commit()
        return info, json.loads(row["channels"] or "[]")

    def list_join_tokens(self) -> list[dict[str, Any]]:
        """All live join tokens WITHOUT secrets — the operator's audit/revoke
        surface. Expired rows are lazily purged first (kubeadm TokenCleaner
        style); exhausted/revoked ones stay listed until expiry so the
        used_by trail remains visible."""
        now = time.time()
        with self._lock:
            self._purge_expired_join_tokens_locked(now)
            rows = self._conn.execute(
                "SELECT token_id, agent_id, about, channels, created_by,"
                " created_at, expires_at, max_uses, uses, revoked_at, used_by"
                " FROM join_tokens ORDER BY created_at"
            ).fetchall()
        return [
            {"token_id": r["token_id"], "agent_id": r["agent_id"],
             "about": r["about"], "channels": json.loads(r["channels"] or "[]"),
             "created_by": r["created_by"], "created_at": r["created_at"],
             "expires_at": r["expires_at"], "max_uses": r["max_uses"],
             "uses": r["uses"], "revoked_at": r["revoked_at"],
             "used_by": json.loads(r["used_by"] or "[]")}
            for r in rows
        ]

    def revoke_join_token(self, token_id: str) -> bool:
        """Mark a token unusable (idempotent). False = no such token."""
        with self._lock:
            row = self._conn.execute(
                "SELECT revoked_at FROM join_tokens WHERE token_id = ?", (token_id,)
            ).fetchone()
            if row is None:
                return False
            if row["revoked_at"] is None:
                self._conn.execute(
                    "UPDATE join_tokens SET revoked_at = ? WHERE token_id = ?",
                    (time.time(), token_id),
                )
                self._conn.commit()
        return True

    def _purge_expired_join_tokens_locked(self, now: float) -> None:
        """Lazy cleanup on access (caller holds the lock): expired tokens are
        inert either way; dropping the rows keeps the table and the --list
        output from accreting dead entries. Commits itself so a read-only
        caller (list) never leaves the transaction open."""
        self._conn.execute("DELETE FROM join_tokens WHERE expires_at < ?", (now,))
        self._conn.commit()

    # -- messages ------------------------------------------------------------

    @staticmethod
    def _ledger_payload(**f: Any) -> str:
        """Canonical, order-stable serialization of a message's immutable fields
        for the hash chain. Deterministic (sorted keys, compact) so any party can
        recompute it from the transcript and verify the chain independently."""
        return json.dumps(f, sort_keys=True, separators=(",", ":"), default=str)

    @classmethod
    def _ledger_hash(cls, prev_hash: str, *, id: str, channel: str, seq: int, sender: str,
                     kind: str, status: str, urgency: str, critical: int, downgraded: int,
                     to: list[str], title: str, body: str, data: Any, reply_to: str | None,
                     created_at: float) -> str:
        payload = cls._ledger_payload(
            id=id, channel=channel, seq=seq, sender=sender, kind=kind, status=status,
            urgency=urgency, critical=critical, downgraded=downgraded, to=to, title=title,
            body=body, data=data, reply_to=reply_to, created_at=created_at)
        return hashlib.sha256((prev_hash + "\n" + payload).encode()).hexdigest()

    def insert_message(self, channel: str, sender: str, *, kind: str, status: str,
                       urgency: str, title: str, body: str,
                       data: dict[str, Any] | None, reply_to: str | None,
                       critical: bool = False, downgraded: bool = False,
                       to: list[str] | None = None) -> Message:
        """Insert atomically with the next per-channel seq (the order authority),
        chaining the message into the channel's append-only hash ledger."""
        now = time.time()
        msg_id = new_ulid()
        to = to or []
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM messages WHERE channel = ?",
                (channel,),
            ).fetchone()
            seq = row["next"]
            prev = self._conn.execute(
                "SELECT hash FROM messages WHERE channel = ? AND seq = ?", (channel, seq - 1)
            ).fetchone()
            prev_hash = prev["hash"] if prev and prev["hash"] else ""
            msg_hash = self._ledger_hash(
                prev_hash, id=msg_id, channel=channel, seq=seq, sender=sender, kind=kind,
                status=status, urgency=urgency, critical=int(critical),
                downgraded=int(downgraded), to=to, title=title, body=body, data=data,
                reply_to=reply_to, created_at=now)
            self._conn.execute(
                "INSERT INTO messages (id, channel, seq, sender, kind, status, urgency,"
                " critical, downgraded, to_agents, title, body, data, reply_to, created_at, hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (msg_id, channel, seq, sender, kind, status, urgency, int(critical),
                 int(downgraded), json.dumps(to), title, body,
                 json.dumps(data) if data is not None else None, reply_to, now, msg_hash),
            )
            self._conn.commit()
        return Message(
            id=msg_id, channel=channel, seq=seq, sender=sender, kind=kind, status=status,
            urgency=urgency, critical=critical, downgraded=downgraded, to=to,
            title=title, body=body, data=data, reply_to=reply_to, created_at=now,
        )

    def channel_ledger(self, channel: str) -> tuple[list[dict[str, Any]], str]:
        """The channel's verbatim: every message in seq order with its chain
        hash, plus the head hash (a compact commitment to the whole transcript).
        This is the durable, replayable common record of a room/session."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, seq, sender, kind, status, title, body, data, reply_to,"
                " created_at, hash FROM messages WHERE channel = ? ORDER BY seq", (channel,)
            ).fetchall()
        entries = []
        for r in rows:
            entries.append({
                "seq": r["seq"], "id": r["id"], "sender": r["sender"], "kind": r["kind"],
                "status": r["status"], "title": r["title"], "body": r["body"],
                "data": json.loads(r["data"]) if r["data"] else None,
                "reply_to": r["reply_to"], "created_at": r["created_at"], "hash": r["hash"],
            })
        head = entries[-1]["hash"] if entries else ""
        return entries, (head or "")

    def verify_channel(self, channel: str) -> dict[str, Any]:
        """Recompute the hash chain from the stored transcript and confirm it is
        intact. Detects any post-hoc edit/insert/reorder of a hashed message
        (its recomputed hash stops matching the stored one). Returns ok, the
        head hash, the count of chained messages, and the first broken seq."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE channel = ? ORDER BY seq", (channel,)
            ).fetchall()
        prev_hash = ""
        chained = 0
        broken_at: int | None = None
        head = ""
        for r in rows:
            if r["hash"] is None:  # legacy pre-ledger row: chain starts after it
                prev_hash = ""
                continue
            m = self._row_to_message(r)
            expect = self._ledger_hash(
                prev_hash, id=m.id, channel=m.channel, seq=m.seq, sender=m.sender,
                kind=m.kind.value if hasattr(m.kind, "value") else m.kind,
                status=m.status.value if hasattr(m.status, "value") else m.status,
                urgency=m.urgency.value if hasattr(m.urgency, "value") else m.urgency,
                critical=int(m.critical), downgraded=int(m.downgraded), to=m.to,
                title=m.title, body=m.body, data=m.data, reply_to=m.reply_to,
                created_at=m.created_at)
            if expect != r["hash"] and broken_at is None:
                broken_at = m.seq
            prev_hash = r["hash"]
            head = r["hash"]
            chained += 1
        return {"ok": broken_at is None, "head": head, "count": chained,
                "broken_at": broken_at}

    def get_message(self, message_id: str) -> Message | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return self._row_to_message(row) if row else None

    def get_messages(self, channel: str, since_seq: int = 0, limit: int = 200) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE channel = ? AND seq > ? ORDER BY seq LIMIT ?",
                (channel, since_seq, limit),
            ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def last_seq(self, channel: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM messages WHERE channel = ?", (channel,)
            ).fetchone()
        return row["s"]

    @staticmethod
    def _row_to_message(r: sqlite3.Row) -> Message:
        return Message(
            id=r["id"], channel=r["channel"], seq=r["seq"], sender=r["sender"],
            kind=r["kind"], status=r["status"], urgency=r["urgency"],
            critical=bool(r["critical"]), downgraded=bool(r["downgraded"]),
            to=json.loads(r["to_agents"]), title=r["title"],
            body=r["body"], data=json.loads(r["data"]) if r["data"] else None,
            reply_to=r["reply_to"], created_at=r["created_at"],
        )

    # -- read receipts (body reads; distinct from triage cursors) ---------------

    def mark_read(self, message_id: str, agent_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO reads (message_id, agent_id, read_at) VALUES (?,?,?)",
                (message_id, agent_id, time.time()),
            )
            self._conn.commit()

    def has_read(self, message_id: str, agent_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM reads WHERE message_id = ? AND agent_id = ?",
                (message_id, agent_id),
            ).fetchone()
        return row is not None

    def unread_criticals(self, agent_id: str, channels: list[str]) -> list[Message]:
        """Critical messages stay pinned until the agent actually reads the body."""
        if not channels:
            return []
        placeholders = ",".join("?" for _ in channels)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT m.* FROM messages m
                WHERE m.critical = 1 AND m.sender != ? AND m.channel IN ({placeholders})
                  AND NOT EXISTS (SELECT 1 FROM reads r
                                  WHERE r.message_id = m.id AND r.agent_id = ?)
                ORDER BY m.created_at
                """,
                (agent_id, *channels, agent_id),
            ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def replies_to(self, message_id: str) -> list[Message]:
        """All messages replying to `message_id`, in channel (seq) order. Used to
        compute per-ask obligation discharge (uses idx_messages_reply_to)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE reply_to = ? ORDER BY seq", (message_id,)
            ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def unread_obligation_candidates(self, agent_id: str, channels: list[str]) -> list[Message]:
        """CANDIDATE obligations to the agent's channels: status open/blocked,
        not sent by the agent, and not yet read by the agent. The 'is it
        answered?' test is applied by the service via `discharge_state`, because
        structured asks need per-ask discharge (a partial answer must keep the
        message pinned) — a single SQL 'any reply exists' cannot express that.
        These stay pinned regardless of the triage cursor, so acking an envelope
        cannot bury a rotting obligation (v0.3 bug C-4); they clear when the
        agent reads the message or when every ask is answered."""
        if not channels:
            return []
        placeholders = ",".join("?" for _ in channels)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT m.* FROM messages m
                WHERE m.status IN ('open', 'blocked') AND m.sender != ?
                  AND m.channel IN ({placeholders})
                  AND NOT EXISTS (SELECT 1 FROM reads r
                                  WHERE r.message_id = m.id AND r.agent_id = ?)
                ORDER BY m.created_at
                """,
                (agent_id, *channels, agent_id),
            ).fetchall()
        return [self._row_to_message(r) for r in rows]

    # -- colleague notes (private, subjective, free-text) ------------------------

    def set_note(self, observer: str, subject: str, note: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO notes (observer, subject, note, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT (observer, subject) DO UPDATE SET"
                " note = excluded.note, updated_at = excluded.updated_at",
                (observer, subject, note, time.time()),
            )
            self._conn.commit()

    def get_note(self, observer: str, subject: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM notes WHERE observer = ? AND subject = ?", (observer, subject)
            ).fetchone()
        return dict(row) if row else None

    def get_notes(self, observer: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notes WHERE observer = ? ORDER BY subject", (observer,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- per-channel store (KV with compare-and-swap) -------------------------

    def store_get(self, channel: str, key: str) -> StoreEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM store WHERE channel = ? AND key = ?", (channel, key)
            ).fetchone()
        if row is None:
            return None
        return StoreEntry(
            channel=row["channel"], key=row["key"], value=json.loads(row["value"]),
            version=row["version"], updated_by=row["updated_by"], updated_at=row["updated_at"],
        )

    def store_set(self, channel: str, key: str, value: Any, updated_by: str,
                  expect_version: int | None = None) -> StoreEntry:
        """Set a key. `expect_version` enables CAS: 0 means "must not exist yet"."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT version FROM store WHERE channel = ? AND key = ?", (channel, key)
            ).fetchone()
            current = row["version"] if row else 0
            if expect_version is not None and expect_version != current:
                raise StoreConflict(current)
            new_version = current + 1
            self._conn.execute(
                "INSERT INTO store (channel, key, value, version, updated_by, updated_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT (channel, key) DO UPDATE SET"
                " value=excluded.value, version=excluded.version,"
                " updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (channel, key, json.dumps(value), new_version, updated_by, now),
            )
            self._conn.commit()
        return StoreEntry(channel=channel, key=key, value=value, version=new_version,
                          updated_by=updated_by, updated_at=now)

    def store_keys(self, channel: str) -> list[dict[str, Any]]:
        # Virtual-filesystem keys (fs/<path>) are excluded: they belong to the
        # fs_* API and namespace, not the generic KV store, so they never leak
        # into the store listing (namespace hygiene — independent-tester nuance).
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, version, updated_by, updated_at FROM store"
                " WHERE channel = ? AND key NOT LIKE 'fs/%' ORDER BY key",
                (channel,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- virtual filesystem storage (monotonic version, tombstone delete) --------
    #
    # Files reuse the `store` table but need a version that is monotonic across
    # a path's ENTIRE lifetime, not per-row. A plain delete + recreate would
    # reset the version to 1, so a stale pre-delete version could pass a CAS
    # check and clobber the recreated file (an ABA hazard found by an
    # independent tester). Fix: delete is a TOMBSTONE (the row persists with a
    # `deleted` marker and a bumped version), so the version never rewinds and
    # CAS remains a valid fencing token across delete/recreate cycles.

    def fs_get(self, channel: str, key: str) -> dict[str, Any] | None:
        """Return {value, version, updated_by, updated_at, deleted} or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value, version, updated_by, updated_at FROM store"
                " WHERE channel = ? AND key = ?", (channel, key)
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["value"])
        return {"value": value, "version": row["version"], "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
                "deleted": bool(isinstance(value, dict) and value.get("deleted"))}

    def fs_put(self, channel: str, key: str, value: dict[str, Any], updated_by: str,
               expect_version: int | None = None) -> StoreEntry:
        """Create/overwrite a file. `expect_version` semantics: for a live file
        it must equal the current version; for an absent-or-tombstoned path
        (creation) it must be 0. The new version always continues the path's
        monotonic sequence (current + 1), never resetting to 1."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT value, version FROM store WHERE channel = ? AND key = ?",
                (channel, key),
            ).fetchone()
            current = row["version"] if row else 0
            is_deleted = bool(row and isinstance(json.loads(row["value"]), dict)
                              and json.loads(row["value"]).get("deleted"))
            exists_live = row is not None and not is_deleted
            if expect_version is not None:
                # Creation (absent/tombstoned) requires 0; editing a live file
                # requires the exact current version. Either way a mismatch is a
                # conflict reporting the true current version.
                want = current if exists_live else 0
                if expect_version != want:
                    raise StoreConflict(current)
            new_version = current + 1
            self._conn.execute(
                "INSERT INTO store (channel, key, value, version, updated_by, updated_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT (channel, key) DO UPDATE SET"
                " value=excluded.value, version=excluded.version,"
                " updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (channel, key, json.dumps(value), new_version, updated_by, now),
            )
            # Same transaction: the head and its archived copy can never
            # disagree (a v6 write no longer destroys what v1..v5 said).
            self._conn.execute(
                "INSERT OR REPLACE INTO fs_versions"
                " (channel, key, version, value, updated_by, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (channel, key, new_version, json.dumps(value), updated_by, now),
            )
            self._conn.commit()
        return StoreEntry(channel=channel, key=key, value=value, version=new_version,
                          updated_by=updated_by, updated_at=now)

    def fs_remove(self, channel: str, key: str, updated_by: str,
                  expect_version: int | None = None) -> int | None:
        """Tombstone a live file (CAS via `expect_version`). Returns the new
        (bumped) version, or None if the path was absent or already deleted."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT value, version FROM store WHERE channel = ? AND key = ?",
                (channel, key),
            ).fetchone()
            if row is None:
                return None
            if isinstance(json.loads(row["value"]), dict) and json.loads(row["value"]).get("deleted"):
                return None  # already a tombstone
            if expect_version is not None and expect_version != row["version"]:
                raise StoreConflict(row["version"])
            new_version = row["version"] + 1
            self._conn.execute(
                "UPDATE store SET value=?, version=?, updated_by=?, updated_at=?"
                " WHERE channel = ? AND key = ?",
                (json.dumps({"deleted": True}), new_version, updated_by, now, channel, key),
            )
            # The delete itself is an archived, attributed version (value NULL).
            self._conn.execute(
                "INSERT OR REPLACE INTO fs_versions"
                " (channel, key, version, value, updated_by, updated_at)"
                " VALUES (?,?,?,NULL,?,?)",
                (channel, key, new_version, updated_by, now),
            )
            self._conn.commit()
        return new_version

    def fs_version(self, channel: str, key: str, version: int) -> dict[str, Any] | None:
        """One archived version's content + provenance, or None if that
        version was never archived (pre-archive files) or was a delete."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value, version, updated_by, updated_at FROM fs_versions"
                " WHERE channel = ? AND key = ? AND version = ?",
                (channel, key, version),
            ).fetchone()
        if row is None or row["value"] is None:
            return None
        return {"value": json.loads(row["value"]), "version": row["version"],
                "updated_by": row["updated_by"], "updated_at": row["updated_at"]}

    def fs_keys_live(self, channel: str, prefix: str) -> list[dict[str, Any]]:
        """List non-tombstoned file keys under a prefix (deleted files excluded
        server-side via json_extract, so a lister never sees a removed file).
        Carries the writer's description, a short content head (fallback
        description material) and the content size — listing stays one query,
        never a content fetch per file."""
        pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, version, updated_by, updated_at,"
                "       COALESCE(json_extract(value, '$.description'), '') AS description,"
                "       substr(COALESCE(json_extract(value, '$.content'), ''), 1, 200) AS head,"
                "       LENGTH(COALESCE(json_extract(value, '$.content'), '')) AS size"
                " FROM store"
                " WHERE channel = ? AND key LIKE ? ESCAPE '\\'"
                " AND COALESCE(json_extract(value, '$.deleted'), 0) = 0 ORDER BY key",
                (channel, pattern),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- cursors (what has each agent seen) ------------------------------------

    def get_cursor(self, agent_id: str, channel: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seq FROM cursors WHERE agent_id = ? AND channel = ?",
                (agent_id, channel),
            ).fetchone()
        return row["last_seq"] if row else 0

    def set_cursor(self, agent_id: str, channel: str, seq: int) -> None:
        """Advance (never rewind) the agent's read cursor."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO cursors (agent_id, channel, last_seq) VALUES (?,?,?)"
                " ON CONFLICT (agent_id, channel) DO UPDATE SET"
                " last_seq = MAX(last_seq, excluded.last_seq)",
                (agent_id, channel, seq),
            )
            self._conn.commit()

    def checkpoint(self) -> None:
        """Fold the WAL back into the main database file. Called on graceful
        shutdown so a backup that copies only `agora.db` is complete and the
        WAL does not grow unbounded across a long-lived hub's lifetime."""
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def ping(self) -> bool:
        """Cheap liveness probe for /healthz."""
        with self._lock:
            self._conn.execute("SELECT 1")
        return True

    def close(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
