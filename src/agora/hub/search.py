"""Hub search executor (agora-0132, build step 3): compile-safe queries,
membership-scoped grouped reports, one snapshot per report.

Everything here was settled by the three adversary cycles (spec:
untracked/search-spec-v2.md + cycle-3 amendments):

- COMPILER: caller text never reaches FTS5 raw (column filters, NEAR,
  wildcards and unbalanced quotes all have live semantics or raise —
  measured). Terms become quote-escaped phrases; bare-punctuation tokens
  are dropped (a lone `-` matched 2,619 docs of markdown bullets). Any
  byte string compiles to valid FTS atoms or raises SearchQueryError —
  one typed 400 whose shape never depends on corpus or scope.
- BLENDED RETRIEVAL (0134, the recall adversary's measured redesign —
  recall@10 0.24 -> 0.41, recall@25 0.24 -> 0.54 on an 18-query topical
  failure set): strict-AND + a zero-hit gate behaved anti-RAG (the
  strict set exhausts; one stray file hit closed the relaxation gate).
  Now ONE grouped union query: a branch per kept term weighted by
  BM25-idf, a NEAR(a b, 10) branch per adjacent pair, soft-stop for
  terms matching >25% of the corpus (keep all if all would drop),
  GROUP BY rowid, ordered by matched-term mass then summed bm25.
  Docs matching ALL terms keep maximal mass and rank first — strict
  winners are unchanged; everything below is graceful OR fill.
  `relaxed: true` when the best hit's term mass < the full query mass.
- VOTES DIMENSION (operator dm#174): `rated=up|down|any` filters
  message hits by their standing tally; `min_votes=N` needs that many
  total votes; `sort=votes` orders the message sections by net rating.
  With `rated` set, `q` may be EMPTY (browse mode: "most downvoted
  work" without knowing its words). Default ranking stays vote-free —
  measured 0.46% of messages carry votes, and agents can rate, so
  vote-weighted default rank would be a burying surface, not a signal.
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

from ..models import elide

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
        # A term is REFUSED, never shortened: searching a prefix of what was
        # typed returns confident results for a question nobody asked, and
        # an empty result set is the one outcome a searcher does not doubt.
        if len(raw) > MAX_TERM_CHARS:
            raise SearchQueryError(
                f"the term '{elide(raw, 32)}' is {len(raw)} characters; the "
                f"cap is {MAX_TERM_CHARS}. Shorten it — searching a prefix "
                "of it would answer a different question.")
        terms.append(raw)
    # A dropped term changes the result set, so say so rather than quietly
    # answering a narrower query. Checked AFTER the loop: exactly MAX_TERMS
    # is a legal query, and the cap must not fire on the last legal one.
    if len(terms) > MAX_TERMS:
        raise SearchQueryError(
            f"a query may carry at most {MAX_TERMS} terms; this one has "
            f"{len(terms)}. Drop the ones that matter least — the hub will "
            "not choose for you.")
    if not terms:
        raise SearchQueryError("query must contain at least one word")
    return terms


def _phrase(term: str) -> str:
    """One term -> a safe FTS5 atom: a quote-escaped phrase. Under the v2
    tokenizer (no tokenchars) a hyphenated term tokenizes into adjacent
    tokens on BOTH sides, so 'agora-0132' and 'stale-claims' match their
    split forms with no expansion tricks."""
    return '"' + term.replace('"', '""') + '"'


def compile_match(terms: list[str], *, operator: str = "AND") -> str:
    joiner = " AND " if operator == "AND" else " OR "
    return joiner.join(_phrase(t) for t in terms)


def near_pair(a: str, b: str) -> str:
    """Adjacent-pair proximity branch: NEAR(a b, 10) — measured pure
    recall@25 gain at zero precision cost (fix c in the recall report)."""
    return f'NEAR({_phrase(a)} {_phrase(b)}, 10)'


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

    # -- the blended one-pass retrieval (0134) ----------------------------

    def _term_weights(self, terms: list[str]) -> list[tuple[str, float]]:
        """BM25-idf weight per term, with the soft-stop: terms matching
        >25% of the corpus carry no signal and drop from the branch set —
        unless ALL would drop (never an empty query). The absolute floor
        (df > 10) keeps the stop from inverting on tiny corpora, where a
        single occurrence exceeds 25% and the MEANINGFUL terms would drop.
        A df=0 term keeps its (maximal) idf: a query word absent from the
        corpus is exactly an unmet expectation, and the relaxed flag must
        see its weight in the full-query mass."""
        import math
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM search_docs").fetchone()["n"] or 1
        weighted: list[tuple[str, float, int]] = []
        for t in terms:
            df = self.conn.execute(
                "SELECT COUNT(*) AS d FROM search_fts WHERE search_fts MATCH ?",
                (_phrase(t),)).fetchone()["d"]
            w = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            weighted.append((t, max(w, 0.0), df))
        kept = [(t, w) for t, w, df in weighted
                if not (df > 0.25 * n and df > 10)]
        if not kept:
            kept = [(t, w) for t, w, _ in weighted]
        return kept

    def _blend(self, terms: list[str], filters: dict[str, Any],
               per: int, sort: str) -> tuple[dict[str, dict[str, Any]], bool, str]:
        """ONE grouped union pass over all kinds: term branches weighted by
        idf + adjacent NEAR-pair branches, ordered by matched-term mass
        then summed bm25. Strict-AND winners keep maximal mass and rank
        first; below them is graceful OR fill. Returns (sections, relaxed,
        or_match_for_snippets)."""
        kept = self._term_weights(terms)
        full_mass = sum(w for _, w in kept)
        branches: list[str] = []
        named: dict[str, Any] = {"caller": self.caller}
        for i, (t, w) in enumerate(kept):
            named[f"m{i}"] = _phrase(t)
            named[f"w{i}"] = w
            branches.append(
                f"SELECT rowid AS rid, bm25(search_fts) AS r, :w{i} AS w,"
                f" 1 AS is_term FROM search_fts WHERE search_fts MATCH :m{i}")
        for i in range(len(kept) - 1):
            a, wa = kept[i]
            b, wb = kept[i + 1]
            j = len(branches)
            named[f"m{j}"] = near_pair(a, b)
            named[f"w{j}"] = (wa + wb) / 2.0
            branches.append(
                f"SELECT rowid, bm25(search_fts), :w{j}, 0"
                f" FROM search_fts WHERE search_fts MATCH :m{j}")
        if len(branches) == 1:
            # Keep the compound-select shape: a lone branch gets
            # query-flattened and bm25() loses its FTS context (measured
            # OperationalError — recall report §2.1). The dummy branch
            # yields no rows and preserves the shape.
            branches.append("SELECT 0, 0.0, 0.0, 0 WHERE 0")
        union = " UNION ALL ".join(branches)

        where, extra = self._filter_sql(filters, named)
        sql = (
            "WITH grouped AS ("
            f"  SELECT rid, SUM(CASE WHEN is_term=1 THEN w ELSE 0 END) AS tmass,"
            f"         SUM(w) AS mass, SUM(r) AS rsum FROM ({union})"
            "   GROUP BY rid)"
            " SELECT sd.doc_id, sd.kind, sd.channel, sd.ref, sd.title,"
            "        sd.created_at, g.tmass, g.mass, g.rsum,"
            "        msg.seq AS seq, msg.sender AS sender, msg.status AS status,"
            "        COALESCE(rt.up,0) AS up, COALESCE(rt.down,0) AS down"
            " FROM grouped g"
            " JOIN search_docs sd ON sd.doc_id = g.rid"
            " LEFT JOIN messages msg ON sd.kind = 'message' AND msg.id = sd.ref"
            " LEFT JOIN (SELECT message_id,"
            "              SUM(CASE WHEN value>0 THEN 1 ELSE 0 END) AS up,"
            "              SUM(CASE WHEN value<0 THEN 1 ELSE 0 END) AS down"
            "            FROM message_ratings GROUP BY message_id) rt"
            "   ON sd.kind = 'message' AND rt.message_id = sd.ref"
            " WHERE (sd.kind != 'message' OR (msg.id IS NOT NULL"
            "        AND msg.retracted_at IS NULL))"
            f" AND (CASE WHEN sd.kind = 'agent' THEN {self._AGENT_SCOPE}"
            f"      ELSE {self._CH_SCOPE} END)"
            f" {where}"
            " ORDER BY g.mass DESC, g.rsum ASC"
            " LIMIT 400")
        rows = self.conn.execute(sql, named).fetchall()

        kind_filter = filters.get("kind")
        secs: dict[str, dict[str, Any]] = {
            name: {"rows": [], "total": 0} for name in SECTIONS}

        def bucket(r: sqlite3.Row) -> str | None:
            k = r["kind"]
            if k == "decision":
                return "decisions"
            if k in ("claim", "work"):
                return "work"
            if k == "agent":
                return "people"
            if k == "file":
                return "files"
            if k == "message":
                return ("open_threads" if r["status"] in ("open", "blocked")
                        else "messages")
            return None

        for r in rows:
            name = bucket(r)
            if name is None:
                continue
            if kind_filter and kind_filter not in (
                    _KINDS_FOR.get(name) or ("message",)):
                continue
            sec = secs[name]
            sec["total"] += 1
            # The FULL bucketed pool rides along for fusion (agora-0137):
            # fusing only the per-section head would strand semantic
            # rescues below lexical noise — the measured design fuses the
            # pools, then caps.
            sec.setdefault("pool", []).append(r)
            if len(sec["rows"]) < per:
                sec["rows"].append(r)

        # Display-order rulings survive the blend: structural sections
        # newest-first; message sections optionally by net votes.
        for name in _STRUCTURAL:
            secs[name]["rows"].sort(key=lambda r: -r["created_at"])
        if sort == "votes":
            # Net DESC — the /top (agora-0125) precedent: best first; the
            # worst-work lens is rated=down (+ this order within it).
            for name in ("open_threads", "messages"):
                secs[name]["rows"].sort(
                    key=lambda r: (-(r["up"] - r["down"]), -r["created_at"]))

        best_tmass = rows[0]["tmass"] if rows else 0.0
        relaxed = bool(rows) and len(kept) > 1 and \
            best_tmass < full_mass - 1e-9
        or_match = compile_match([t for t, _ in kept], operator="OR")
        return secs, relaxed, or_match

    def _fuse_semantic(self, secs: dict[str, dict[str, Any]],
                       semantic_keys: list[tuple[str, str, str]],
                       per: int, kind_filter: str | None,
                       sort: str) -> dict[str, dict[str, Any]]:
        """Per-SECTION weighted RRF of the lexical pools with the ranked
        semantic keys (agora-0137; k=60, w_sem=2 — the measured winners).
        Semantic-only docs are hydrated to the blend's row shape; display
        rulings re-apply after fusion (structural newest-first, votes)."""
        from .semantic import rrf_fuse

        hydrated = self._hydrate(semantic_keys)
        # Route each semantic key to its section (message status decides
        # open_threads vs messages — the key alone cannot).
        sem_by_section: dict[str, list[tuple[str, str, str]]] = {}
        for key in semantic_keys:
            row = hydrated.get(key)
            if row is None:
                continue                      # retracted/imposter: not served
            k = row["kind"]
            if k == "decision":
                name = "decisions"
            elif k in ("claim", "work"):
                name = "work"
            elif k == "agent":
                name = "people"
            elif k == "file":
                name = "files"
            elif k == "message":
                name = ("open_threads"
                        if row["status"] in ("open", "blocked") else "messages")
            else:
                continue
            if kind_filter and kind_filter not in (
                    _KINDS_FOR.get(name) or ("message",)):
                continue
            sem_by_section.setdefault(name, []).append(key)

        def rkey(r) -> tuple[str, str, str]:
            return (r["kind"], r["channel"] or "", r["ref"])

        for name in SECTIONS:
            sec = secs[name]
            pool = sec.get("pool", sec["rows"])
            lex_keys = [rkey(r) for r in pool]
            sem_keys = sem_by_section.get(name, [])
            if not sem_keys and not lex_keys:
                continue
            fused = rrf_fuse(lex_keys, sem_keys)
            by_key = {rkey(r): r for r in pool}
            rows = [by_key.get(k) or hydrated[k] for k in fused[:per]]
            sec["rows"] = rows
            sec["total"] = max(sec["total"], len(fused))
        for name in _STRUCTURAL:
            secs[name]["rows"].sort(key=lambda r: -r["created_at"])
        if sort == "votes":
            for name in ("open_threads", "messages"):
                secs[name]["rows"].sort(
                    key=lambda r: (-(r["up"] - r["down"]), -r["created_at"]))
        return secs

    def _hydrate(self, keys: list[tuple[str, str, str]]) -> dict[
            tuple[str, str, str], Any]:
        """The blend's row shape for semantic-only docs — same joins, same
        retraction guard, CHUNKED to stay under sqlite's bound-var
        ceiling. Membership is already enforced upstream (the visible-set
        gate before cosine); this re-checks nothing it shouldn't."""
        out: dict[tuple[str, str, str], Any] = {}
        for i in range(0, len(keys), 120):
            chunk = keys[i:i + 120]
            preds = []
            named: dict[str, Any] = {}
            for j, (kind, channel, ref) in enumerate(chunk):
                preds.append(f"(sd.kind = :k{j} AND COALESCE(sd.channel,'')"
                             f" = :c{j} AND sd.ref = :r{j})")
                named[f"k{j}"] = kind
                named[f"c{j}"] = channel
                named[f"r{j}"] = ref
            sql = (
                "SELECT sd.doc_id, sd.kind, sd.channel, sd.ref, sd.title,"
                "       sd.text, sd.created_at, 0.0 AS tmass, 0.0 AS mass,"
                "       0.0 AS rsum,"
                "       msg.seq AS seq, msg.sender AS sender,"
                "       msg.status AS status,"
                "       COALESCE(rt.up,0) AS up, COALESCE(rt.down,0) AS down"
                " FROM search_docs sd"
                " LEFT JOIN messages msg ON sd.kind = 'message'"
                "   AND msg.id = sd.ref"
                " LEFT JOIN (SELECT message_id,"
                "     SUM(CASE WHEN value>0 THEN 1 ELSE 0 END) AS up,"
                "     SUM(CASE WHEN value<0 THEN 1 ELSE 0 END) AS down"
                "   FROM message_ratings GROUP BY message_id) rt"
                "   ON sd.kind = 'message' AND rt.message_id = sd.ref"
                " WHERE (sd.kind != 'message' OR (msg.id IS NOT NULL"
                "        AND msg.retracted_at IS NULL))"
                f" AND ({' OR '.join(preds)})")
            for r in self.conn.execute(sql, named):
                out[(r["kind"], r["channel"] or "", r["ref"])] = r
        return out

    def _filter_sql(self, filters: dict[str, Any],
                    named: dict[str, Any]) -> tuple[str, None]:
        """Shared caller-filter fragment for the blend query (channels,
        sender, since/until, ref, rated, min_votes)."""
        parts: list[str] = []
        if filters.get("channels"):
            keys = []
            for i, ch in enumerate(filters["channels"]):
                named[f"fc{i}"] = ch
                keys.append(f":fc{i}")
            parts.append(f"AND sd.channel IN ({','.join(keys)})")
        if filters.get("sender"):
            named["fsender"] = filters["sender"]
            parts.append("AND (sd.kind != 'message' OR msg.sender = :fsender)")
        if filters.get("since") is not None:
            named["fsince"] = filters["since"]
            parts.append("AND sd.created_at >= :fsince")
        if filters.get("until") is not None:
            named["funtil"] = filters["until"]
            parts.append("AND sd.created_at <= :funtil")
        if filters.get("ref"):
            named["frefpat"] = f"%{filters['ref']}%"
            parts.append("AND (sd.ref LIKE :frefpat OR sd.title LIKE :frefpat"
                         " OR sd.text LIKE :frefpat)")
        rated = filters.get("rated")
        if rated == "up":
            parts.append("AND COALESCE(rt.up,0) > 0")
        elif rated == "down":
            parts.append("AND COALESCE(rt.down,0) > 0")
        elif rated == "any":
            parts.append("AND (COALESCE(rt.up,0) + COALESCE(rt.down,0)) > 0")
        if filters.get("min_votes"):
            named["fmv"] = int(filters["min_votes"])
            parts.append("AND (COALESCE(rt.up,0) + COALESCE(rt.down,0)) >= :fmv")
        return " ".join(parts), None

    def _browse(self, filters: dict[str, Any], per: int,
                sort: str) -> dict[str, dict[str, Any]]:
        """No-query browse mode (rated filter required by the service):
        'most downvoted work' without knowing its words. Message kinds
        only — ratings exist nowhere else."""
        named: dict[str, Any] = {"caller": self.caller}
        where, _ = self._filter_sql(filters, named)
        order = ("(COALESCE(rt.up,0) - COALESCE(rt.down,0)) DESC,"
                 " sd.created_at DESC" if sort == "votes"
                 else "sd.created_at DESC")
        sql = (
            "SELECT sd.doc_id, sd.kind, sd.channel, sd.ref, sd.title,"
            "       sd.created_at, msg.seq AS seq, msg.sender AS sender,"
            "       msg.status AS status,"
            "       COALESCE(rt.up,0) AS up, COALESCE(rt.down,0) AS down"
            " FROM search_docs sd"
            " JOIN messages msg ON msg.id = sd.ref AND msg.retracted_at IS NULL"
            " LEFT JOIN (SELECT message_id,"
            "              SUM(CASE WHEN value>0 THEN 1 ELSE 0 END) AS up,"
            "              SUM(CASE WHEN value<0 THEN 1 ELSE 0 END) AS down"
            "            FROM message_ratings GROUP BY message_id) rt"
            "   ON rt.message_id = sd.ref"
            f" WHERE sd.kind = 'message' AND {self._CH_SCOPE}"
            f" {where}"
            f" ORDER BY {order}"
            " LIMIT 400")
        rows = self.conn.execute(sql, named).fetchall()
        secs: dict[str, dict[str, Any]] = {
            name: {"rows": [], "total": 0} for name in SECTIONS}
        for r in rows:
            name = ("open_threads" if r["status"] in ("open", "blocked")
                    else "messages")
            secs[name]["total"] += 1
            if len(secs[name]["rows"]) < per:
                secs[name]["rows"].append(r)
        return secs

    def run(self, terms: list[str], filters: dict[str, Any], *,
            sort: str = "relevance", limit: int = DEFAULT_PER_SECTION,
            cursor: str | None = None,
            semantic_keys: list[tuple[str, str, str]] | None = None,
            semantic_only: bool = False) -> dict[str, Any]:
        """The grouped report as plain dicts (typed models wrap in the
        service). Three paths: blended one-pass (relevance, the default),
        per-section recent (keyset-pageable), and no-query browse.

        `semantic_keys` (agora-0137): a ranked (kind, channel, ref) list
        already membership- and hash-gated by the caller; fused per
        SECTION with the lexical pools (weighted RRF — global fusion
        evicted 26/61 work rows, measured). `semantic_only` serves the
        explicit mode=semantic override: same machinery, empty lexical
        side. Fusion applies to the blend path only; sort=recent and
        browse stay lexical by ruling."""
        per = min(limit, MAX_LIMIT)
        kind_filter = filters.get("kind")
        relaxed = False
        or_match: str | None = None

        if not terms and not semantic_only:
            secs = self._browse(filters, per, sort)
        elif sort == "relevance" or sort == "votes":
            if semantic_only:
                secs = {name: {"rows": [], "total": 0} for name in SECTIONS}
            else:
                secs, relaxed, or_match = self._blend(terms, filters, per, sort)
            if semantic_keys:
                secs = self._fuse_semantic(secs, semantic_keys, per,
                                           kind_filter, sort)
        else:
            # sort=recent: the per-section path (keyset cursor contract).
            match = compile_match(terms, operator="AND")
            or_match = match
            anchor = None
            if cursor:
                decoded = decode_cursor(cursor)
                # Malformed/foreign cursor = start over (never an oracle).
                if decoded and decoded["k"] == kind_filter:
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
            secs = {}
            for name, kinds, kw in spec:
                if kind_filter and kind_filter not in kinds:
                    secs[name] = {"rows": [], "total": 0}
                    continue
                rows = self._hits(match, kinds, filters=filters,
                                  order="recent", quota=per, anchor=anchor,
                                  **kw)
                total = self._count(match, kinds, filters=filters, **kw)
                secs[name] = {"rows": rows, "total": total}

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

        # Snippets: two-phase, winners only. Browse mode has no matched
        # terms — hits ride on titles alone there.
        all_ids = [r["doc_id"] for s in secs.values() for r in s["rows"]]
        snips = self._snippets(or_match, all_ids) if or_match else {}

        report: dict[str, Any] = {"relaxed": relaxed, "sections": {},
                                  "computed_at": time.time()}
        for name in SECTIONS:
            rows = secs[name]["rows"]
            hits = []
            for r in rows:
                snippet, highlights = snips.get(r["doc_id"], ("", []))
                if not snippet and "text" in r.keys():
                    # Semantic-only hit (agora-0137): no FTS offsets exist —
                    # serve the doc's head, plain, NO fake highlights
                    # (retrieval P3's snippet rule).
                    head = (r["text"] or "").strip().replace("\n", " ")
                    snippet = head[:160]
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
