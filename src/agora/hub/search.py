"""Hub search executor (agora-0132, build step 3): compile-safe queries,
membership-scoped grouped reports, one snapshot per report.

Everything here was settled by the three adversary cycles (spec:
untracked/search-spec-v2.md + cycle-3 amendments):

- COMPILER: caller text never reaches FTS5 raw (column filters, NEAR,
  wildcards and unbalanced quotes all have live semantics or raise —
  measured). Terms become quote-escaped phrases joined by implicit AND;
  bare-punctuation tokens are dropped (a lone `-` matched 2,619 docs of
  markdown bullets); hyphenated terms expand to OR with their split
  phrase so "thumbs down" finds "thumbs-down" (9/11 docs missed
  otherwise). Any byte string compiles to a valid MATCH or raises
  SearchQueryError — one typed 400 whose shape never depends on corpus
  or scope.
- ZERO-HIT RELAXATION (F1): strict AND returns 0 for natural questions
  ("who broke the reputation score" = 0 hits while "reputation bug" =
  15, measured). On a zero-hit strict pass the executor re-runs the same
  terms joined by OR and the report carries relaxed=true — loud, never
  silent.
- ACL: one membership JOIN inside ONE read-snapshot transaction per
  report (R1). Channel-bearing kinds join `members` for the caller;
  kind=agent joins the live roster. Non-member channels contribute
  NOTHING — no rows, no counts (filtering to one behaves exactly like
  filtering to a nonexistent one).
- SECTIONS: six, fixed, always served — decisions, open_threads, work,
  people, files, messages. Structural sections (decisions, open_threads,
  work, files) order newest-first (stale-decision defusal +
  term-stuffing immunity); bm25 order only in messages/people, and bm25
  SCORES never leave the hub (measured cross-tenant side channel).
- THREAD COLLAPSE: message hits collapse to one row per thread root
  with a hit count (the digest property; a busy thread must not flood
  its section).
- SNIPPETS: two-phase (rank first, snippet only winners — snippet() runs
  per match row); served as plain text + code-point highlight offsets
  (sentinel markers are stripped server-side; author bytes were already
  stripped at ingest).
"""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from typing import Any

MAX_QUERY_CHARS = 256
MAX_TERMS = 8
MAX_TERM_CHARS = 64
DEFAULT_PER_SECTION = 10
MAX_LIMIT = 50
MAX_REPORT_BYTES = 32 * 1024
SNIPPET_TOKENS = 12

SECTIONS = ("decisions", "open_threads", "work", "people", "files", "messages")
_KINDS_FOR = {
    "decisions": ("decision",),
    "work": ("claim", "work"),
    "people": ("agent",),
    "files": ("file",),
    # open_threads and messages both draw from kind=message; the split is
    # by live obligation status at query time.
}
_STRUCTURAL = {"decisions", "open_threads", "work", "files"}


class SearchQueryError(ValueError):
    """Raised for uncompilable/over-budget queries — maps to ONE typed 400
    whose text never varies with corpus or membership."""


def compile_terms(q: str) -> list[str]:
    """Caller text -> bare terms (validated, capped). Raises SearchQueryError."""
    if q is None or len(q) > MAX_QUERY_CHARS:
        raise SearchQueryError("query must be 1..256 characters")
    terms: list[str] = []
    for raw in q.split():
        # Drop tokens with no indexable characters (F2: bare punctuation
        # like `-` or `:` is a real token in the tokenizer's eyes and
        # matches thousands of markdown bullets).
        if not any(c.isalnum() for c in raw):
            continue
        terms.append(raw[:MAX_TERM_CHARS])
        if len(terms) >= MAX_TERMS:
            break
    if not terms:
        raise SearchQueryError("query must contain at least one word")
    return terms


