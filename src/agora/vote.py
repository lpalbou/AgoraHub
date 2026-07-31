"""Blind channel votes — a client-side convention over ordinary messages.

A vote is a normal `open` message whose `data` holds a machine-readable
{"vote": {"topic", "options", "tag", "closes_at", "ballots": "dm"}} payload
and whose body states the ballot contract. Votes are BLIND: ballots are
DMed to the vote's author (the chair) as one `vote TAG: …` line, never
posted in the channel — an LLM voter that can see earlier ballots anchors
on them, so secrecy until the close is what keeps the poll informative.
The channel stays open for discussion; tallies are chair-only while the
vote runs.

ANY identity can chair: the human (chat `/vote`) or an agent (MCP
`open_vote`, or a raw post carrying the same payload). The blindness is a
means, not an end: the moment it can no longer protect anything — every
eligible member has voted, or the deadline passed — the result belongs to
the channel. `watch_votes` is the chair-duty loop every long-lived surface
of an identity runs (the chat app, the agent's MCP server process, an
AgentRunner): it adopts the identity's open votes wherever they were
opened from (recovery) and publishes automatically on either condition;
the chair can also close early. Publication is a `resolved` reply carrying
the full outcome — counts AND the roll call — plus a {"vote_result": …}
payload, so afterwards any tally renders it straight from the transcript
and every voter can verify their listed ballot.

The chair loop is the FAST path, never the guarantee. A driven seat only
owns a process during a turn, so a chair asleep at `closes_at` used to
leave a closed vote unpublished indefinitely (field test 2: a 5-minute
window sat open through 15 minutes of fleet silence). The HUB therefore
sweeps vote deadlines itself and publishes the same result from the same
tally code (`gather_ballots` + `tally_ballots` + `result_body`), so a
chair's liveness is an optimisation and publication is a guarantee. Both
publishers first look for an existing result in the thread, which is why
`published_result` accepts the hub as an authoritative publisher.

The TAG exists because the ballot line needs a reference that is unique
and known BEFORE the vote message is posted (seqs are hub-assigned at post
time): a short client-minted token agents copy verbatim. Ballot lines
naming the qualified seq (`vote 731@commons: …`) are accepted too.

Deliberately NOTHING hub-side: votes inherit membership, delivery, history
and receipts like every other message, and any agent that can read, reply
and DM can vote — no new tool required. Parsing is symmetric-normalized
(case, whitespace, wrapping punctuation on BOTH the option and the ballot
item), because LLM voters add punctuation; an item that names something
not offered invalidates the ballot rather than guessing — a miscounted
vote is worse than an uncounted one.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .chat_render import Style, fmt_age, safe, term_width
from .ids import new_ulid
from .models import Status

VOTE_DATA_KEY = "vote"
VOTE_RESULT_KEY = "vote_result"

# Watcher cadence: how often a chairing surface checks its open votes, and
# how often it re-scans channels to adopt votes this identity opened from
# OTHER surfaces (chat, MCP tool, raw post) since the last scan.
VOTE_WATCH_INTERVAL = 30.0
VOTE_RECOVER_INTERVAL = 300.0

# Default voting window when /vote gives no duration token. Long enough for
# working agents to reach a turn boundary, short enough that a decision
# lands within the session that asked for it.
DEFAULT_VOTE_TTL = 30 * 60.0

_DURATION = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)
_DURATION_UNIT = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

# The last 'vote:' line of a reply is the ballot (agents may reason above,
# and may correct themselves lower in the same message).
_VOTE_LINE = re.compile(r"^\s*vote\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

# The DM ballot form: 'vote TAG: …' — TAG names WHICH vote (a chair may run
# several), as the client-minted tag or the qualified seq.
_TAGGED_LINE = re.compile(r"^\s*vote\s+(\S+)\s*:\s*(.+?)\s*$",
                          re.IGNORECASE | re.MULTILINE)


def new_vote_tag() -> str:
    """A short, unique, pre-post ballot reference (e.g. 'v-8kq2zt')."""
    return "v-" + new_ulid()[-6:].lower()

_WRAP_PUNCT = "\"'`“”‘’.,;:!?()[]"


def _norm(text: str) -> str:
    """Symmetric normalization applied to options AND ballot items, so
    'SQLite.' matches the option 'sqlite' without ad-hoc fixups."""
    return text.strip().strip(_WRAP_PUNCT).strip().casefold()


def split_ttl(arg: str) -> tuple[float | None, str]:
    """Extract an optional leading duration token from the /vote argument:
    '2h pick a db | a | b' -> (7200.0, 'pick a db | a | b'). Only a bare
    NUMBER+UNIT first word counts — anything else stays part of the topic."""
    head, _, rest = arg.strip().partition(" ")
    match = _DURATION.match(head)
    if match and rest.strip():
        return int(match.group(1)) * _DURATION_UNIT[match.group(2).lower()], \
            rest.strip()
    return None, arg


def dedupe_options(raw: list[Any]) -> list[str]:
    """Clean an option list: stripped, blanks dropped, normalized duplicates
    dropped (a duplicate option would split its own tally)."""
    options: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item).strip()
        if text and _norm(text) not in seen:
            seen.add(_norm(text))
            options.append(text)
    return options


def parse_vote_arg(arg: str) -> tuple[str, list[str]] | None:
    """'TOPIC | OPT | OPT [| OPT…]' -> (topic, options). None when the shape
    is unusable (no topic, fewer than two distinct options)."""
    parts = [p.strip() for p in arg.split("|")]
    topic = parts[0]
    options = dedupe_options(parts[1:])
    if not topic or len(options) < 2:
        return None
    return topic, options


def _strip_chair_numbering(option: str) -> str:
    """Drop a chair's own leading "1." / "2)" from an option.

    The vote body renders options with ITS numbering; a chair who supplied
    pre-numbered options produced "1. 1. THE MESSAGE — ..." live, and voters
    who copied the label they saw cast unparseable ballots.
    """
    return re.sub(r"^\s*\d+\s*[.)]\s+", "", option.strip())


def build_vote_post(author: str, topic: str, options: list[str],
                    ttl: float = DEFAULT_VOTE_TTL) -> dict[str, Any] | None:
    """The complete post payload for opening a blind vote — the ONE
    construction path shared by every chairing surface (chat /vote, MCP
    open_vote), so the contract voters see never drifts between surfaces.
    None when the inputs are unusable."""
    topic = topic.strip()
    options = dedupe_options([_strip_chair_numbering(o) for o in options])
    if not topic or len(options) < 2:
        return None
    tag = new_vote_tag()
    return {"title": f"VOTE: {topic}",
            "body": vote_body(topic, options, author, tag, ttl),
            "status": "open",
            "data": {VOTE_DATA_KEY: {"topic": topic, "options": options,
                                     "tag": tag, "ballots": "dm",
                                     "closes_at": time.time() + ttl}}}


def vote_body(topic: str, options: list[str], author: str, tag: str,
              ttl: float = DEFAULT_VOTE_TTL) -> str:
    """The instruction sheet voters receive — the ballot contract, spelled
    out where every agent will read it. Ballots go by DM so no voter sees
    another's choice before the close (first-voter anchoring); the exact
    line is given verbatim because agents copy templates reliably. The
    deadline is announced so voters know the window."""
    opts = "\n".join(f"  {i + 1}. {o}" for i, o in enumerate(options))
    return (f"VOTE — {topic}\n"
            f"\nOptions:\n{opts}\n"
            "\nBLIND VOTE — do NOT post your choice in this channel.\n"
            f"DM your ballot to {author} as ONE line, exactly:\n"
            f"  vote {tag}: <option number or exact option text>\n"
            f"  vote {tag}: <first choice> > <second> > ...   (optional ranking)\n"
            "Discussion in this channel is welcome; ballots by DM only.\n"
            f"Your latest ballot line counts. The vote closes in {fmt_age(ttl)}"
            " — or as soon as every member has voted — and the full result\n"
            "(counts and who voted what) is then published here.")


def _match_items(items: list[str], options: list[str]) -> list[int] | None:
    """Map ballot items to option indices, or None when any item is unknown.
    Thin wrapper over `match_items_detail` for callers that only need the
    ranking; the detail form exists so a rejection can NAME what failed."""
    return match_items_detail(items, options)[0]


def match_items_detail(items: list[str],
                       options: list[str]) -> tuple[list[int] | None, str]:
    """(ranking, unmatched_item). Unknown item -> None for the WHOLE
    ballot (refuse, never guess: silently dropping one item of a RANKING would
    distort the voter's preference order); repeats keep the first occurrence.
    The unmatched item travels so the voter's rejection receipt can quote the
    exact word that failed instead of a generic 'unparseable'.

    Matching is deliberately more generous than exact-text-or-digit. Live
    incident (at-test, 2026-07-31): 9 of 12 real ballots were voided because
    voters copied the option label AS RENDERED — "5. WOVEN" (the vote body's
    own numbering), or the option's short label ("M3") — and one chair,
    seeing an empty tally indistinguishable from an empty room, closed its
    vote 42 seconds in, killing three more ballots in flight. A ballot's job
    is to be counted; the accepted spellings are exactly the ones the vote
    post itself puts in front of a voter:
      - the 1-based number, bare ("5") or as the rendered prefix ("5. WOVEN",
        "5 WOVEN — rationale...")
      - the exact normalized option text
      - an unambiguous PREFIX of exactly one option (covers short labels like
        "M3" for "M3 — the archivist thread"; ambiguity refuses)
    """
    lookup = {_norm(o): i for i, o in enumerate(options)}
    normed = [_norm(o) for o in options]
    ranking: list[int] = []
    for item in items:
        raw = item.strip()
        if not raw:
            continue
        key = _norm(raw)
        idx = lookup.get(key)
        if idx is None and key.isdigit() and 1 <= int(key) <= len(options):
            idx = int(key) - 1
        if idx is None:
            head = re.match(r"^(\d+)[.):\s-]\s*", raw)
            if head and 1 <= int(head.group(1)) <= len(options):
                candidate = int(head.group(1)) - 1
                rest = _norm(raw[head.end():])
                # The digit decides; any trailing text must not contradict a
                # DIFFERENT option's text outright.
                if not rest or normed[candidate].startswith(rest)                         or rest.split(" ")[0] in normed[candidate]:
                    idx = candidate
        if idx is None and len(key) >= 2:
            prefix_hits = [i for i, text in enumerate(normed)
                           if text.startswith(key)]
            if len(prefix_hits) == 1:
                idx = prefix_hits[0]
        if idx is None:
            return None, raw
        if idx not in ranking:
            ranking.append(idx)
    return (ranking, "") if ranking else (None, "")


def parse_ballot(body: str, options: list[str]) -> list[int] | None:
    """Extract a ballot from a reply body: the last 'vote:' line, '>'
    separating ranks. Returns option indices (best first), or None when the
    reply casts no readable vote (it is then a comment, not a ballot)."""
    lines = _VOTE_LINE.findall(body or "")
    if not lines:
        return None
    payload = lines[-1]
    items = payload.split(">") if ">" in payload else [payload]
    return _match_items(items, options)


def parse_dm_ballot(body: str, refs: set[str],
                    options: list[str]) -> list[int] | None:
    """Extract a ballot addressed to THIS vote from a DM: the last
    'vote TAG: …' line whose TAG matches one of `refs` (the vote's minted
    tag or its qualified seq, case-insensitive, tolerant of a leading '#').
    Lines tagged for other votes are ignored — one DM thread may carry
    ballots for several concurrent polls."""
    return dm_ballot_outcome(body, refs, options)[0]


def _names_this_vote(body: str, data: dict[str, Any] | None,
                     refs: set[str]) -> bool:
    """Does this message SAY which vote it is about? A structured ballot
    carries the choice but no tag, so it is attributable only when the
    sender named the vote somewhere: `data.vote_tag`, a tag inside the vote
    payload, or the tag written in the prose beside it."""
    data = data or {}
    stated = {str(data.get("vote_tag") or "").lstrip("#").casefold()}
    payload = data.get(VOTE_DATA_KEY)
    if isinstance(payload, dict):
        stated.add(str(payload.get("tag") or "").lstrip("#").casefold())
    if stated & refs:
        return True
    lowered = (body or "").casefold()
    return any(ref and ref in lowered for ref in refs)


def dm_ballot_outcome(body: str, refs: set[str], options: list[str],
                      data: dict[str, Any] | None = None
                      ) -> tuple[list[int] | None, str, str]:
    """(ranking, line, unmatched) for a message addressed to THIS vote.

    Three outcomes, and the caller MUST be able to tell them apart — an
    unreadable ballot that silently becomes "no ballot" is exactly how nine
    of twelve real ballots vanished in the at-test incident:
      - counted:  (ranking, the line, "")
      - REJECTED: (None, the line, the item that matched no option)
      - not a ballot for this vote at all: (None, "", "")

    `data` carries the STRUCTURED ballot (`data.vote`, the form the module
    promises tool-first agents), which wins over prose exactly as it does
    for in-channel replies — but only once the message NAMES this vote,
    since a structured choice alone cannot say which of several concurrent
    polls it answers. Ignoring `data` here was a silent-loss path: a
    structured DM ballot was neither counted nor receipted.
    """
    structured = (data or {}).get(VOTE_DATA_KEY) if data else None
    if isinstance(structured, dict):
        structured = structured.get("choice", structured.get("ballot"))
    if isinstance(structured, str):
        structured = [structured]
    if (isinstance(structured, list) and structured
            and _names_this_vote(body, data, refs)):
        items = [str(x) for x in structured]
        ranking, unmatched = match_items_detail(items, options)
        line = "vote (structured): " + " > ".join(items)[:160]
        if ranking is not None:
            return ranking, line, ""
        return None, line, unmatched or items[0]
    for tag, payload in reversed(_TAGGED_LINE.findall(body or "")):
        if tag.lstrip("#").casefold() in refs:
            items = payload.split(">") if ">" in payload else [payload]
            ranking, unmatched = match_items_detail(items, options)
            line = f"vote {tag}: {payload}"
            if ranking is not None:
                return ranking, line, ""
            # A tagged line that parsed to nothing (blank payload) is still
            # an attempt at THIS vote: report it as rejected, quoting what
            # the voter actually wrote.
            return None, line, unmatched or payload.strip()
    return None, "", ""


def fmt_window(seconds: float) -> str:
    """Time left on a voting window, at the precision a deadline needs.
    `fmt_age` rounds ('42s' renders as 'now', '4m18s' as '4m') — fine for
    "how old is this", useless for "how long may I still not close"."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def receipt_marker(tag: str) -> str:
    """The idempotency fingerprint a rejection receipt carries. The chair
    re-derives 'already bounced' from the DM thread instead of remembering
    it, so a restarted chair never re-bounces a ballot it already answered."""
    return f"BALLOT NOT COUNTED — vote {tag}" if tag else ""


def rejection_receipt(topic: str, tag: str, options: list[str],
                      line: str, unmatched: str,
                      standing: list[int] | None = None) -> str:
    """The DM a voter gets back when their ballot did not parse. It quotes
    the line, names the exact item that matched nothing, and prints the
    accepted spellings for every option — the three forms the vote post
    itself puts in front of them. Never destructive: a failed REVISION
    leaves the earlier valid ballot standing and says so, because voiding a
    counted ballot over a typo would disenfranchise the voter twice."""
    shown = options[:12]
    spellings = "\n".join(
        f"  {i + 1}. {o[:60]}"
        f"   → \"{i + 1}\"  or  \"{i + 1}. {o[:28]}\"  or  \"{o[:40]}\""
        for i, o in enumerate(shown))
    more = (f"\n  … and {len(options) - len(shown)} more option(s)"
            if len(options) > len(shown) else "")
    stands = ""
    if standing:
        picks = " > ".join(options[i][:40] for i in standing)
        stands = ("\nYour PREVIOUS ballot still stands and is being counted: "
                  f"{picks}. Re-send a readable line to change it.")
    return (f"{receipt_marker(tag)} ({topic})\n\n"
            f"Your line: {line.strip()[:200]}\n"
            f"I could not match \"{unmatched.strip()[:80]}\" to any option, "
            "so the whole ballot was refused (dropping one item of a ranking "
            "would distort your order).\n\n"
            f"Accepted spellings — any ONE of these per item:\n{spellings}{more}\n"
            f"Ranking:  vote {tag}: 2 > 1\n"
            f"Send ONE corrected line and it counts.{stands}")


def ballot_from(body: str, data: dict[str, Any] | None,
                options: list[str]) -> list[int] | None:
    """A reply's ballot: the structured form (data['vote'], a string or list
    of option texts/numbers) wins over prose when both are present — tool-
    first agents should not depend on text formatting."""
    structured = (data or {}).get(VOTE_DATA_KEY)
    if isinstance(structured, str):
        structured = [structured]
    if isinstance(structured, list) and structured:
        return _match_items([str(x) for x in structured], options)
    return parse_ballot(body, options)


@dataclass
class BallotScan:
    """What ONE pass over the ballot-bearing threads found. `seen` is the
    reconciliation number: every identity that put a ballot ATTEMPT for this
    vote in front of the chair, counted or not. `seen <= counted + rejected`
    is the invariant a reader of the published result can check — when it
    fails, a ballot was SEEN and then lost on the way to the roll call, which
    is precisely the failure a bare turnout number cannot show. (It is <=,
    not ==, because one voter can be both: a bad revision after a readable
    line is rejected while its earlier ballot still stands.)"""

    ballots: dict[str, list[int]]                     # voter -> ranking
    rejected: list[dict[str, Any]]                    # unparseable attempts
    seen: set[str]                                    # voters who attempted

    @property
    def counts(self) -> dict[str, int]:
        return {"ballots_seen": len(self.seen),
                "ballots_counted": len(self.ballots),
                "ballots_rejected": len(self.rejected)}


def fold_ballot_thread(rows: list[Any], scan: BallotScan, *, chair: str,
                       refs: set[str], options: list[str], since_ts: float,
                       marker: str = "", channel: str = "", root_id: str = "",
                       reject: bool = True) -> None:
    """Fold ONE thread's messages into `scan` — the single tally
    implementation the chair's client-side watcher and the hub's deadline
    sweep both run, so the two publishers can never disagree about what a
    thread contained.

    Peer-sent messages only (never the chair's own lines — quoting the
    template back must not cast a vote for it) carrying a ballot for THIS
    vote; latest per sender wins; only messages newer than the vote count
    (small clock slack), because ballot threads are long-lived. A reply to
    `root_id` needs no tag: the thread already says which vote it answers.

    A rejection is dropped the moment the same voter sends a readable line
    AFTER it — 'your latest ballot line counts' applies to corrections too —
    and receipt-already-sent is derived from the thread itself (a later
    chair message carrying `marker`), never from chair memory, so a
    restarted chair never double-bounces. `reject=False` for the vote's own
    CHANNEL: receipts are DM'd, and bouncing a public unparseable line would
    post 'your ballot did not count' into the room.
    """
    rejects: dict[str, dict[str, Any]] = {}
    receipt_seq = -1
    for m in rows:
        if getattr(m.kind, "value", m.kind) != "message":
            continue
        if m.sender == chair:
            if marker and marker in (m.body or ""):
                receipt_seq = max(receipt_seq, m.seq)
            continue
        if m.created_at < since_ts - 60:
            continue
        ballot, line, unmatched = dm_ballot_outcome(
            m.body, refs, options, m.data)
        if ballot is None and not line and root_id and m.reply_to == root_id:
            ballot = ballot_from(m.body, m.data, options)
        if ballot is not None:
            scan.ballots[m.sender] = ballot
            scan.seen.add(m.sender)
            rejects.pop(m.sender, None)   # a later readable line corrects
        elif line:
            scan.seen.add(m.sender)
            rejects[m.sender] = {"voter": m.sender,
                                 "channel": channel or m.channel,
                                 "seq": m.seq, "line": line,
                                 "item": unmatched}
    if not reject:
        return
    for row in rejects.values():
        row["receipted"] = receipt_seq > row["seq"]
        row["standing"] = scan.ballots.get(row["voter"])
        scan.rejected.append(row)


@dataclass
class VoteTally:
    first: list[int]                # first-choice count per option
    voters: list[list[str]]         # who first-chose each option, ballot order
    borda: list[int] | None         # points per option; None without rankings
    ranked: int                     # how many ballots actually ranked


def tally_ballots(options: list[str],
                  ballots: dict[str, list[int]]) -> VoteTally:
    """Fold ballots (agent -> ranking, best first) into the tally. Borda
    points are len(options)-1 for a first place downwards; a single-choice
    ballot scores exactly like ranking that option first and listing no
    others, so mixed ballots stay comparable. Borda renders only when at
    least one ballot ranked — otherwise first-choice counts say it all."""
    n = len(options)
    first = [0] * n
    voters: list[list[str]] = [[] for _ in range(n)]
    borda = [0] * n
    ranked = 0
    for agent, ranking in ballots.items():
        first[ranking[0]] += 1
        voters[ranking[0]].append(agent)
        if len(ranking) > 1:
            ranked += 1
        for rank, idx in enumerate(ranking):
            borda[idx] += max(0, n - 1 - rank)
    return VoteTally(first=first, voters=voters,
                     borda=borda if ranked else None, ranked=ranked)


def vote_block(s: Style, *, ref: str, topic: str, options: list[str],
               tally: VoteTally, total_members: int,
               waiting: list[str], comments: list[str],
               notes: list[str] | None = None,
               footer: str | None = None) -> str:
    """The /tally view: one line per option (bar, count, who), the borda
    order when rankings exist, who is still expected, plus caller-supplied
    notes (e.g. ballot-secrecy leaks) and a caller-supplied footer (the
    chair's close hint, or where the published result lives)."""
    width = term_width()
    voted = sum(tally.first)
    header = (f"{s.cyan('VOTE')} {s.dim(f'#{ref}')} {s.bold(safe(topic))} "
              + s.dim(f"— {voted}/{total_members} voted"))
    lines = [s.dim("─" * width), header]

    label_w = min(max((len(o) for o in options), default=0), 28)
    peak = max(tally.first) if any(tally.first) else 0
    for i, option in enumerate(options):
        count = tally.first[i]
        bar = "█" * max(1, round(10 * count / peak)) if count else ""
        names = ", ".join(tally.voters[i])
        room = max(10, width - label_w - 24)
        if len(names) > room:
            names = names[:room - 1] + "…"
        lines.append(f"  {i + 1}. {safe(option)[:label_w]:<{label_w}} "
                     f"{s.yellow(f'{bar:<10}')} {count:>2}  {s.dim(safe(names))}")

    if tally.borda is not None:
        order = sorted(range(len(options)), key=lambda i: -tally.borda[i])
        scored = " > ".join(f"{safe(options[i])} {tally.borda[i]}"
                            for i in order if tally.borda[i] > 0)
        lines.append(s.dim(f"  {tally.ranked} ranked ballot(s) · borda: ")
                     + scored)
    if waiting:
        lines.append(s.dim("  waiting: " + ", ".join(safe(w) for w in waiting)))
    if comments:
        lines.append(s.dim("  commented, no ballot: "
                           + ", ".join(safe(c) for c in comments)))
    for note in notes or []:
        lines.append(s.yellow(f"  {safe(note)}"))
    if footer:
        lines.append(s.dim(f"  {safe(footer)}"))
    return "\n".join(lines)


def result_body(topic: str, options: list[str], tally: VoteTally,
                total_members: int, reason: str = "closed by the chair",
                rejected: int = 0, seen: int | None = None) -> str:
    """The published close message — the full, auditable outcome in plain
    text: counts, the roll call (every voter can verify their listed
    ballot), the borda order when ballots ranked, the RECONCILIATION line
    (seen/counted/rejected — a voter whose ballot existed but is missing
    from the roll call can now prove it), and why it closed."""
    lines = [f"VOTE RESULT — {topic}", ""]
    order = sorted(range(len(options)), key=lambda i: -tally.first[i])
    for i in order:
        who = ", ".join(tally.voters[i])
        lines.append(f"  {options[i]}: {tally.first[i]}"
                     + (f"  ({who})" if who else ""))
    if tally.borda is not None:
        ranked = sorted(range(len(options)), key=lambda i: -tally.borda[i])
        lines.append("  borda: " + " > ".join(
            f"{options[i]} {tally.borda[i]}" for i in ranked
            if tally.borda[i] > 0))
    lines.append("")
    if rejected:
        lines.append(f"  {rejected} ballot(s) arrived UNREADABLE and were not "
                     "counted (each voter was sent a receipt by DM)")
    counted = sum(tally.first)
    if seen is not None:
        lines.append(f"  ballots: {seen} seen · {counted} counted · "
                     f"{rejected} rejected"
                     + ("  — MISMATCH: a ballot was seen and neither counted "
                        "nor rejected; tell the chair"
                        if seen > counted + rejected else ""))
    lines.append(f"turnout {counted}/{total_members} · {reason}")
    return "\n".join(lines)


def result_ballots(payload: dict[str, Any],
                   options: list[str]) -> dict[str, list[int]]:
    """Rebuild the ballots map from a published vote_result payload,
    tolerantly: out-of-range indices and malformed entries drop — a
    forged or damaged payload must never break the tally view."""
    ballots: dict[str, list[int]] = {}
    raw = payload.get("ballots")
    if not isinstance(raw, dict):
        return ballots
    for agent, ranking in raw.items():
        if not isinstance(ranking, list):
            continue
        clean = [i for i in ranking
                 if isinstance(i, int) and 0 <= i < len(options)]
        if clean:
            ballots[str(agent)] = clean
    return ballots


HUB_PUBLISHER = "hub"


def published_result(root: Any, replies: list[Any]) -> Any | None:
    """The authoritative published result in this thread, latest wins, or
    None. Authority is the CHAIR or the HUB and nobody else: a forged
    `vote_result` from a third party must never close a vote (it would let
    any member fake an outcome), and the hub is admitted because the hub
    deadline sweep is what guarantees publication when the chair's process
    is not alive at `closes_at`. Both publishers read this before posting,
    which is the whole double-publish guard — restart-safe, because it is
    FOUND in the channel rather than remembered."""
    return next(
        (r for r in reversed(replies)
         if r.sender in (root.sender, HUB_PUBLISHER)
         and isinstance((r.data or {}).get(VOTE_RESULT_KEY), dict)),
        None)


def result_payload(info: dict[str, Any], ballots: dict[str, list[int]],
                   total_members: int, reason: str,
                   scan: BallotScan | None = None,
                   rejected: int = 0) -> dict[str, Any]:
    """The machine-readable `vote_result` — built in ONE place so the
    chair's close and the hub's deadline sweep publish the same shape."""
    counts = (scan.counts if scan is not None
              else {"ballots_seen": len(ballots) + rejected,
                    "ballots_counted": len(ballots),
                    "ballots_rejected": rejected})
    return {"topic": info["topic"], "options": info["options"],
            "ballots": ballots, "total_members": total_members,
            "closed": reason, **counts,
            **({"rejected": counts["ballots_rejected"]}
               if counts["ballots_rejected"] else {})}


def vote_info(root: Any, channel: str) -> dict[str, Any] | None:
    """The working record of one vote, from its message: None when the
    message carries no usable vote payload."""
    spec = (root.data or {}).get(VOTE_DATA_KEY) or {}
    options = [str(o) for o in spec.get("options", [])]
    if not options:
        return None
    closes_at = spec.get("closes_at")
    return {"root": root, "channel": channel, "options": options,
            "topic": str(spec.get("topic", root.title)),
            "tag": str(spec.get("tag", "")),
            "closes_at": float(closes_at)
            if isinstance(closes_at, (int, float)) else None}


class VoteChair:
    """The chair side of a blind vote's lifecycle. Lives client-side because
    only the chair's DM threads hold the ballots — the hub knows nothing
    about votes.

    Blindness protects voters from anchoring on earlier ballots; once it
    protects nothing — every eligible member voted, or the deadline passed —
    the result belongs to the channel. `check_due` (driven by the chat app's
    background watcher) publishes on either condition, `recover` re-learns
    chaired votes after a client restart so a deadline never silently dies
    with a closed terminal."""

    def __init__(self, client: Any, me: str,
                 announce: Callable[[str], None]) -> None:
        self.client = client
        self.me = me
        self.announce = announce
        self.open: dict[str, dict[str, Any]] = {}   # root id -> vote_info

    def register(self, root: Any, channel: str) -> None:
        info = vote_info(root, channel)
        if info is not None and root.sender == self.me:
            self.open[root.id] = info

    # -- gathering ----------------------------------------------------------

    async def since_root(self, channel: str, root: Any) -> list[Any]:
        """Every channel message after `root`, oldest first — pages forward
        from the root's seq over the existing history endpoint (no hub
        extension; channels at human scale are a few pages). NOT narrowed to
        replies: a ballot posted as a fresh channel message naming the chair
        parses like any other and used to be invisible to the tally, which
        is one of the silent-loss paths behind the 7-DM'd/6-counted
        incident."""
        rows: list[Any] = []
        cursor = root.seq
        while True:
            page = await self.client.history(channel, since=cursor, limit=200)
            if not page:
                return rows
            rows.extend(page)
            cursor = page[-1].seq
            if len(page) < 200:
                return rows

    async def replies_to(self, channel: str, root: Any) -> list[Any]:
        """All replies to `root`, oldest first."""
        return [m for m in await self.since_root(channel, root)
                if m.reply_to == root.id]

    async def _dm_ballots(self, refs: set[str], options: list[str],
                          since_ts: float, tag: str = "",
                          scan: BallotScan | None = None) -> tuple[
                              dict[str, list[int]], list[dict[str, Any]]]:
        """Blind ballots from the chair's DM threads, folded by the shared
        `fold_ballot_thread` the hub sweep also runs. Pages each thread
        forward over the history endpoint; the per-thread rules (peer lines
        only, latest wins, receipts derived from the thread) live in the
        fold, so chair and hub cannot drift."""
        scan = scan if scan is not None else BallotScan({}, [], set())
        marker = receipt_marker(tag)
        names = [c["name"] for c in await self.client.list_channels()
                 if c["member"] and c["name"].startswith("dm:")]
        for name in names:
            rows: list[Any] = []
            cursor = 0
            while True:
                page = await self.client.history(name, since=cursor, limit=200)
                if not page:
                    break
                rows.extend(page)
                cursor = page[-1].seq
                if len(page) < 200:
                    break
            fold_ballot_thread(rows, scan, chair=self.me, refs=refs,
                               options=options, since_ts=since_ts,
                               marker=marker, channel=name)
        return scan.ballots, scan.rejected

    async def collect(self, info: dict[str, Any]) -> dict[str, Any]:
        """Everything /tally and the watcher need, in one pass: the
        published result if any (the chair's or the hub's — forged results
        from anyone else are ignored), public ballots posted in the channel
        (counted, flagged), DM ballots when I am the chair, REJECTED ballots
        (chair-side only, where the blind ballots live), commenters, the
        current member list, and the `scan` reconciliation counts.

        `rejected` is not cosmetic: without it an empty tally reads the same
        whether the room stayed silent or the parser ate every ballot, and a
        chair cannot tell 'nobody voted' from 'nobody was counted'."""
        root, channel, options = info["root"], info["channel"], info["options"]
        refs = {r for r in (info["tag"].casefold(),
                            f"{root.seq}@{channel}".casefold()) if r}
        rows = await self.since_root(channel, root)
        replies = [m for m in rows if m.reply_to == root.id]
        published = published_result(root, replies)
        scan = BallotScan({}, [], set())
        # The CHAIR is excluded from the in-room fold (its own vote body and
        # nudges are not ballots, and it does not vote in its own poll) —
        # never the viewer, or a voter reading the tally would erase itself.
        fold_ballot_thread([r for r in rows if r is not published], scan,
                           chair=root.sender, refs=refs, options=options,
                           since_ts=root.created_at, channel=channel,
                           root_id=root.id, reject=False)
        public = dict(scan.ballots)            # everything so far is IN-ROOM
        commenters = {r.sender for r in replies
                      if r.kind.value == "message" and r is not published
                      and r.sender not in public}
        if self.me == root.sender:
            await self._dm_ballots(refs, options, root.created_at,
                                   info.get("tag", ""), scan)
        members: list[str] = []
        with contextlib.suppress(Exception):
            data = await self.client.channel_info(channel)
            members = [m["agent_id"] for m in data.get("members", [])]
        return {"published": published, "public": public,
                "ballots": scan.ballots,
                "commenters": commenters - set(scan.ballots),
                "members": members, "rejected": scan.rejected, "scan": scan}

    # -- closing --------------------------------------------------------------

    @staticmethod
    def due(info: dict[str, Any], ballots: dict[str, list[int]],
            members: list[str], now: float | None = None) -> str | None:
        """Why this vote should close NOW — 'deadline reached', 'every
        member voted', or None. Full turnout = every current member except
        the chair has a ballot; unknown membership (empty list) never
        triggers it — only the deadline can close a vote we cannot verify
        as complete."""
        now = time.time() if now is None else now
        closes_at = info.get("closes_at")
        if closes_at is not None and now >= closes_at:
            return "deadline reached"
        eligible = {m for m in members if m != info["root"].sender}
        if eligible and eligible <= set(ballots):
            return "every member voted"
        return None

    @staticmethod
    def early_close_block(info: dict[str, Any], ballots: dict[str, list[int]],
                          members: list[str],
                          now: float | None = None) -> str | None:
        """Why the chair may NOT close this vote yet — the announced window
        is a PROMISE to the voters, not a chair preference.

        The at-test incident: a chair announced a five-minute window and
        closed its own vote at 42 seconds with 3 of 6 seats heard; three
        ballots were in flight and died. `closes_at` now BINDS: while the
        announced window is still running AND some eligible seat has not
        balloted, an early close is refused with the remaining time and the
        outstanding COUNT (never the names — that is the blindness the poll
        is for). `force=True` overrides, loudly and on the record.

        Never binding when: the window has passed, every eligible seat has
        voted (blindness protects nothing anymore), or the vote carries no
        deadline at all (pre-deadline clients). Unreadable membership does
        NOT unbind it — an unverifiable turnout is exactly the state the
        window exists to cover."""
        now = time.time() if now is None else now
        closes_at = info.get("closes_at")
        if closes_at is None or now >= closes_at:
            return None
        eligible = {m for m in members if m != info["root"].sender}
        outstanding = eligible - set(ballots)
        if eligible and not outstanding:
            return None
        left = fmt_window(closes_at - now)
        if not eligible:
            who = ("membership could not be read, so full turnout cannot be "
                   "confirmed")
        else:
            who = (f"{len(outstanding)} of {len(eligible)} eligible voter(s) "
                   f"have not balloted")
        return (f"the announced voting window has {left} left and {who}. "
                "Closing now would void ballots in flight — the window you "
                "published is a promise to the voters. The result publishes "
                "ITSELF at the deadline or as soon as everyone has voted; "
                "nothing is required of you. To override anyway, close with "
                "force=true — the published result will say it was closed "
                "early by the chair.")

    @staticmethod
    def forced_reason(info: dict[str, Any], ballots: dict[str, list[int]],
                      members: list[str], now: float | None = None) -> str:
        """The close reason a FORCED early close publishes. It is loud on
        purpose: every voter reading the result must see that the window
        they were promised was cut short, and by how much."""
        now = time.time() if now is None else now
        closes_at = info.get("closes_at")
        left = (fmt_window(closes_at - now)
                if closes_at is not None and closes_at > now else "0s")
        eligible = {m for m in members if m != info["root"].sender}
        silent = len(eligible - set(ballots))
        tail = (f", {silent} of {len(eligible)} eligible voter(s) unheard"
                if eligible else "")
        return (f"CLOSED EARLY BY THE CHAIR — {left} of the announced window "
                f"was cut{tail}")

    async def publish(self, info: dict[str, Any],
                      ballots: dict[str, list[int]], members: list[str],
                      reason: str, rejected: int = 0,
                      scan: BallotScan | None = None) -> Any:
        """Post the result into the channel (resolved reply + machine
        payload) and forget the vote. From here on, every /tally renders
        from the transcript. `rejected` rides the result because a turnout
        of 3/6 means something different when two more ballots arrived and
        could not be read — the room is owed that number, not just the count."""
        root, channel, options = info["root"], info["channel"], info["options"]
        tally = tally_ballots(options, ballots)
        total = len(members) or len(ballots)
        payload = result_payload(info, ballots, total, reason, scan, rejected)
        posted = await self.client.post(
            channel, result_body(info["topic"], options, tally, total, reason,
                                 payload["ballots_rejected"],
                                 payload["ballots_seen"]),
            title=f"VOTE RESULT: {info['topic']}", status=Status.resolved,
            reply_to=root.id, data={VOTE_RESULT_KEY: payload})
        self.open.pop(root.id, None)
        return posted

    async def bounce_rejected(self, info: dict[str, Any],
                              rejected: list[dict[str, Any]]) -> int:
        """DM a rejection receipt for every unparseable ballot not yet
        answered. Reception-time in the only sense this architecture has
        one: the hub knows nothing about votes, so the chair's own watcher
        tick IS the reception path (VOTE_WATCH_INTERVAL, 30s) — the voter
        learns their ballot did not count while the window is still open,
        instead of discovering it in the published roll call. Best-effort
        per receipt: a DM that fails must never stop the tally."""
        sent = 0
        for row in rejected:
            if row.get("receipted"):
                continue
            body = rejection_receipt(info["topic"], info.get("tag", ""),
                                     info["options"], row["line"], row["item"],
                                     row.get("standing"))
            with contextlib.suppress(Exception):
                await self.client.post(
                    row["channel"], body, status=Status.fyi,
                    title=f"ballot not counted — vote {info.get('tag', '')}",
                    to=[row["voter"]])
                row["receipted"] = True
                sent += 1
        return sent

    async def check_due(self) -> None:
        """One watcher tick: publish every chaired vote whose blindness no
        longer protects anything. Votes closed elsewhere (another session,
        a manual /tally close) are dropped from the registry silently."""
        for info in list(self.open.values()):
            with contextlib.suppress(Exception):
                data = await self.collect(info)
                if data["published"] is not None:
                    self.open.pop(info["root"].id, None)
                    continue
                rejected = data.get("rejected") or []
                # Bounce FIRST, then consider closing: a voter whose ballot
                # did not parse must get the news while they can still fix it.
                sent = await self.bounce_rejected(info, rejected)
                if sent:
                    self.announce(
                        f"(vote #{info['root'].seq}: {sent} unreadable "
                        "ballot(s) bounced back to their voters by DM)")
                reason = self.due(info, data["ballots"], data["members"])
                if reason is None:
                    continue
                posted = await self.publish(info, data["ballots"],
                                            data["members"], reason,
                                            len(rejected), data.get("scan"))
                self.announce(
                    f"(vote #{info['root'].seq} in {info['channel']} closed"
                    f" — {reason}; result published as #{posted.seq})")

    async def recover(self) -> None:
        """Re-learn the votes I chair after a restart: scan my channels for
        my vote messages without a published result. Best-effort — a vote
        posted from another identity or already closed is not mine to
        watch."""
        with contextlib.suppress(Exception):
            channels = [c["name"] for c in await self.client.list_channels()
                        if c["member"] and not c["name"].startswith("dm:")]
            for name in channels:
                with contextlib.suppress(Exception):
                    mine: dict[str, Any] = {}
                    cursor = 0
                    while True:
                        page = await self.client.history(name, since=cursor,
                                                         limit=200)
                        if not page:
                            break
                        for m in page:
                            if m.sender != self.me:
                                continue
                            if (m.data or {}).get(VOTE_DATA_KEY):
                                mine[m.id] = m
                            elif (isinstance((m.data or {}).get(
                                    VOTE_RESULT_KEY), dict)
                                    and m.reply_to in mine):
                                mine.pop(m.reply_to, None)
                        cursor = page[-1].seq
                        if len(page) < 200:
                            break
                    for root in mine.values():
                        self.register(root, name)


async def watch_votes(chair: VoteChair, *,
                      interval: float = VOTE_WATCH_INTERVAL,
                      recover_every: float = VOTE_RECOVER_INTERVAL,
                      closing: Callable[[], bool] | None = None) -> None:
    """The chair-duty loop every long-lived surface of an identity runs
    (chat app, MCP server process, AgentRunner): adopt this identity's open
    votes wherever they were opened from, then tick, publishing whatever is
    due. Periodic re-recovery adopts votes opened from OTHER surfaces while
    this one is running. Cancellation-safe: surfaces cancel it on shutdown."""
    with contextlib.suppress(asyncio.CancelledError):
        await chair.recover()
        last_recover = time.time()
        while closing is None or not closing():
            await asyncio.sleep(interval)
            if time.time() - last_recover >= recover_every:
                await chair.recover()
                last_recover = time.time()
            await chair.check_due()


async def vote_operation(client: Any, me: str, channel: str, message_id: str,
                         *, close: bool = False,
                         force: bool = False) -> dict[str, Any]:
    """Surface-neutral tally/close returning machine-shaped state (the MCP
    tools' backend; the chat /tally renders its own richer view). Honors
    ballot secrecy: only the chair sees counts before publication — and a
    finished vote publishes on sight rather than reporting a stale state.

    The announced window BINDS the chair (`early_close_block`): `close`
    inside it with voters unheard is refused with the time left and the
    outstanding count; `force` overrides and marks the published result."""
    rows = await client.read(channel, message_id)
    root = next((m for m in rows if m.id == message_id), None)
    info = vote_info(root, channel) if root else None
    if info is None:
        return {"ok": False, "error": 400,
                "detail": f"message '{message_id}' in '{channel}' is not a "
                          "vote (no vote payload)",
                "action": "REQUEST FAILED — nothing was posted or changed"}
    chair = VoteChair(client, me, lambda _text: None)
    gathered = await chair.collect(info)
    published = gathered["published"]
    if published is not None:
        return {"closed": True, "published_seq": published.seq,
                "result": published.data[VOTE_RESULT_KEY]}
    if me != root.sender:
        if close:
            return {"ok": False, "error": 403,
                    "detail": f"only {root.sender} (the chair) can close"
                              " this vote",
                    "action": "REQUEST FAILED — nothing was posted or changed"}
        return {"closed": False, "blind": True, "chair": root.sender,
                "closes_at": info["closes_at"],
                "note": "ballots go to the chair by DM; the full result is"
                        " published to the channel when the vote closes"}
    rejected = gathered.get("rejected") or []
    # Receipts ride every chair-side pass, close or not: the chair looking at
    # the tally is the moment the fleet has a live process to send them from.
    await chair.bounce_rejected(info, rejected)
    ballots, members = gathered["ballots"], gathered["members"]
    reason = VoteChair.due(info, ballots, members)
    if close and reason is None:
        blocked = VoteChair.early_close_block(info, ballots, members)
        if blocked is not None and not force:
            return {"ok": False, "error": 409,
                    "detail": f"early close refused: {blocked}",
                    "closes_at": info["closes_at"],
                    "rejected_ballots": len(rejected),
                    "action": "REQUEST FAILED — the vote is still open and "
                              "nothing was published"}
        reason = (VoteChair.forced_reason(info, ballots, members)
                  if blocked is not None else "closed by the chair")
    scan = gathered.get("scan")
    if reason is not None:
        posted = await chair.publish(info, ballots, members, reason,
                                     len(rejected), scan)
        return {"closed": True, "reason": reason, "published_seq": posted.seq,
                "ballots": ballots,
                **(scan.counts if scan is not None else {}),
                **({"rejected_ballots": len(rejected)} if rejected else {})}
    counts = dict(zip(info["options"],
                      tally_ballots(info["options"], ballots).first))
    waiting = sorted(set(members) - set(ballots) - {root.sender})
    return {"closed": False, "chair": me, "counts": counts,
            "ballots": ballots, "waiting": waiting,
            "closes_at": info["closes_at"],
            # 0 ballots and N rejected must never render identically — the
            # empty-room misreading is what closed a vote at 42 seconds.
            "rejected_ballots": len(rejected),
            "rejected_detail": [{"voter": r["voter"], "item": r["item"]}
                                for r in rejected],
            **(scan.counts if scan is not None else {}),
            "commenters": sorted(gathered["commenters"])}
