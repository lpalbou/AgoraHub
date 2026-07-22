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
    created_at  REAL NOT NULL,
    retired_at  REAL,             -- NULL = active; set = retired (0089): auth
                                  -- refused neutrally, off rosters, id reserved
    retired_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS channels (
    name        TEXT PRIMARY KEY,
    private     INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    archived_at REAL              -- NULL = live; set = archived (0090): evicted,
                                  -- delisted, posts/joins refused, history kept
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
    retracted_at   REAL,            -- author/operator retraction (0097): redact-at-read
    retracted_by   TEXT,            -- who retracted (author or an operator)
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
-- Charter read receipts: "version N of this channel's charter was DELIVERED
-- to agent A" (recorded when the head is read; writing your own edit counts).
-- This is the honest, machine-attestable core of "accepted the rules" — it
-- proves delivery, never understanding. The norms_required post gate keys on it.
-- Message attachments (0091): channel-scoped, content-addressed, immutable
-- blobs. id = sha256(bytes), so identical bytes dedup within a channel and
-- a message's data.attachments commits the LEDGER to the exact file bytes
-- (offline-verifiable). Bytes live here rather than on disk to preserve the
-- single-file backup property of the hub database.
CREATE TABLE IF NOT EXISTS blobs (
    channel      TEXT NOT NULL,
    id           TEXT NOT NULL,          -- sha256 hex of the bytes
    filename     TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size         INTEGER NOT NULL,
    bytes        BLOB NOT NULL,
    created_by   TEXT NOT NULL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (channel, id)
);
CREATE TABLE IF NOT EXISTS charter_receipts (
    agent_id    TEXT NOT NULL,
    channel     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    read_at     REAL NOT NULL,
    PRIMARY KEY (agent_id, channel)
);
-- The hub rules the operator serves to every agent via /whoami (single row).
-- No row = the packaged default text (version 0) is served.
CREATE TABLE IF NOT EXISTS hub_rules (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    text        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    updated_at  REAL NOT NULL
);
-- Delegation record (0068): the operator's delegate as verifiable hub state
-- — who, which POWERS (ruling/operational/reporting), until when. Grants are
-- append-only rows; the active grant for an agent is the newest unrevoked,
-- unexpired one. Prose claims of delegation count for nothing.
CREATE TABLE IF NOT EXISTS delegations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    powers      TEXT NOT NULL,          -- JSON list, subset of ruling|operational|reporting
    granted_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    revoked_at  REAL,
    note        TEXT NOT NULL DEFAULT ''
);
-- Operator pause (0069): singleton state + the interval history. Persisted
-- so a hub restart cannot silently resume the fleet; intervals feed the
-- escalation-clock exclusion (a pause must never age obligations into an
-- SLA-breach storm). ended_at NULL = the pause is ongoing.
CREATE TABLE IF NOT EXISTS hub_pauses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    reason      TEXT NOT NULL DEFAULT '',
    started_by  TEXT NOT NULL DEFAULT 'operator'
);
-- Moderation blocks (kick/ban): scope is 'hub' or a channel name. A kick is
-- a block with an expiry; a ban has expires_at NULL (forever). Rows are
-- append-only history; the ACTIVE block for (scope, agent) is the newest
-- unlifted, unexpired one. Like delegations, authority is verifiable state:
-- enforcement reads these rows, never anyone's prose.
CREATE TABLE IF NOT EXISTS blocks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,          -- 'hub' | channel name
    agent_id    TEXT NOT NULL,
    imposed_by  TEXT NOT NULL,
    imposed_at  REAL NOT NULL,
    expires_at  REAL,                   -- NULL = ban (no expiry)
    lifted_at   REAL,
    reason      TEXT NOT NULL DEFAULT ''
);
-- block_get runs inside authenticate() on EVERY request; this append-only
-- table never prunes, so index the lookup key to keep it O(log n) rather
-- than a growing reverse table scan (review F6).
CREATE INDEX IF NOT EXISTS idx_blocks_scope_agent ON blocks (scope, agent_id);
-- Reputation (0094): peer-assigned ±1 on four fixed axes, per channel.
-- ONE live vote per (channel, target, rater, axis) — revising overwrites the
-- row (updated_at moves), so the table IS the audit trail: who stands where
-- on whom, right now, with full attribution. Identity-bound (rater is the
-- authenticated agent), self-votes refused in the service, membership
-- required for both parties. Aggregation: docs/protocol.md 'Reputation'.
CREATE TABLE IF NOT EXISTS reputation_votes (
    channel     TEXT NOT NULL,
    target      TEXT NOT NULL,
    rater       TEXT NOT NULL,
    axis        TEXT NOT NULL,          -- trust|wisdom|thorough|helper
    value       INTEGER NOT NULL,       -- +1 | -1
    note        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (channel, target, rater, axis)
);
CREATE INDEX IF NOT EXISTS idx_reputation_target ON reputation_votes (target);
-- One-reputation unification (agora-0122, operator ruling dm#111: "giving
-- +/- points IS defining reputation"): a ± on a MESSAGE is reputation input
-- about its sender, held as ONE standing rating per (message, rater) —
-- PUT replaces, DELETE withdraws, exactly the toggle semantics the web UI
-- already speaks. All columns NOT NULL and message_id in the PRIMARY KEY:
-- SQLite treats NULLs in unique indexes as pairwise DISTINCT, so a nullable
-- evidence column would let unlimited duplicate rows past the idempotency
-- key (adversary-reproduced, 54 dupes). Direct agent votes stay in
-- reputation_votes — the two natural keys are incompatible (also
-- adversary-reproduced); the SYSTEM is unified at the aggregation layer.
CREATE TABLE IF NOT EXISTS message_ratings (
    channel     TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    rater       TEXT NOT NULL,
    target      TEXT NOT NULL,          -- message sender at cast time
    value       INTEGER NOT NULL,       -- +1 | -1
    note        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (message_id, rater)
);
CREATE INDEX IF NOT EXISTS idx_message_ratings_target ON message_ratings (target);
CREATE INDEX IF NOT EXISTS idx_message_ratings_channel ON message_ratings (channel, rater);
-- Hub-level metadata (0.12.20): tiny key/value row set, currently the
-- directive-debt epoch (semantics changes must not rewrite history — a
-- message posted under old rules must not become a debt retroactively).
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
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
            # Message retraction (0097): redact-at-read columns on a
            # pre-existing table. NULL = live (never retracted), so the
            # migration is a no-op for all existing rows.
            if "retracted_at" not in cols:
                self._conn.execute("ALTER TABLE messages ADD COLUMN retracted_at REAL")
            if "retracted_by" not in cols:
                self._conn.execute("ALTER TABLE messages ADD COLUMN retracted_by TEXT")
            # Channel archive (0090) + agent retirement (0089): lifecycle
            # columns added to pre-existing tables. Older rows default NULL
            # (live / active), so the migration is a no-op for existing state.
            chan_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(channels)")}
            if "archived_at" not in chan_cols:
                self._conn.execute("ALTER TABLE channels ADD COLUMN archived_at REAL")
            agent_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(agents)")}
            if "retired_at" not in agent_cols:
                self._conn.execute("ALTER TABLE agents ADD COLUMN retired_at REAL")
            if "retired_reason" not in agent_cols:
                self._conn.execute(
                    "ALTER TABLE agents ADD COLUMN retired_reason TEXT NOT NULL DEFAULT ''")
            self._conn.commit()
        self._migrate_operator_reactions()

    def _migrate_operator_reactions(self) -> None:
        """One-time conversion of OPERATOR-cast reaction rows into message
        ratings (agora-0122). The operator's thumbs clicks landed in the web
        UI's `reactions:<message-id>` store convention while he believed he
        was rating agents; his ruling (dm#111: 'giving +/- points IS
        defining reputation') attests what those clicks MEANT, so his — and
        ONLY his class of — rows migrate. Agent-cast reactions are NOT
        converted (adversary P0: store rows are member-writable data;
        converting them would mint identity-bound attestations nobody made
        — as it happens, zero agent reactions exist, but the guard is the
        principle, not the census).

        Skips, per the design review: all-empty rows (withdrawn is a
        statement), missing/retracted/system-authored target messages,
        self-reactions, unregistered senders. Guarded by a meta key so a
        later withdrawal is never resurrected by re-running; INSERT OR
        IGNORE keeps it idempotent within the run."""
        with self._lock:
            done = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'reactions_migrated'"
            ).fetchone()
            if done:
                return
            operators = {r["id"] for r in self._conn.execute(
                "SELECT id FROM agents WHERE operator = 1")}
            registered = {r["id"] for r in self._conn.execute(
                "SELECT id FROM agents")}
            rows = self._conn.execute(
                "SELECT channel, key, value FROM store"
                " WHERE key LIKE 'reactions:%'").fetchall()
            now = time.time()
            migrated = 0
            for row in rows:
                try:
                    value = json.loads(row["value"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(value, dict):
                    continue
                message_id = row["key"][len("reactions:"):]
                m = self._conn.execute(
                    "SELECT sender, kind, retracted_at FROM messages"
                    " WHERE id = ? AND channel = ?",
                    (message_id, row["channel"])).fetchone()
                if (m is None or m["kind"] != "message"
                        or m["retracted_at"] is not None
                        or m["sender"] not in registered):
                    continue
                for sign, names in ((1, value.get("up")), (-1, value.get("down"))):
                    for rater in (names or []):
                        if rater not in operators or rater == m["sender"]:
                            continue
                        self._conn.execute(
                            "INSERT OR IGNORE INTO message_ratings"
                            " (channel, message_id, rater, target, value,"
                            "  note, created_at, updated_at)"
                            " VALUES (?,?,?,?,?,?,?,?)",
                            (row["channel"], message_id, rater, m["sender"],
                             sign, "migrated from reaction (agora-0122)",
                             now, now))
                        migrated += 1
            self._conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES"
                " ('reactions_migrated', ?)", (f"{migrated} at {now}",))
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

    def agent_retirement(self, agent_id: str) -> dict[str, Any] | None:
        """The agent's retirement record ({retired_at, reason}) or None if
        active. Distinct from a block: retirement is neutral lifecycle, not
        moderation (0089)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT retired_at, retired_reason FROM agents WHERE id = ?",
                (agent_id,)).fetchone()
        if row is None or row["retired_at"] is None:
            return None
        return {"retired_at": row["retired_at"], "reason": row["retired_reason"]}

    def list_retired_agents(self) -> list[dict[str, Any]]:
        """Retired agents ({id, reason, retired_at}), newest first. The ONLY
        surface that enumerates them — they are off every roster by design,
        so an operator un-retire UI needs this list to offer candidates
        (0089 consumer gap, continuum dm#17)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, retired_reason, retired_at FROM agents "
                "WHERE retired_at IS NOT NULL ORDER BY retired_at DESC").fetchall()
        return [{"id": r["id"], "reason": r["retired_reason"],
                 "retired_at": r["retired_at"]} for r in rows]

    def retire_agent(self, agent_id: str, reason: str) -> list[str]:
        """Retire an agent: neutral end-of-life, NOT a block. Sets the
        retirement record and removes ALL channel memberships (roster
        exclusion) — the id stays in `agents` forever so message attribution
        can never be hijacked by re-registration. Returns the channels the
        agent was evicted from. Idempotent (reason refreshes).

        Also withdraws the retiree's reputation votes AS A RATER (0094
        hardening): a decommissioned seat must not keep voting weight it
        can no longer stand behind — otherwise a farm's sock-puppets keep
        pumping after retirement. Votes ABOUT the agent are kept: they are
        colleagues' standing record, unaffected by the target's exit."""
        now = time.time()
        with self._lock:
            channels = [r["channel"] for r in self._conn.execute(
                "SELECT channel FROM members WHERE agent_id = ?", (agent_id,)).fetchall()]
            self._conn.execute(
                "UPDATE agents SET retired_at = COALESCE(retired_at, ?), "
                "retired_reason = ? WHERE id = ?", (now, reason, agent_id))
            self._conn.execute("DELETE FROM members WHERE agent_id = ?", (agent_id,))
            self._conn.execute("DELETE FROM reputation_votes WHERE rater = ?",
                               (agent_id,))
            # Same F2 hardening for message ratings (agora-0122): a
            # decommissioned seat's ± weight retires with it; ratings ABOUT
            # its messages (cast by others) stay, like votes about it.
            self._conn.execute("DELETE FROM message_ratings WHERE rater = ?",
                               (agent_id,))
            self._conn.commit()
        return channels

    def unretire_agent(self, agent_id: str) -> None:
        """Restore a retired agent's auth (operator only, service-gated).
        Memberships are NOT restored — rejoining rooms is explicit."""
        with self._lock:
            self._conn.execute(
                "UPDATE agents SET retired_at = NULL, retired_reason = '' WHERE id = ?",
                (agent_id,))
            self._conn.commit()

    def list_agent_ids(self, *, include_retired: bool = False) -> list[str]:
        """Registered agent ids (operator-scope surface only). Retired seats
        are EXCLUDED by default (c3707/M2, hub-alerts#224): the dark watchdog
        alerted 'agency is offline holding 33 SLA-breached obligations' six
        hours after agency was retired — the hub itself hand-carried a stale
        row while teaching agents 'derive, never remember'. Every live-fleet
        derivation (watchdog, status overview, presence) must not see
        decommissioned seats; pass include_retired=True only for surfaces
        that deliberately show history."""
        q = "SELECT id FROM agents"
        if not include_retired:
            q += " WHERE retired_at IS NULL"
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY id").fetchall()
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

    def channel_archived(self, name: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT archived_at FROM channels WHERE name = ?", (name,)).fetchone()
        return row is not None and row["archived_at"] is not None

    def archive_channel(self, name: str) -> list[str]:
        """Mark a channel archived and EVICT every member (channel-scoped —
        hub membership and identities are untouched). Messages, store, fs and
        blobs stay in the DB (append-only; operator-readable). Returns the
        evicted member ids for the announcement/audit. Idempotent."""
        now = time.time()
        with self._lock:
            evicted = [r["agent_id"] for r in self._conn.execute(
                "SELECT agent_id FROM members WHERE channel = ?", (name,)).fetchall()]
            self._conn.execute(
                "UPDATE channels SET archived_at = COALESCE(archived_at, ?) WHERE name = ?",
                (now, name))
            self._conn.execute("DELETE FROM members WHERE channel = ?", (name,))
            self._conn.commit()
        return evicted

    def unarchive_channel(self, name: str) -> None:
        """Clear archived state (operator only, service-gated). Members are
        NOT restored — rejoin/re-invite is an explicit act."""
        with self._lock:
            self._conn.execute(
                "UPDATE channels SET archived_at = NULL WHERE name = ?", (name,))
            self._conn.commit()

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

    def channel_names(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT name FROM channels").fetchall()
        return [r["name"] for r in rows]

    def channels_of(self, agent_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel FROM members WHERE agent_id = ?", (agent_id,)
            ).fetchall()
        return [r["channel"] for r in rows]

    def list_channels(self, agent_id: str,
                      include_archived: bool = False) -> list[dict[str, Any]]:
        """Channels visible to the agent: memberships plus public channels.
        Carries lightweight stats (member count, head seq, last activity) so a
        human-facing surface can show a room directory without N round-trips.
        Archived channels (0090) are excluded unless `include_archived` (the
        operator's inspect view) — an archived room has no members anyway, so
        it only lingers here while public and un-evicted."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT c.name, c.private, c.created_by, c.archived_at,
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
                WHERE (c.private = 0 OR m.agent_id IS NOT NULL)
                  AND (? OR c.archived_at IS NULL)
                ORDER BY c.name
                """,
                (agent_id, int(include_archived)),
            ).fetchall()
        return [
            {"name": r["name"], "private": bool(r["private"]),
             "created_by": r["created_by"], "member": bool(r["member"]),
             "member_count": r["member_count"], "last_seq": r["last_seq"],
             "last_at": r["last_at"],
             "archived": r["archived_at"] is not None}
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
        for the hash chain — the byte-exact definition lives in docs/protocol.md
        ("Canonicalization"). Deterministic (sorted keys, compact, ASCII-only)
        so any party can recompute it from the served transcript. allow_nan
        fails loudly on non-finite floats: they are refused at the post
        boundary, and a value that cannot round-trip as strict JSON must never
        enter the chain (it would hash here but break every consumer)."""
        return json.dumps(f, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)

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
        This is the durable, replayable common record of a room/session.

        Every hashed field is served (urgency, critical, downgraded, to
        included), so a third party can recompute the chain from this response
        alone — see docs/protocol.md "Verbatim ledger" for the byte-exact
        canonicalization and scripts/verify_ledger.py for a standalone,
        stdlib-only verifier written from that text."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, seq, sender, kind, status, urgency, critical, downgraded,"
                " to_agents, title, body, data, reply_to, created_at, hash"
                " FROM messages WHERE channel = ? ORDER BY seq", (channel,)
            ).fetchall()
        entries = []
        for r in rows:
            entries.append({
                "seq": r["seq"], "id": r["id"], "sender": r["sender"], "kind": r["kind"],
                "status": r["status"], "urgency": r["urgency"],
                # 0/1 ints, exactly as they enter the canonical payload.
                "critical": r["critical"], "downgraded": r["downgraded"],
                "to": json.loads(r["to_agents"]) if r["to_agents"] else [],
                "title": r["title"], "body": r["body"],
                "data": json.loads(r["data"]) if r["data"] else None,
                "reply_to": r["reply_to"], "created_at": r["created_at"], "hash": r["hash"],
            })
        # The head is the last HASHED turn's hash (protocol.md rule 4) — not
        # the last row's. They differ only when a trailing row has been
        # un-hashed by direct DB tampering; serving "" there would make the
        # hub disagree with a doc-faithful external verifier.
        head = next((e["hash"] for e in reversed(entries) if e["hash"]), "")
        return entries, head

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
            if r["hash"] is None:
                # Legacy pre-ledger rows precede the first hashed turn (every
                # insert hashes, and seq is append-only, so the hub can never
                # interleave them). An unhashed row AFTER a hashed one is
                # therefore evidence of direct DB tampering — flag it instead
                # of silently restarting the chain (spec review, 0.9.0).
                if chained and broken_at is None:
                    broken_at = r["seq"]
                prev_hash = ""
                continue
            m = self._row_to_message(r, redact=False)  # hash commits to ORIGINAL bytes
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

    def get_message(self, message_id: str, *, redact: bool = True) -> Message | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return self._row_to_message(row, redact=redact) if row else None

    def get_messages(self, channel: str, since_seq: int = 0, limit: int = 200) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE channel = ? AND seq > ? ORDER BY seq LIMIT ?",
                (channel, since_seq, limit),
            ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def retract_message(self, message_id: str, by: str) -> None:
        """Mark a message retracted (0097): redact-at-read on every
        agent-facing surface, original bytes preserved in the row for
        operator audit and for the ledger hash (which commits to the
        original — retraction is presentation, not a chain rewrite).
        Idempotent; first retractor's identity/time stick."""
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET retracted_at = COALESCE(retracted_at, ?), "
                "retracted_by = COALESCE(retracted_by, ?) WHERE id = ?",
                (time.time(), by, message_id))
            self._conn.commit()

    def last_seq(self, channel: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM messages WHERE channel = ?", (channel,)
            ).fetchone()
        return row["s"]

    @staticmethod
    def _row_to_message(r: sqlite3.Row, *, redact: bool = True) -> Message:
        keys = r.keys()
        retracted_at = r["retracted_at"] if "retracted_at" in keys else None
        retracted_by = r["retracted_by"] if "retracted_by" in keys else None
        title, body = r["title"], r["body"]
        data = json.loads(r["data"]) if r["data"] else None
        status = r["status"]
        # Redact-at-read (0097): a retracted message serves a tombstone on
        # every agent-facing surface — the words are unreachable through any
        # API, so a future entity can never consume them, AND its status
        # downgrades to fyi so it obliges nobody (an open message you
        # retract must stop showing as an unanswered question). `redact=False`
        # is for the ledger's hash verification ONLY (it commits to the
        # original bytes + original status, preserved for operator audit).
        if retracted_at is not None and redact:
            title = ""
            body = f"[retracted by {retracted_by or r['sender']}]"
            data = None  # drop asks/answers/attachments: nothing consumable
            status = "fyi"
        return Message(
            id=r["id"], channel=r["channel"], seq=r["seq"], sender=r["sender"],
            kind=r["kind"], status=status, urgency=r["urgency"],
            critical=bool(r["critical"]), downgraded=bool(r["downgraded"]),
            to=json.loads(r["to_agents"]), title=title,
            body=body, data=data,
            reply_to=r["reply_to"], created_at=r["created_at"],
            retracted=retracted_at is not None, retracted_at=retracted_at,
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
                  AND m.retracted_at IS NULL
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

    def replies_map(self, message_ids: list[str]) -> dict[str, list[Message]]:
        """Replies to EACH of `message_ids`, grouped by parent — one batched
        query per 500-id chunk for a whole history page (parity move 2,
        agora-0118) instead of the N per-row `replies_to` calls that
        decorating every listed message would otherwise cost. Chunked because
        a 1000-row page (the route's max) would exceed the 999
        bound-variable ceiling of older bundled SQLites (modern builds allow
        32766, but the package must not depend on the host's build). Order
        within each group is seq order, matching replies_to."""
        out: dict[str, list[Message]] = {}
        for i in range(0, len(message_ids), 500):
            chunk = message_ids[i:i + 500]
            placeholders = ",".join("?" for _ in chunk)
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT * FROM messages WHERE reply_to IN ({placeholders}) "
                    "ORDER BY seq", (*chunk,),
                ).fetchall()
            for r in rows:
                out.setdefault(r["reply_to"], []).append(self._row_to_message(r))
        return out

    def get_message_by_seq(self, channel: str, seq: int) -> Message | None:
        """Positional lookup (parity move 2): chat's `/read N` and every UI's
        'jump to #N' used to page history and search client-side — the hub
        has the index."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE channel = ? AND seq = ?",
                (channel, seq)).fetchone()
        return self._row_to_message(row) if row else None

    def obligation_candidates(self, agent_id: str, channels: list[str]) -> list[Message]:
        """CANDIDATE obligations to the agent's channels: status open/blocked,
        not sent by the agent — READ OR NOT. The 'is it answered?' test is
        applied by the service via `discharge_state` (structured asks need
        per-ask discharge), and the read-release policy is the service's too:
        since the 0080 watcher audit, a bare read releases only BYSTANDERS —
        an ADDRESSED obligation stays pinned until its addressee engages,
        because read+ack was exactly how lurking seats blinded the inbox,
        `agora status`, the stop hook, and the dark watchdog all at once.
        These stay pinned regardless of the triage cursor, so acking an
        envelope cannot bury a rotting obligation (v0.3 bug C-4)."""
        if not channels:
            return []
        placeholders = ",".join("?" for _ in channels)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT m.* FROM messages m
                WHERE m.status IN ('open', 'blocked') AND m.sender != ?
                  AND m.channel IN ({placeholders})
                  AND m.retracted_at IS NULL
                ORDER BY m.created_at
                """,
                (agent_id, *channels),
            ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def open_obligations(self, channels: list[str]) -> list[Message]:
        """Every open/blocked message in these channels, READ OR NOT (0079):
        the owed surface deliberately ignores read receipts — read-but-
        unanswered is precisely the lurk case the receipt filter would hide."""
        if not channels:
            return []
        placeholders = ",".join("?" for _ in channels)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT m.* FROM messages m
                WHERE m.status IN ('open', 'blocked')
                  AND m.channel IN ({placeholders})
                  AND m.retracted_at IS NULL
                ORDER BY m.created_at
                """,
                (*channels,),
            ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def addressed_directives(self, channels: list[str]) -> list[Message]:
        """Reply/fyi messages that NAME specific recipients (to_agents
        non-empty), read or not (0101, widened 0102). Replies and fyi
        normally oblige nobody — a peer answering your ask is discharge, not
        a new debt, and obliging every reply would create ping-pong. But an
        ADDRESSED one can carry a directive ('now do X') that must not
        silently drop: the service applies the per-sender/per-viewer
        exemptions (answers, replies-to-your-own-message, operator authority,
        the pre-feature epoch) and treats the rest as obligations each named
        seat owes. Retracted rows excluded like every read surface."""
        if not channels:
            return []
        placeholders = ",".join("?" for _ in channels)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT m.* FROM messages m
                WHERE m.status IN ('reply', 'fyi') AND m.to_agents != '[]'
                  AND m.kind = 'message'
                  AND m.channel IN ({placeholders})
                  AND m.retracted_at IS NULL
                ORDER BY m.created_at
                """,
                (*channels,),
            ).fetchall()
        return [self._row_to_message(r) for r in rows]

    # -- hub metadata (tiny KV; currently the directive-debt epoch) -------------

    def meta_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def meta_set_default(self, key: str, value: str) -> str:
        """Set `key` only if absent; return the stored value either way —
        one atomic read-or-init so every hub start agrees on the epoch."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?,?)",
                (key, value))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"]

    def meta_set(self, key: str, value: str) -> None:
        """Upsert a meta key. Used for restart-durable watchdog flap guards
        (c3436): a re-alert cooldown kept only in process memory re-fires
        the whole DARK/DEAF wave on every hub restart."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value))
            self._conn.commit()

    def my_open_messages(self, sender: str, channels: list[str]) -> list[Message]:
        """The agent's own still-open questions (0078): the messages whose
        incoming answers can create a consumption debt for their asker."""
        if not channels:
            return []
        placeholders = ",".join("?" for _ in channels)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT m.* FROM messages m
                WHERE m.status IN ('open', 'blocked') AND m.sender = ?
                  AND m.channel IN ({placeholders})
                ORDER BY m.created_at
                """,
                (sender, *channels),
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

    # -- message attachments (0091): content-addressed channel blobs ------------

    def blob_put(self, channel: str, data: bytes, *, filename: str,
                 content_type: str, created_by: str) -> dict[str, Any]:
        """Store bytes under their sha256. Idempotent by construction: the
        same bytes in the same channel land on the same row (INSERT OR
        IGNORE), so a duplicate upload costs one hash and returns the same
        id — content addressing is also what lets the message ledger commit
        to exact file bytes."""
        blob_id = hashlib.sha256(data).hexdigest()
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO blobs"
                " (channel, id, filename, content_type, size, bytes,"
                "  created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (channel, blob_id, filename, content_type, len(data), data,
                 created_by, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT channel, id, filename, content_type, size,"
                " created_by, created_at FROM blobs"
                " WHERE channel = ? AND id = ?", (channel, blob_id),
            ).fetchone()
        return dict(row)

    def blob_channel_bytes(self, channel: str) -> int:
        """Total stored blob bytes in a channel (distinct blobs only — dedup
        means identical uploads share one row). Powers the per-channel quota
        that keeps append-only storage from growing without bound."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(size), 0) AS total FROM blobs"
                " WHERE channel = ?", (channel,),
            ).fetchone()
        return int(row["total"])

    def blob_meta(self, channel: str, blob_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT channel, id, filename, content_type, size,"
                " created_by, created_at FROM blobs"
                " WHERE channel = ? AND id = ?", (channel, blob_id),
            ).fetchone()
        return dict(row) if row else None

    def blob_get(self, channel: str, blob_id: str) -> tuple[dict[str, Any], bytes] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM blobs WHERE channel = ? AND id = ?",
                (channel, blob_id),
            ).fetchone()
        if row is None:
            return None
        meta = {k: row[k] for k in ("channel", "id", "filename", "content_type",
                                    "size", "created_by", "created_at")}
        return meta, row["bytes"]

    # -- work-id activity index (0093): the stitch between hub and files ---------

    def work_activity(self, item_id: str,
                      channels: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Everything in the CALLER'S channels citing one work id: pointer
        claims (`claim:<id>` rows), decisions (`decision:*` rows whose value
        cites the id), and messages (structured `item_ref` first, free-text
        mentions second). One query set, so the board renders 'claimed by X,
        discussed here' in ONE call instead of scraping channels. The
        channel list is the membership gate — computed by the service from
        the caller, never trusted from input."""
        if not channels:
            return {"claims": [], "decisions": [], "messages": []}
        ph = ",".join("?" * len(channels))
        needle = f"%{item_id}%"
        with self._lock:
            claims = self._conn.execute(
                f"SELECT channel, key, value, version, updated_by, updated_at"
                f" FROM store WHERE channel IN ({ph}) AND key = ?",
                (*channels, f"claim:{item_id}"),
            ).fetchall()
            decisions = self._conn.execute(
                f"SELECT channel, key, value, version, updated_by, updated_at"
                f" FROM store WHERE channel IN ({ph})"
                f" AND key LIKE 'decision:%' AND value LIKE ?",
                (*channels, needle),
            ).fetchall()
            messages = self._conn.execute(
                f"SELECT id, channel, seq, sender, status, title, body, data,"
                f" reply_to, created_at FROM messages"
                f" WHERE channel IN ({ph})"
                f" AND (data LIKE ? OR body LIKE ? OR title LIKE ?)"
                f" ORDER BY created_at",
                (*channels, needle, needle, needle),
            ).fetchall()
        out_msgs: list[dict[str, Any]] = []
        for r in messages:
            data = json.loads(r["data"]) if r["data"] else {}
            structured = isinstance(data, dict) and data.get("item_ref") == item_id
            out_msgs.append({
                "id": r["id"], "channel": r["channel"], "seq": r["seq"],
                "sender": r["sender"], "status": r["status"],
                "title": r["title"], "reply_to": r["reply_to"],
                "created_at": r["created_at"],
                "via": "item_ref" if structured else "mention",
            })
        def _row(r: Any) -> dict[str, Any]:
            return {"channel": r["channel"], "key": r["key"],
                    "value": json.loads(r["value"]), "version": r["version"],
                    "updated_by": r["updated_by"], "updated_at": r["updated_at"]}
        return {"claims": [_row(r) for r in claims],
                "decisions": [_row(r) for r in decisions],
                "messages": out_msgs}

    # -- reputation (0094): peer ±1 votes on fixed axes, one live per rater ------

    def reputation_cast(self, channel: str, target: str, rater: str,
                        axis: str, value: int, note: str) -> dict[str, Any]:
        """Upsert the rater's ONE live vote on (target, axis) in a channel.
        Revision overwrites in place (updated_at moves, created_at stays):
        a change of judgment replaces the old one on the record rather than
        stacking — the anti-ballot-stuffing property, enforced by the
        primary key rather than by policy prose."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO reputation_votes"
                " (channel, target, rater, axis, value, note,"
                "  created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(channel, target, rater, axis) DO UPDATE SET"
                " value = excluded.value, note = excluded.note,"
                " updated_at = excluded.updated_at",
                (channel, target, rater, axis, value, note, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM reputation_votes WHERE channel = ? AND"
                " target = ? AND rater = ? AND axis = ?",
                (channel, target, rater, axis),
            ).fetchone()
        return dict(row)

    def reputation_clear_rater(self, channel: str, rater: str) -> int:
        """Withdraw ALL of one rater's votes in a channel (used when the
        rater leaves — 0094 hardening F2: otherwise a rater could drive-by
        downvote then leave, and the membership gate would forever block
        both their withdrawal and the target's recourse). Votes ABOUT the
        leaver, cast by others, are untouched."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM reputation_votes WHERE channel = ? AND rater = ?",
                (channel, rater))
            self._conn.commit()
        return cur.rowcount

    def reputation_clear(self, channel: str, target: str, rater: str,
                         axis: str | None = None) -> int:
        """Withdraw the rater's live vote(s) on a target (one axis, or all
        four when axis is None). Returns rows removed."""
        with self._lock:
            if axis is None:
                cur = self._conn.execute(
                    "DELETE FROM reputation_votes WHERE channel = ? AND"
                    " target = ? AND rater = ?", (channel, target, rater))
            else:
                cur = self._conn.execute(
                    "DELETE FROM reputation_votes WHERE channel = ? AND"
                    " target = ? AND rater = ? AND axis = ?",
                    (channel, target, rater, axis))
            self._conn.commit()
        return cur.rowcount

    def reputation_totals(self, channel: str | None) -> list[dict[str, Any]]:
        """ONE unified aggregation for the whole reputation system
        (agora-0123). The operator's final rule (dm#134: "i meant the
        MECHANICS!!! 10 messages = UP TO 10 votes"), two sentences:

        You may vote each message (the CASTING mechanics — one standing
        vote per rater per message, flip/withdraw any time). The SCORE
        counts each colleague once per category: their net stance,
        collapsed to one sign — so fifty pleased thumbs from one colleague
        weigh one voice, and the measured pair-farm (30 points from 1
        rater in 4.7s) is structurally impossible.

        Same collapse in every scope; `channel=None` = hub-wide, DMs
        included (with collapse a DM adds at most one voice per colleague);
        no channel names returned. up/down are collapsed-RATER counts, so
        score = up - down per category and total = sum(categories) — the
        invariants continuum's conformance suite pins.

        Rows: {target, category, score, up, down, raters} where raters =
        distinct colleagues with any standing input in the category (a
        net-zero stance shows engagement without adding weight)."""
        scope = "WHERE channel = ?" if channel else ""
        args = (channel,) if channel else ()
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT target, category, SUM(sign) AS score,
                       SUM(CASE WHEN sign > 0 THEN 1 ELSE 0 END) AS up,
                       SUM(CASE WHEN sign < 0 THEN 1 ELSE 0 END) AS down,
                       COUNT(*) AS raters
                FROM (
                  SELECT target, category, rater,
                         CASE WHEN SUM(value) > 0 THEN 1
                              WHEN SUM(value) < 0 THEN -1 ELSE 0 END AS sign
                  FROM (
                    SELECT target, axis AS category, rater, value
                    FROM reputation_votes {scope}
                    UNION ALL
                    SELECT target, 'general' AS category, rater, value
                    FROM message_ratings {scope}
                  ) GROUP BY target, category, rater
                ) GROUP BY target, category
                """, (*args, *args)).fetchall()
        return [dict(r) for r in rows]

    def reputation_spread(self, channel: str | None = None) -> dict[str, dict[str, int]]:
        """Per-target spread within the scope: distinct channels + distinct
        raters across BOTH reputation tables (DMs included — one system,
        one rule). The raters count is the honesty signal that rides next
        to the per-action score: 40 points from 2 raters READS differently
        than 40 from 15, and serving that distinction is the anti-farming
        design under the operator's per-action ruling."""
        scope = "WHERE channel = ?" if channel else ""
        args = (channel,) if channel else ()
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT target, COUNT(DISTINCT channel) AS channels,
                       COUNT(DISTINCT rater) AS raters
                FROM (
                  SELECT target, channel, rater FROM reputation_votes {scope}
                  UNION
                  SELECT target, channel, rater FROM message_ratings {scope}
                ) GROUP BY target
                """, (*args, *args)).fetchall()
        return {r["target"]: {"channels": int(r["channels"]),
                              "raters": int(r["raters"])} for r in rows}

    # History note (agora-0123): the pre-unification aggregations
    # (reputation_channel with raw axis sums + total/axes, reputation_hub
    # with voucher collapse EXCLUDING dm:*, and the messages-fold overlay)
    # were replaced by reputation_totals/reputation_spread above — ONE
    # collapse rule for every reputation input, DMs included, per the
    # operator's dm#129 ruling ("all one and the same system... you really
    # over complexified"). The 0094 anti-farming property (one colleague =
    # one voice, enforced by collapse not policy prose) is preserved and
    # now UNIFORM: the channel board previously summed raw axis votes while
    # the hub board collapsed — that inconsistency is gone.

    def reputation_votes_for(self, channel: str,
                             target: str) -> list[dict[str, Any]]:
        """The live votes behind one target's channel score — full
        attribution (rater, axis, value, note, timestamps), newest first.
        This is the 'why did the score move' surface."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reputation_votes WHERE channel = ? AND"
                " target = ? ORDER BY updated_at DESC", (channel, target),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- message ratings (agora-0122: one reputation system) ---------------------

    def rating_cast(self, channel: str, message_id: str, rater: str,
                    target: str, value: int, note: str) -> dict[str, Any]:
        """Upsert the rater's ONE standing rating of a message (PK
        message_id+rater): re-rating replaces (flip), never stacks — the
        same anti-ballot-stuffing-by-key property as reputation_cast."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO message_ratings"
                " (channel, message_id, rater, target, value, note,"
                "  created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(message_id, rater) DO UPDATE SET"
                " value = excluded.value, note = excluded.note,"
                " updated_at = excluded.updated_at",
                (channel, message_id, rater, target, value, note, now, now))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM message_ratings WHERE message_id = ? AND rater = ?",
                (message_id, rater)).fetchone()
        return dict(row)

    def rating_clear(self, message_id: str, rater: str) -> int:
        """Withdraw the rater's rating of a message (toggle-off). Returns
        rows removed (0 = nothing standing)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM message_ratings WHERE message_id = ? AND rater = ?",
                (message_id, rater))
            self._conn.commit()
        return cur.rowcount

    def rating_clear_rater(self, channel: str, rater: str) -> int:
        """Withdraw ALL of one rater's message ratings in a channel — the
        same F2 hardening as reputation_clear_rater (leave/kick/retire must
        clear BOTH tables or the drive-by-downvote hole reopens through
        whichever door forgot)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM message_ratings WHERE channel = ? AND rater = ?",
                (channel, rater))
            self._conn.commit()
        return cur.rowcount

    def ratings_for_messages(self, message_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """All standing ratings of the given messages, grouped by message id —
        one chunked query per history page (same batching discipline as
        replies_map) so tally decorations cost O(pages), not O(rows)."""
        out: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(message_ids), 500):
            chunk = message_ids[i:i + 500]
            placeholders = ",".join("?" for _ in chunk)
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT * FROM message_ratings WHERE message_id IN"
                    f" ({placeholders})", (*chunk,)).fetchall()
            for r in rows:
                out.setdefault(r["message_id"], []).append(dict(r))
        return out

    # (rating_signs_channel / rating_signs_hub, 0122's message-ratings-only
    # folds, were absorbed into reputation_signs above — one query, one
    # collapse rule for every reputation input; agora-0123.)

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

    def list_operator_ids(self) -> list[str]:
        # Retired operators are excluded (c3436, HOLE 7): a decommissioned
        # operator must not keep minting unbounded directive debts nor keep
        # closure authority. Its id is never recycled, so this only ever
        # narrows the set.
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM agents WHERE operator = 1 "
                "AND retired_at IS NULL").fetchall()
        return [r["id"] for r in rows]

    # -- charter receipts + hub rules (governance surfaces) ----------------------

    def charter_receipt_set(self, agent_id: str, channel: str, version: int) -> None:
        """Record 'charter version N was delivered to this agent' — advance
        only (a concurrent older read can never regress a fresher receipt)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO charter_receipts (agent_id, channel, version, read_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT (agent_id, channel) DO UPDATE SET"
                " version = MAX(version, excluded.version), read_at = excluded.read_at",
                (agent_id, channel, version, time.time()),
            )
            self._conn.commit()

    def charter_receipt_get(self, agent_id: str, channel: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT version FROM charter_receipts"
                " WHERE agent_id = ? AND channel = ?", (agent_id, channel),
            ).fetchone()
        return row["version"] if row else None

    def hub_rules_get(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT text, version, updated_at FROM hub_rules WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return {"text": row["text"], "version": row["version"],
                "updated_at": row["updated_at"]}

    def hub_rules_set(self, text: str) -> dict[str, Any]:
        """Replace the operator-served hub rules; the version only ever grows
        (agents cache by version, so a rewrite must always look new)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO hub_rules (id, text, version, updated_at)"
                " VALUES (1, ?, 1, ?)"
                " ON CONFLICT (id) DO UPDATE SET"
                " text = excluded.text, version = hub_rules.version + 1,"
                " updated_at = excluded.updated_at",
                (text, time.time()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT text, version, updated_at FROM hub_rules WHERE id = 1"
            ).fetchone()
        return {"text": row["text"], "version": row["version"],
                "updated_at": row["updated_at"]}

    # -- delegation record (0068) --------------------------------------------------

    def delegation_set(self, agent_id: str, powers: list[str],
                       expires_at: float, note: str) -> dict[str, Any]:
        """Grant (or replace) an agent's delegation. Prior active grants for
        the same agent are revoked in the same transaction — one active grant
        per agent, history preserved as rows."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE delegations SET revoked_at = ? WHERE agent_id = ?"
                " AND revoked_at IS NULL", (now, agent_id))
            self._conn.execute(
                "INSERT INTO delegations (agent_id, powers, granted_at,"
                " expires_at, note) VALUES (?,?,?,?,?)",
                (agent_id, json.dumps(sorted(powers)), now, expires_at, note))
            self._conn.commit()
        return {"agent_id": agent_id, "powers": sorted(powers),
                "granted_at": now, "expires_at": expires_at, "note": note}

    def delegation_revoke(self, agent_id: str) -> bool:
        """True only when a LIVE grant was revoked: expired rows are still
        stamped (history hygiene) but do not count — revoking a dead grant
        must not announce anything (review LOW-4)."""
        now = time.time()
        with self._lock:
            live = self._conn.execute(
                "SELECT COUNT(*) AS n FROM delegations WHERE agent_id = ?"
                " AND revoked_at IS NULL AND expires_at > ?",
                (agent_id, now)).fetchone()["n"]
            self._conn.execute(
                "UPDATE delegations SET revoked_at = ? WHERE agent_id = ?"
                " AND revoked_at IS NULL", (now, agent_id))
            self._conn.commit()
        return live > 0

    def delegations_active(self) -> list[dict[str, Any]]:
        """Active grants: unrevoked and unexpired, newest per agent."""
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT agent_id, powers, granted_at, expires_at, note"
                " FROM delegations WHERE revoked_at IS NULL AND expires_at > ?"
                " ORDER BY id DESC", (now,)).fetchall()
        seen: set[str] = set()
        out = []
        for r in rows:
            if r["agent_id"] in seen:
                continue
            seen.add(r["agent_id"])
            out.append({"agent_id": r["agent_id"],
                        "powers": json.loads(r["powers"]),
                        "granted_at": r["granted_at"],
                        "expires_at": r["expires_at"], "note": r["note"]})
        return out

    # -- moderation blocks (kick/ban) -----------------------------------------------

    @staticmethod
    def _block_row(r: Any) -> dict[str, Any]:
        return {"scope": r["scope"], "agent_id": r["agent_id"],
                "imposed_by": r["imposed_by"], "imposed_at": r["imposed_at"],
                "expires_at": r["expires_at"], "reason": r["reason"]}

    def block_set(self, scope: str, agent_id: str, imposed_by: str,
                  expires_at: float | None, reason: str) -> dict[str, Any]:
        """Impose a block (kick when expires_at is set, ban when None). Any
        prior active block on the same (scope, agent) is lifted in the same
        transaction — one active block per pair, history preserved as rows."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE blocks SET lifted_at = ? WHERE scope = ?"
                " AND agent_id = ? AND lifted_at IS NULL", (now, scope, agent_id))
            self._conn.execute(
                "INSERT INTO blocks (scope, agent_id, imposed_by, imposed_at,"
                " expires_at, reason) VALUES (?,?,?,?,?,?)",
                (scope, agent_id, imposed_by, now, expires_at, reason))
            self._conn.commit()
        return {"scope": scope, "agent_id": agent_id, "imposed_by": imposed_by,
                "imposed_at": now, "expires_at": expires_at, "reason": reason}

    def block_lift(self, scope: str, agent_id: str) -> bool:
        """True only when a LIVE block was lifted — lifting an expired or
        absent block is a no-op that must not announce anything."""
        now = time.time()
        with self._lock:
            live = self._conn.execute(
                "SELECT COUNT(*) AS n FROM blocks WHERE scope = ? AND agent_id = ?"
                " AND lifted_at IS NULL AND (expires_at IS NULL OR expires_at > ?)",
                (scope, agent_id, now)).fetchone()["n"]
            self._conn.execute(
                "UPDATE blocks SET lifted_at = ? WHERE scope = ?"
                " AND agent_id = ? AND lifted_at IS NULL", (now, scope, agent_id))
            self._conn.commit()
        return live > 0

    def block_get(self, scope: str, agent_id: str) -> dict[str, Any] | None:
        """The ACTIVE block for (scope, agent), or None: newest unlifted row
        that has not expired. Expired rows stay as history but gate nothing."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT scope, agent_id, imposed_by, imposed_at, expires_at, reason"
                " FROM blocks WHERE scope = ? AND agent_id = ? AND lifted_at IS NULL"
                " AND (expires_at IS NULL OR expires_at > ?)"
                " ORDER BY id DESC LIMIT 1", (scope, agent_id, now)).fetchone()
        return None if row is None else self._block_row(row)

    def blocks_active(self, scope: str | None = None) -> list[dict[str, Any]]:
        """All active blocks (newest per scope+agent), optionally one scope."""
        now = time.time()
        cond = " AND scope = ?" if scope is not None else ""
        args: tuple[Any, ...] = (now, scope) if scope is not None else (now,)
        with self._lock:
            rows = self._conn.execute(
                "SELECT scope, agent_id, imposed_by, imposed_at, expires_at, reason"
                " FROM blocks WHERE lifted_at IS NULL"
                " AND (expires_at IS NULL OR expires_at > ?)" + cond +
                " ORDER BY id DESC", args).fetchall()
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for r in rows:
            key = (r["scope"], r["agent_id"])
            if key in seen:
                continue
            seen.add(key)
            out.append(self._block_row(r))
        return out

    # -- operator pause (0069) ----------------------------------------------------

    def pause_get(self) -> dict[str, Any] | None:
        """The ongoing pause, or None. At most one row has ended_at NULL."""
        with self._lock:
            row = self._conn.execute(
                "SELECT started_at, reason, started_by FROM hub_pauses"
                " WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {"since": row["started_at"], "reason": row["reason"],
                "by": row["started_by"]}

    def pause_start(self, reason: str, by: str) -> tuple[dict[str, Any], bool]:
        """Idempotent: starting while paused returns the existing pause.
        The `created` flag is decided under the SAME lock as the existence
        check, so two racing pause calls cannot both claim creation (and
        therefore cannot both broadcast — review LOW-5)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT started_at, reason, started_by FROM hub_pauses"
                " WHERE ended_at IS NULL").fetchone()
            if row is not None:
                return ({"since": row["started_at"], "reason": row["reason"],
                         "by": row["started_by"]}, False)
            now = time.time()
            self._conn.execute(
                "INSERT INTO hub_pauses (started_at, reason, started_by)"
                " VALUES (?,?,?)", (now, reason, by))
            self._conn.commit()
        return ({"since": now, "reason": reason, "by": by}, True)

    def pause_end(self) -> bool:
        """Idempotent: True if a pause was actually ended."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE hub_pauses SET ended_at = ? WHERE ended_at IS NULL",
                (time.time(),))
            self._conn.commit()
        return cur.rowcount > 0

    def pause_intervals(self, since: float) -> list[tuple[float, float | None]]:
        """Pause intervals overlapping [since, now] — feeds the escalation
        clock exclusion. Few rows by nature; callers overlap in Python."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT started_at, ended_at FROM hub_pauses"
                " WHERE ended_at IS NULL OR ended_at > ?", (since,)).fetchall()
        return [(r["started_at"], r["ended_at"]) for r in rows]

    def ping(self) -> bool:
        """Cheap liveness probe for /healthz."""
        with self._lock:
            self._conn.execute("SELECT 1")
        return True

    def close(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