def _phrase(term: str) -> str:
    """One term -> a safe FTS5 atom. Quote-escaped phrase; hyphen/underscore
    terms additionally OR their split-phrase form (F3)."""
    quoted = '"' + term.replace('"', '""') + '"'
    parts = [p for p in term.replace("_", "-").split("-") if p]
    if len(parts) > 1:
        split_phrase = '"' + " ".join(p.replace('"', '""') for p in parts) + '"'
        return f"({quoted} OR {split_phrase})"
    return quoted


def compile_match(terms: list[str], *, operator: str = "AND") -> str:
    joiner = " AND " if operator == "AND" else " OR "
    return joiner.join(_phrase(t) for t in terms)


def encode_cursor(kind: str, created_at: float, doc_id: int) -> str:
    """Opaque keyset cursor for sort=recent single-kind pages. UNSIGNED by
    ruling: every page re-runs the membership join at its own snapshot, so
    a forged cursor can only page within what the caller may see anyway."""
    raw = json.dumps({"k": kind, "c": created_at, "d": doc_id},
                     separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> dict[str, Any] | None:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return {"k": str(raw["k"]), "c": float(raw["c"]), "d": int(raw["d"])}
    except Exception:
        return None  # malformed cursor = start over, never an error oracle


def _strip_sentinels(marked: str) -> tuple[str, list[list[int]]]:
    """Sentinel-marked snippet -> (plain text, [[start, len], ...]) with
    code-point offsets into the SERVED string (2B H7: offsets computed on
    the final wire string; sentinels never leave the hub)."""
    out: list[str] = []
    highlights: list[list[int]] = []
    start: int | None = None
    pos = 0
    for ch in marked:
        if ch == "\u0001":
            start = pos
        elif ch == "\u0002":
            if start is not None:
                highlights.append([start, pos - start])
                start = None
        else:
            out.append(ch)
            pos += 1
    return "".join(out), highlights


class SearchExecutor:
    """Runs one grouped report inside one caller-provided read snapshot.

    The connection is a read-only pool member (or the writer under its
    lock on :memory:); this class never writes and never commits."""

    def __init__(self, conn: sqlite3.Connection, caller_id: str) -> None:
        self.conn = conn
        self.caller = caller_id

    # -- scope fragments -------------------------------------------------
    # Channel-bearing docs: EXISTS against members for THIS caller — the
    # always-fresh ACL, evaluated inside the report's snapshot.
    _CH_SCOPE = ("EXISTS (SELECT 1 FROM members mm WHERE mm.channel = sd.channel"
                 " AND mm.agent_id = :caller)")
    _AGENT_SCOPE = ("EXISTS (SELECT 1 FROM agents ag WHERE ag.id = sd.ref"
                    " AND ag.retired_at IS NULL AND ag.deleted_at IS NULL)")

    def _hits(self, match: str, kinds: tuple[str, ...], *,
              statuses: tuple[str, ...] | None = None,
              exclude_statuses: tuple[str, ...] | None = None,
              filters: dict[str, Any], order: str, quota: int,
              anchor: dict[str, Any] | None = None) -> list[sqlite3.Row]:
        agent_kind = kinds == ("agent",)
        scope = self._AGENT_SCOPE if agent_kind else self._CH_SCOPE
        kp = ",".join("?" for _ in kinds)
        args: list[Any] = list(kinds)
        named: dict[str, Any] = {"match": match, "caller": self.caller}
        join_msg = bool(statuses or exclude_statuses or
                        (filters.get("sender") and "message" in kinds))
        sql = [
            "SELECT sd.doc_id, sd.kind, sd.channel, sd.ref, sd.title,"
            " sd.created_at, bm25(search_fts) AS rank"
            + (", msg.seq AS seq, msg.sender AS sender, msg.status AS status,"
               " msg.reply_to AS reply_to" if join_msg else ""),
            "FROM search_fts",
            "JOIN search_docs sd ON sd.doc_id = search_fts.rowid",
        ]
        if join_msg:
            sql.append("JOIN messages msg ON msg.id = sd.ref"
                       " AND msg.retracted_at IS NULL")
        sql.append(f"WHERE search_fts MATCH :match AND sd.kind IN ({kp})"
                   f" AND {scope}")
        if statuses:
            sp = ",".join("?" for _ in statuses)
            sql.append(f"AND msg.status IN ({sp})")
            args.extend(statuses)
        if exclude_statuses:
            sp = ",".join("?" for _ in exclude_statuses)
            sql.append(f"AND msg.status NOT IN ({sp})")
            args.extend(exclude_statuses)
        if filters.get("channels"):
            cp = ",".join("?" for _ in filters["channels"])
            sql.append(f"AND sd.channel IN ({cp})")
            args.extend(filters["channels"])
        if filters.get("sender") and join_msg:
            sql.append("AND msg.sender = :sender")
            named["sender"] = filters["sender"]
        if filters.get("since") is not None:
            sql.append("AND sd.created_at >= :since")
            named["since"] = filters["since"]
        if filters.get("until") is not None:
            sql.append("AND sd.created_at <= :until")
            named["until"] = filters["until"]
        if filters.get("ref"):
            sql.append("AND (sd.ref LIKE :refpat OR sd.title LIKE :refpat"
                       " OR sd.text LIKE :refpat)")
            named["refpat"] = f"%{filters['ref']}%"
        if anchor is not None:
            sql.append("AND (sd.created_at < :a_c OR"
                       " (sd.created_at = :a_c AND sd.doc_id < :a_d))")
            named["a_c"] = anchor["c"]
            named["a_d"] = anchor["d"]
        sql.append("ORDER BY " + ("sd.created_at DESC, sd.doc_id DESC"
                                  if order == "recent"
                                  else "rank, sd.doc_id DESC"))
        sql.append("LIMIT :quota")
        named["quota"] = quota
        # sqlite named+positional mix: use named-only by folding positionals.
        text = "\n".join(sql)
        for i, a in enumerate(args):
            named[f"p{i}"] = a
        for i in range(len(args)):
            text = text.replace("?", f":p{i}", 1)
        return self.conn.execute(text, named).fetchall()

    def _count(self, match: str, kinds: tuple[str, ...], *,
               statuses: tuple[str, ...] | None = None,
               exclude_statuses: tuple[str, ...] | None = None,
               filters: dict[str, Any]) -> int:
        rows = self._hits(match, kinds, statuses=statuses,
                          exclude_statuses=exclude_statuses, filters=filters,
                          order="recent", quota=1_000_000)
        return len(rows)

    def _thread_root(self, msg_id: str) -> str:
        row = self.conn.execute(
            "WITH RECURSIVE up(id, parent) AS ("
            "  SELECT id, reply_to FROM messages WHERE id = :m"
            "  UNION ALL"
            "  SELECT m.id, m.reply_to FROM messages m JOIN up ON m.id = up.parent"
            ") SELECT id FROM up WHERE parent IS NULL LIMIT 1",
            {"m": msg_id}).fetchone()
        return row["id"] if row else msg_id

    def _snippets(self, match: str, doc_ids: list[int]) -> dict[int, tuple[str, list[list[int]]]]:
        if not doc_ids:
            return {}
        dp = ",".join(str(int(d)) for d in doc_ids)
        out: dict[int, tuple[str, list[list[int]]]] = {}
        for r in self.conn.execute(
                f"SELECT rowid, snippet(search_fts, -1, char(1), char(2), '…',"
                f" {SNIPPET_TOKENS}) AS snip"
                f" FROM search_fts WHERE rowid IN ({dp})"
                f" AND search_fts MATCH :match", {"match": match}):
            out[r["rowid"]] = _strip_sentinels(r["snip"])
        return out

    def run(self, terms: list[str], filters: dict[str, Any], *,
            sort: str = "relevance", limit: int = DEFAULT_PER_SECTION,
            cursor: str | None = None) -> dict[str, Any]:
        """The grouped report as plain dicts (typed models wrap in step 4)."""
        match = compile_match(terms, operator="AND")
        relaxed = False

        kind_filter = filters.get("kind")

        def sections_for(m: str) -> dict[str, dict[str, Any]]:
            per = min(limit, MAX_LIMIT)
            secs: dict[str, dict[str, Any]] = {}
            anchor = None
            if cursor:
                decoded = decode_cursor(cursor)
                # Malformed/foreign cursor = start over (never an oracle).
                if decoded and decoded["k"] == kind_filter and sort == "recent":
                    anchor = decoded
            spec: list[tuple[str, tuple[str, ...], dict[str, Any]]] = [
                ("decisions", ("decision",), {}),
                ("open_threads", ("message",),
                 {"statuses": ("open", "blocked")}),
                ("work", ("claim", "work"), {}),
                ("people", ("agent",), {}),
                ("files", ("file",), {}),
                ("messages", ("message",),
                 {"exclude_statuses": ("open", "blocked")}),
            ]
            for name, kinds, kw in spec:
                # kind filter gates WHICH sections carry content; the six
                # sections are always served (fixed shape, 3A ruling).
                if kind_filter and kind_filter not in kinds:
                    secs[name] = {"rows": [], "total": 0}
                    continue
                order = ("recent" if (name in _STRUCTURAL or sort == "recent")
                         else "relevance")
                rows = self._hits(m, kinds, filters=filters, order=order,
                                  quota=per, anchor=anchor, **kw)
                total = self._count(m, kinds, filters=filters, **kw)
                secs[name] = {"rows": rows, "total": total}
            return secs

        secs = sections_for(match)
        if all(s["total"] == 0 for s in secs.values()) and len(terms) > 1:
            # F1: strict AND found nothing anywhere — relax to OR, loudly.
            match = compile_match(terms, operator="OR")
            secs = sections_for(match)
            relaxed = True

        # Thread collapse (messages section only): one row per root.
        msg_rows = secs["messages"]["rows"]
        collapsed: list[Any] = []
        counts: dict[str, int] = {}
        root_of: dict[str, str] = {}
        for r in msg_rows:
            root = self._thread_root(r["ref"])
            root_of[r["ref"]] = root
            if root in counts:
                counts[root] += 1
            else:
                counts[root] = 1
                collapsed.append(r)
        secs["messages"]["rows"] = collapsed

        # Snippets: two-phase, winners only.
        all_ids = [r["doc_id"] for s in secs.values() for r in s["rows"]]
        snips = self._snippets(match, all_ids)

        report: dict[str, Any] = {"relaxed": relaxed, "sections": {},
                                  "computed_at": time.time()}
        for name in SECTIONS:
            rows = secs[name]["rows"]
            hits = []
            for r in rows:
                snippet, highlights = snips.get(r["doc_id"], ("", []))
                hit = {
                    "kind": r["kind"], "channel": r["channel"], "ref": r["ref"],
                    "title": r["title"], "created_at": r["created_at"],
                    "snippet": snippet, "highlights": highlights,
                }
                keys = r.keys()
                if "seq" in keys:
                    hit["seq"] = r["seq"]
                    hit["sender"] = r["sender"]
                    hit["status"] = r["status"]
                if r["kind"] == "message":
                    root = root_of.get(r["ref"], r["ref"])
                    if counts.get(root, 1) > 1:
                        hit["thread_hits"] = counts[root]
                hits.append(hit)
            report["sections"][name] = {
                "hits": hits, "shown": len(hits), "total": secs[name]["total"]}

        # Keyset cursor: recent mode + exactly one kind section requested.
        report["next_cursor"] = None
        if sort == "recent" and filters.get("kind"):
            kind = filters["kind"]
            sec_name = next((n for n, ks in _KINDS_FOR.items() if kind in ks),
                            "messages" if kind == "message" else None)
            if sec_name:
                rows = secs[sec_name]["rows"]
                if rows and len(rows) >= min(limit, MAX_LIMIT):
                    last = rows[-1]
                    report["next_cursor"] = encode_cursor(
                        kind, last["created_at"], last["doc_id"])
        return report
