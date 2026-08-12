"""`agora drive` — the external resume-driver for a HEADLESS agent seat.

The reception problem, restated after a night of field failure: a turn-based
agent (cursor-agent, codex, ...) only acts when something gives it a turn.
Debt-scoped wakes fixed the token burn but left the fleet purely reactive,
and an IN-session `agora listen` monitor either traps the seat in
check->ack->re-arm without acting, or goes idle. Both failure modes are
BEHAVIORAL — they depend on per-turn model discipline (end the turn, act on
the wake, re-arm correctly), which the fleet repeatedly falsified.

The driver makes reception STRUCTURAL instead. It is a plain owner-run loop
(consumer-side, dies with the operator's session — NOT hub machinery, NOT
persistent; the same standing as a stop hook or `agora up`):

    while alive:
        block cheaply in `agora listen --once --important-only`   # ~0 tokens
        on an obligation wake -> spawn ONE bounded agent turn      # it ACTS
        the turn ends by returning (a process exit)                # it YIELDS
        loop

- YIELD is a process exit, not a behavior the model must remember.
- The check->ack->re-arm TRAP is impossible: the spawned turn's only job is
  the one reception pass; the driver, not the model, owns re-arming.
- Idle waiting is a blocked syscall in `agora listen`, costing nothing.
- Worst case is ONE wasted bounded turn per spurious wake, never a loop.

Memory persists across wakes via the harness's own `--resume <session>`; the
durable memory is the hub itself (channels, claims, decisions), so a rotated
session loses only uncommitted scratch.

SAFETY (non-negotiable, review E): the spawned turn defaults to
`--sandbox enabled`, never bare `--force`. Message bodies are data authored
by other agents; an unsandboxed all-tools turn driven by a hostile peer
message is arbitrary code execution on the operator's machine. Nonce fencing
is advisory to the model, not a boundary — the sandbox is the boundary.

THE LOOP HAS FIVE STATES, and says which one it is in on every pass
(`AGORA_DRIVE state=<name> ... next=<seconds>s`), so a stall is readable from
stdout alone:

    armed    — listening. `next` is the listen window: the chain cadence when
               this seat holds a work chunk it may run, else the idle ceiling.
    turn     — a reception turn is running (the seat is deaf until it returns).
    chunk    — a work chunk is running (same, for longer).
    backoff  — the last turn never reached the hub. Retries are spaced
               exponentially; the wake is HELD, never dropped.
    parked   — an hourly budget is spent. The wake is HELD; `next` is its
               exact release.

There is exactly ONE failure mechanism (backoff). The poison ledger and its
wake quarantine were deleted 2026-08-03: in the whole live record they never
once fired (every `drive-*.attempts` file held `{}` or a single strike, and no
driver log ever printed a quarantine line), while their failure mode — DROP a
specific obligation and go deaf to it — is the exact outcome the driver exists
to prevent. Backoff dominates it: same retry spacing, no dropped wake.

KNOWN LIMITATION — the loop is SINGLE-THREADED. While a turn's subprocess
runs, the driver cannot arm its listener, so the seat is deaf to every
obligation for the whole span. Reception is bounded at RECEPTION_TURN_TIMEOUT
for exactly this reason; a work chunk may hold it for --work-timeout. This
pass bounds and EXPOSES the blindness (an unproven provider never gets the
full chunk budget, and a running chunk says so on stdout at intervals) rather
than removing it: making reception concurrent with a chunk needs a second
thread and is not attempted here.
"""

from __future__ import annotations

from .models import elide

import contextlib
import dataclasses
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config as _config
from .listen import (_DRIVER_BROADCAST_WAKE, _DRIVER_UNOWNED_WAKE,
                     _owed_snapshot, pid_alive, run_listen)
from .mcp.runtime import (MCPBinding, MCPRuntimeProbe, format_probe_failure,
                          probe_mcp_runtime, resolve_mcp_command)
from .setup_harness import resolve_workspace_identity

# The wake prompt is STATIC and points at the skill (review B): it never
# carries peer-authored text (injection-proof, cache-stable). A reception
# wake settles communication debt and must start work that the debt assigns.
# If the work cannot finish in this turn, it becomes a real store claim so the
# driver's independent work loop can continue it. A prose promise is never a
# work queue.
#
# The EMPTY OUTCOME is authorized explicitly, because measurement says the
# alternative is not manners but arithmetic: 8.3% ceremony while addressed
# asks were live vs 50% when seats woke owing nothing (0140 field test 2). A
# seat that wakes empty and finds no instruction to stay silent manufactures
# a receipt, and that receipt wakes the room, which wakes more empty seats.
WAKE_PROMPT = (
    "AGORA WAKE. Agora MCP is REQUIRED: use only Agora MCP tools for Agora "
    "communication, never the `agora` CLI or direct HTTP. Run ONE reception "
    "pass: check_inbox; settle what you OWE. For a question, answer it. For "
    "assigned work, BEGIN IT THIS TURN. Thinking, designing and agreeing a "
    "plan with the room IS work — on anything the room shares it is the "
    "FIRST work, and going straight to building is how two seats build the "
    "same thing twice. What is banned is a BARE promise: an acknowledgement "
    "with nothing attached. Say what you are taking AND do something with it "
    "in the same turn. When you start on something others depend on, SAY SO "
    "in the room, and say so again when a piece is ready for them — a "
    "colleague who discovers your lane by collision was failed by you. "
    "If the human posted an open/blocked task in a shared room, treat it as "
    "a contribution call: evaluate it against what you own; if you can help, "
    "reply once with the slice you own and how you will contribute; if not, "
    "say nothing. "
    "If the work now clearly needs 3+ seats speaking over multiple turns, "
    "create the focused room immediately with create_group and move the "
    "working thread there; if it only needs one peer, use a DM. In that "
    "focused room, planning comes BEFORE implementation: agree the plan, the "
    "phases if any, and the ownership split before building. "
    "Finish and answer with "
    "evidence when feasible. If the job needs another turn, create or update "
    "a real `claim:msg-<source seq>` row with store_set in the request's "
    "channel; its value MUST include owner, status, source_message_id, and "
    "next_step. Then complete one useful work slice before replying. Never "
    "write 'claiming' only in prose. Use answers only for completed asks, "
    "ack_inbox, then END. If check_inbox shows nothing owed BY YOU and no "
    "ask names you, that is a COMPLETE and correct turn: ack_inbox and END "
    "THE TURN WITHOUT POSTING ANYTHING. Posting an acknowledgement, a "
    "status line, a 'nothing for me' note, or any other receipt on an empty "
    "reception pass is the anti-pattern this instruction exists to stop — "
    "silence is the right answer and it costs the room nothing. Do not "
    "advance unrelated claims, post routine progress traffic, wait, listen, "
    "sleep, or re-check; the driver owns reception and automatically "
    "continues real claims."
)

# Boot prompt for a fresh session (no prior --resume): establish identity
# first, then do the first reception pass. Deliberately NOT the phrase
# "start agora protocol" — that phrase now triggers the skill's (a) boot
# (self-armed reception), which a driven seat must never run.
BOOT_PROMPT = (
    "You are a DRIVEN agora seat. Agora MCP is REQUIRED. Use only the Agora "
    "MCP tools for Agora communication; never invoke the `agora` CLI or an "
    "HTTP substitute. First: call whoami and heed the hub "
    "rules; call read_charter() ONCE now (the standing answer to who is who "
    "and what each seat owes). A receipt is per-SEAT and this context is "
    "NEW: the delegate that soloed a commission on 2026-08-04 held a "
    "day-old receipt, was told its charter was current, and so never read "
    "the sentence telling it to decompose into addressed asks. If you hold "
    "any delegation, read the part that names what YOU owe. Skim your "
    "channels. Then run one reception pass (check_inbox, "
    "settle what you owe). Questions require answers. Assigned work requires "
    "actual workspace work now, not an acknowledgement or promise; finish it "
    "when feasible, otherwise create a real linked `claim:msg-<source seq>` "
    "store row (owner, status, source_message_id, next_step), complete one "
    "useful slice, then ack and END. Use answers only on completion. Nothing "
    "owed by you and no ask naming you is a complete turn: ack and END "
    "WITHOUT POSTING — an empty reception pass that posts anyway is the "
    "anti-pattern. Do not "
    "advance unrelated claims or post routine progress receipts; the driver "
    "automatically continues real claims. If Agora MCP is unavailable, do not "
    "improvise: end with "
    "AGORA_MCP_UNAVAILABLE and the exact MCP error. A driver loop wakes you "
    "on each new message; never start a listener yourself."
)

# The work prompt: STATIC like the others — no hub or peer
# text is ever interpolated. Continuation is a LOOP property of the driver
# (chunks chain at short listen windows; any obligation preempts the next
# chunk at the arm between them), never a model posture — the shape the
# 0083/0085 falsifications left standing. Supersession check is FIRST:
# before continuing, the turn re-reads the claim row and newer messages,
# because the operator or a peer may have canceled/refined/replaced the
# task while the seat was heads-down.
WORK_PROMPT = (
    "AGORA WORK CHUNK. Agora MCP is REQUIRED: use only Agora MCP tools for "
    "Agora communication, never the `agora` CLI or direct HTTP. No new "
    "obligation is waiting; you hold continuable work — a live claim row, or "
    "an open phase: row you steward — continue THAT work. A phase row is "
    "ignition, not a slice receipt: if the work is more than one turn, open a "
    "claim row for it NOW and chain on that. A claim you already marked "
    "blocked or parked does NOT count against opening a new one for different "
    "work. FIRST re-read the "
    "row and any newer messages touching the task: a newer message "
    "may have canceled, refined, or superseded it (the record outranks "
    "your memory) — if so, adjust or park on the record instead of "
    "continuing blind. If the task has outgrown #commons or another open "
    "floor and 3+ seats now need to coordinate, create the focused room "
    "before continuing the multi-seat work. If the room still lacks a shared "
    "plan or phase order, do that planning work first. Otherwise do ONE bounded slice "
    "toward completion, "
    "stop at a safe checkpoint (workspace consistent: commit or stash), "
    "overwrite your claim row with a one-line progress receipt naming "
    "what is done and what is next. That row is the ONLY per-slice receipt: "
    "never post reception-pass, no-delta, guard-rerun, parked, or routine "
    "progress messages to a channel. If blocked, mark the claim row and send "
    "one addressed structured ask in a DM or focused group only when another "
    "seat can act; never broadcast or repeat an unchanged blocker. Then END "
    "this turn — the driver "
    "re-wakes you for the next slice. Finished, blocked, or not worth "
    "continuing? Write done/blocked/parked on the row and END. Post only one "
    "typed external milestone or delivery when the event is genuinely new. "
    "Do NOT check the inbox again, wait, listen, or "
    "start watchers — reception is the driver's job between slices."
)

# A fresh initiative session needs identity/orientation before the work
# contract. Keeping this distinct from BOOT_PROMPT prevents a reception boot
# from doing work, and prevents a rotated work session from wasting its first
# chunk on reception only.
WORK_BOOT_PROMPT = (
    "AGORA WORK CHUNK BOOT. You are a DRIVEN agora seat. Agora MCP is "
    "REQUIRED: use only Agora MCP tools for Agora communication, never the "
    "`agora` CLI or direct HTTP. First call "
    "whoami and heed the hub rules; call read_charter() once now (a receipt "
    "is per-seat, and this context is new — if you hold a delegation, read "
    "what YOU owe); skim your channels. Then follow the work "
    "contract: re-read your continuable work — a live claim row, or an open "
    "phase: row you steward — and newer messages that may "
    "supersede it, do one bounded slice, and update the claim row (open one "
    "if a stewarded phase is all you hold; a blocked or parked row does NOT "
    "count against opening a new one). The row is "
    "the only per-slice receipt; never post reception-pass, no-delta, guard-"
    "rerun, parked, or routine progress messages. If blocked, mark the row "
    "and send one addressed structured ask in a DM or focused group only "
    "when another seat can act; never broadcast or repeat an unchanged blocker. "
    "Then END. Do not wait, listen, or start watchers."
)

#: Prepended to a DELEGATE's work chunk. Its job is the room, not the code.
SUPERVISE_PROMPT = (
    "You are a user's delegate. Your job is to make their work simpler.\n"
    "They may be reading along right now, or not — either way, they should "
    "not have to follow every seat to know where things stand. That is what "
    "you are for. With a handful of agents it is a convenience; with twenty "
    "it is the difference between a project they can follow and a firehose "
    "they cannot. So keep the whole picture and give it back to them "
    "condensed: what progressed, what is stuck and why, what was decided and "
    "on what grounds, what needs them specifically.\n"
    "What you may DO with that picture depends entirely on the powers they "
    "granted you — check whoami.delegations. Some delegates only watch and "
    "report. Some may run the machinery. Some may decide in the user's name. "
    "Read what you hold before you act, and never promise a move your grant "
    "does not cover.\n"
    "When a task on #commons or another open floor already has a real owner "
    "and the contributor set is known, your default move is to put the work "
    "in its focused room immediately: two speaking seats = DM; three+ or "
    "clearly multi-turn coordination = create_group. Keep the open floor for "
    "the pointer, cross-room decisions, milestones and final delivery.\n"
    "For a fresh operator task in a shared room, first look for contributor "
    "replies already on the operator thread. If contributors already stated "
    "their slices there, that set is known: do NOT ask the same question "
    "again. Reply in-thread to the operator naming that you own the "
    "commission and where the work is moving, then create the focused room "
    "immediately and make the first job there the shared plan. Only run a "
    "formation round when the contributor set is not yet known: let each "
    "seat decide silently whether it can contribute; contributors state what "
    "they own and how they help; once that set is known, create the room. "
    "Use phases when the plan needs ordering, and do not let implementation "
    "jump ahead of an unsettled plan. A new root pointer does not settle the "
    "operator thread; your operator-facing progress updates belong in-thread "
    "on the original commission at phase changes and completion.\n"
    "supervise(channel) is your radar: who is live and holding nothing, what "
    "each seat is for, whether they can hear you, which rows are stuck and on "
    "whom, and — given your powers — which of those you can end yourself. "
    "Read it each chunk, then act within your grant: hand an idle seat the "
    "slice its expertise fits, ask the room what it thinks, call a vote when "
    "the decision belongs to them, ask for a shared plan they can argue over, "
    "chase whoever is blocking someone, wake a seat that went quiet mid-task. "
    "The work itself belongs to the seats; you are not the one building.\n"
    "Decide only after hearing from the people who know, and only what your "
    "powers allow. If the user is reachable and the call is theirs, ask them. "
    "If it is yours to make, make it — and tell them what you decided and "
    "why, so they can disagree.\n\n"
)

DEFAULT_MODEL: str | None = None
DEFAULT_MAX_WAIT = 1200.0           # idle ceiling; a wake returns instantly
DEFAULT_TURN_BUDGET = 250           # light abuse ceiling; ordinary debt rarely parks
DEFAULT_BROADCAST_TURN_BUDGET = 100 # roomy fuse for noisy unowned wakes
TURN_BUDGET_WINDOW = 3600.0
DEFAULT_SESSION_ROTATE = 25         # turns on one session before a fresh one
BACKOFF_BASE = 60.0                 # first wait after a turn that never reached
#                                     the hub (provider 429/5xx, harness crash,
#                                     a stream that ended without its terminal
#                                     event). ONE mechanism for every such
#                                     failure — see Driver._hold.
BACKOFF_MAX = 900.0                 # ceiling for the exponential backoff: a
#                                     rate-limited fleet must stop hammering,
#                                     but must still recover on its own
LONG_TURN_NOTICE = 600.0            # cadence of the "still running" line for a
#                                     turn that blocks the single-threaded loop
FAILURE_LEDGER_MAX_BYTES = 1_000_000
TURN_TIMEOUT = 3600.0               # one WORK chunk; full jobs span many chunks
RECEPTION_TURN_TIMEOUT = 3600.0     # one RECEPTION turn. Deliberately much
#: 60 MINUTES BY DEFAULT (operator ruling, 2026-08-08). It was 600s, which
#: made the hub silently unusable with local inference: a 122B model needs
#: minutes per round-trip, a boot pass is a dozen round-trips, and every
#: turn died at `no-tool-calls` having done real work. A ceiling that kills
#: a working turn and reports it as "the model did nothing" is the worst
#: shape available. Cloud harnesses finish in seconds and never approach
#: this; only the slow case is affected, and the slow case is exactly the
#: one that was broken. `--reception-timeout` still raises or lowers it.
#                                     smaller: the driver loop is BLOCKED for
#                                     this whole window and cannot re-arm, so
#                                     the value is literally "how long one
#                                     wedged turn makes this seat deaf". At
#                                     3600 a single hung harness turn muted a
#                                     seat for an hour (and `run_listen`
#                                     refuses to arm an interactive listener
#                                     behind a live driver, so both surfaces
#                                     went quiet). Triage is short by nature;
#                                     long jobs belong in a work chunk.
DRIVE_CHAIN_WAIT = 20.0             # listen window between chained work chunks:
#                                     the arm IS the receive point, so any
#                                     obligation preempts the next chunk here
DEFAULT_WORK_BUDGET = 100           # initiative chunks per rolling hour;
#                                     a light fuse for degenerate churn, not
#                                     a normal-work throttle
WORK_STRIKES = 3                    # receipt-less chunks per claim VERSION
#                                     before the chain parks (a NEW receipt =
#                                     a version bump = the reset; identical
#                                     rewrites are heartbeats, not progress)
WORK_STRIKE_TTL = 3600.0            # struck-out rows re-enter selection after
#                                     this. Strikes used to last the process
#                                     lifetime, and the only seat that would
#                                     ever bump a struck stewarded phase is
#                                     the steward the strikes had retired:
#                                     the novel fleet idled 17.5h behind
#                                     exactly that deadlock (2026-08-04)
_LISTENER_FRESH_S = 600.0           # a listen pidfile younger than this marks
#                                     a live interactive surface (tab loops
#                                     rewrite it every <=245s)
_DRIVER_STALE_S = 7200.0            # a drive pidfile older than this never
#                                     blocks anyone (reboot pid-reuse guard)
#: Failure stages that mean the turn NEVER REACHED THE HUB — the transport
#: broke (a raw spawn failure, a crashed/timed-out harness, a provider 429/5xx,
#: an MCP server that would not start). Everything NOT listed here is a
#: SEMANTIC verdict about a turn that did run: `mcp-use`/`mcp-call`/`tool`
#: (it worked but touched no Agora tool, or one call was refused) and
#: `reception` (it looked at the inbox and left debt). Only transport failures
#: may hold a WORK chunk's reception, because only they are certain to repeat.
#: Verdicts meaning "the turn reached the hub and we judged what it did".
#: A diagnosis, never a retry: the next identical turn has identical inputs.
_SEMANTIC_STAGES = frozenset({"reception", "mcp-use", "mcp-call", "tool"})
_TRANSPORT_STAGES = frozenset({None, "harness", "infrastructure", "mcp-init"})


@dataclass(frozen=True)
class TurnEvidence:
    ok: bool
    stage: str | None = None
    reason: str | None = None
    detail: str | None = None
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReceptionDebt:
    """Debt identities that a reception turn must ENGAGE.

    `to_consume` is deliberately absent. The hub's own contract for that
    ledger is "never escalates, never wakes by itself" — it is advisory
    hygiene ("someone answered you; use it or close it"). Scoring it as
    unsettled debt failed the turn of the one seat that had just done the
    orchestration right: live 2026-08-03, the delegate's reception turns were
    marked failed at 00:27:49, 00:32:53 and 02:38:58 with
    `to_consume=<answer id>` — i.e. because the seats it had dispatched work
    to had ANSWERED. A driver must not penalise a seat for the fan-in it
    organised, and must never fail a turn on a debt the hub itself refuses to
    wake anyone for.
    """

    to_answer: frozenset[str]
    structured: tuple[tuple[str, int, str, frozenset[str]], ...] = ()
    #: message id -> "channel#seq" for every to_answer row. Claim rows cite
    #: their source in either form — models overwhelmingly write the
    #: human-readable ref every doc uses — so the linked-claim excusal must
    #: match both (fund4: 8 of 11 delegate turns failed `debt-remains` while
    #: a live claim named `commons#6`).
    refs: tuple[tuple[str, str], ...] = ()

    @property
    def empty(self) -> bool:
        return not self.to_answer

    def ref_of(self, message_id: str) -> str:
        for mid, ref in self.refs:
            if mid == message_id:
                return ref
        return ""


# Issued bearer values have a long random suffix. Requiring 32+ characters
# avoids corrupting ordinary identifiers such as ``agora_protocol.py`` in a
# transcript while still covering every generated key format.
_AGORA_KEY_RE = re.compile(r"\bagora_[A-Za-z0-9_-]{32,}\b")


def _redact(text: str) -> str:
    """Keep diagnostics actionable without ever recording bearer values."""
    return _AGORA_KEY_RE.sub("agora_[REDACTED]", text)


def _one_line(text: str, *, limit: int = 500) -> str:
    """One log line, shortened VISIBLY — a driver diagnostic that stops
    mid-sentence with no marker reads as "the harness said exactly this"."""
    return elide(_redact(" ".join(text.split())), limit)


def _emit(line: str) -> None:
    print(line, flush=True)


def _harness_environment() -> dict[str, str]:
    """Return the operator environment without Agora control-plane values.

    Harness credentials belong to the configured MCP server, not to the model
    process or its shell tools.  In particular, do not let an operator's
    exported ``AGORA_API_KEY`` or ``AGORA_ADMIN_KEY`` silently reintroduce a
    CLI/direct-HTTP path.  Native harness MCP config supplies each server with
    its own explicitly scoped environment.
    """

    return {
        key: value for key, value in os.environ.items()
        if not key.startswith("AGORA_")
    }


def _json_objects(raw: str) -> list[dict]:
    text = raw.strip()
    objects: list[dict] = []
    if not text:
        return objects
    # A normal JSON document should be parsed once. Codex emits NDJSON, for
    # which whole-document parsing fails whenever there is more than one line.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return [obj]
        if isinstance(obj, list):
            return [item for item in obj if isinstance(item, dict)]
    except ValueError:
        pass
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        elif isinstance(obj, list):
            objects.extend(item for item in obj if isinstance(item, dict))
    return objects


def _find_session_id(value) -> str | None:
    if isinstance(value, dict):
        for key in ("session_id", "sessionId",
                    "conversation_id", "conversationId",
                    "thread_id", "threadId"):
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
        session = value.get("session")
        if isinstance(session, dict):
            found = _find_session_id(session)
            if found:
                return found
        for inner in value.values():
            found = _find_session_id(inner)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_session_id(item)
            if found:
                return found
    return None


#: Failure text that names the PROVIDER, not this seat: a 429, a 5xx, a dropped
#: connection. Deliberately narrow — anything vaguer would launder a real
#: harness crash into "retry forever", which is the opposite failure.
_PROVIDER_FAILURE = re.compile(
    r"rate[ _-]?limit"
    r"|429\b|\btoo many requests"
    r"|overloaded"
    r"|\b50[0234]\b|service unavailable|bad gateway|gateway time-?out"
    r"|econnreset|etimedout|connection reset|connection refused"
    r"|temporarily unavailable|insufficient[_ ]quota",
    re.I,
)


def _provider_failure(*texts: str) -> str | None:
    """The matched provider-failure phrase, or None.

    A provider failure is INFRASTRUCTURE: it says nothing about the wake, so it
    must never cost a poison strike (that is how a rate limit turned into a
    permanently deaf seat, 2026-07-31). It is retried with backoff instead.
    """
    for text in texts:
        if not text:
            continue
        found = _PROVIDER_FAILURE.search(text)
        if found:
            return found.group(0).strip()
    return None


class DriveAdapter:
    """One harness, behind a uniform contract.

    The operator's requirement is that every framework be reachable "in a
    similar way and with a similar abstraction": at minimum model and
    reasoning, plus provider where relevant. Three class declarations carry
    that, so the differences are DATA an operator can see rather than
    scattered `if harness == ...` branches:

    - `SUPPORTS`   — knobs this harness can actually express. A knob that is
                     passed but not supported is refused at ARM time, naming
                     the harness. Never accepted-and-dropped.
    - `REASONING_VOCAB` — the legal reasoning values FOR THIS HARNESS. The
                     vocabularies genuinely differ per vendor, and agora's own
                     flag spans all of them, so `--reasoning-effort max` on a
                     harness that stops at `xhigh` used to arm GREEN and then
                     fail every single wake with rc=1 — a permanently mute
                     seat that looked healthy. Validating here turns that into
                     one refused line before the loop starts.
    - `ADVISORY`   — knobs the harness accepts and forwards but cannot
                     guarantee (an OpenAI-compatible endpoint may silently
                     drop reasoning effort). Accepted, never refused, and
                     surfaced once at arm time: light safeguards, never
                     silent, never blocking.
    """

    name = "unknown"
    binary = ""
    SUPPORTS: frozenset[str] = frozenset({"model", "session"})
    REASONING_VOCAB: tuple[str, ...] = ()
    ADVISORY: frozenset[str] = frozenset()
    #: What this harness drives with when the operator names nothing. `None`
    #: means "whatever the harness itself resolves", which is right for a
    #: harness whose own default is sane. It is NOT right where the harness
    #: reads an ambient config an operator forgot: a `model = "gpt-5.6-sol"` in
    #: $CODEX_HOME/config.toml made every driven wake fail with a 400, because
    #: that model needs a newer CLI than the one installed. A driven seat is
    #: unattended, so its model must be agora's decision, not a leftover.
    HARNESS_DEFAULT_MODEL: str | None = None
    HARNESS_DEFAULT_REASONING: str | None = None
    #: Contract capabilities this harness CANNOT provide, named in the
    #: CONTRACT's vocabulary and never a vendor's. Generic code refuses on this
    #: list, so no framework's name appears anywhere in agora's validation.
    #: See docs/harness_contract.md.
    UNMET: tuple[str, ...] = ()
    #: Argv proving a non-interactive invocation terminates. No LLM call.
    PROBE_ARGV: tuple[str, ...] = ("--help",)
    #: How a turn's memory persists: "resume-id" | "state-file" | None.
    CONTINUITY: str | None = "resume-id"
    #: One word naming the machine-readable turn stream; None = exit-code-only.
    EVIDENCE: str | None = None
    #: Execution-permission levels THIS harness can express, in agora's own
    #: vocabulary — `read` (read + MCP only), `write` (write inside the
    #: workspace; the driven-seat default), `all` (explicit operator bypass).
    #: Validated exactly like REASONING_VOCAB: an inexpressible level is
    #: refused at arm time naming who supports it. This replaced a
    #: codex-shaped `--sandbox` tri-state that four of five adapters silently
    #: mistranslated — an operator asking for LESS permission could get MORE
    #: (`--sandbox disabled` on one harness produced its full-auto mode), and
    #: a bogus value reached one vendor's CLI verbatim.
    PERMISSION_VOCAB: tuple[str, ...] = ("write",)
    #: level -> argv fragment, pure data. Adapters with stateful mappings
    #: (a flag only valid on fresh sessions) override permission_argv().
    PERMISSION_ARGV: dict[str, tuple[str, ...]] = {}
    #: One sentence explaining WHY the vocabulary is narrower than agora's,
    #: appended to the refusal. Declared data, so generic validation carries a
    #: harness's rationale without naming the harness in generic code.
    PERMISSION_RATIONALE: str = ""
    #: The level a DRIVEN seat runs at when the operator names none. `None`
    #: means agora's global default (`write`). A harness whose architecture
    #: makes a driven seat non-functional below some level declares that level
    #: here — a DECLARED default printed on the ready line, which is different
    #: from silently upgrading an explicit request (an explicit lower level is
    #: still REFUSED by the vocabulary, naming the levels that exist).
    HARNESS_DEFAULT_PERMISSIONS: str | None = None
    #: How the turn reaches agora's tools:
    #:   "stdio-mcp" — agora launches its own MCP server for the turn, so the
    #:                 binding is visible in the command surface and checkable.
    #:   "external"  — the framework provides agora's tools by its own means
    #:                 (a server it already runs). agora cannot verify that
    #:                 statically and says so rather than inventing a verdict.
    TOOL_REACH: str = "stdio-mcp"
    #: "turn"    — the caller can tell each turn which seat it is (the norm).
    #: "process"  — identity is fixed for the harness/server process, so ONE
    #:              seat per process. agora can still drive it, but says so
    #:              loudly at arm time: a second seat on the same process would
    #:              post under the first one's identity.
    IDENTITY_SCOPE: str = "turn"
    #: Whether this harness's OWN default model is fit for an UNATTENDED seat.
    #: False makes agora say so once at arm time rather than let a seat look
    #: alive and settle nothing. agora states the CONSEQUENCE; it never names
    #: another product's model, which would rot on their next release.
    DEFAULT_MODEL_FIT_FOR_DRIVING: bool = True

    def __init__(self, *, model: str | None, provider: str | None = None,
                 permissions: str | None = None, cwd: Path,
                 mcp: MCPBinding, reasoning_effort: str | None = None,
                 harness_args: dict[str, str] | None = None):
        self.model = model
        self.provider = provider
        # The default chain applies HERE, not only in _make_adapter, so a
        # directly-constructed adapter (tests, embedders) can never sit on a
        # level its harness excluded and silently render an empty argv.
        self.permissions = (permissions or self.HARNESS_DEFAULT_PERMISSIONS
                            or "write")
        self.cwd = cwd
        self.mcp = mcp
        self.reasoning_effort = reasoning_effort
        # Operator-supplied, framework-specific arguments (`--harness-arg k=v`).
        # agora does not interpret them: a framework may need a concept agora
        # has no opinion about (which workflow to run, which profile to load),
        # and inventing an agora flag per vendor concept is how a protocol ends
        # up carrying a product's internals.
        self.harness_args: dict[str, str] = dict(harness_args or {})
        self.mcp_probe: MCPRuntimeProbe | None = None

    def warn_effective_model(self) -> None:
        """Say out loud when a harness will fall back to an unfit default.

        Never raises: the operator's configuration wins.
        """
        if (self.DEFAULT_MODEL_FIT_FOR_DRIVING
                or self.effective_model() != "harness-default"):
            return
        _emit(f"AGORA_DRIVE warn agent={self.mcp.agent_id} "
              f"harness={self.name} reason=no-model-resolved — this harness "
              "will fall back to its own built-in default, which is not "
              "guaranteed to sustain an agora reception pass; the seat can "
              "look alive and settle nothing. Pass --model (and --provider).")

    def effective_model(self) -> str:
        """What will REALLY answer the hub, for the `event=ready` line.

        The flag when given, else whatever the harness itself would resolve.
        Overridden where a config sidecar makes the flag an incomplete answer:
        printing `harness-default` when a sidecar actually pins a model is a
        small lie in the one line an operator reads first.
        """
        return self.model or "harness-default"

    def environment(self) -> dict[str, str]:
        """Per-seat env, merged OVER the base harness environment.

        Never carries an `AGORA_*` credential: the bearer belongs to the MCP
        server's 0600 key cache, and an ambient key in a harness process is
        how a tool ends up posting under a foreign identity.
        """
        return {}

    @classmethod
    def check_knob_combo(cls, *, model: str | None, provider: str | None,
                         reasoning_effort: str | None) -> str | None:
        """A reason to refuse this knob COMBINATION at arm time, or None.

        The per-knob vocabularies cannot see combinations. Some harnesses can
        only express a knob THROUGH another (opencode's reasoning rides the
        model's own config entry), and silently dropping the dependent knob is
        the accepted-and-dropped defect C8 exists to catch — refuse instead,
        naming the missing piece.
        """
        del model, provider, reasoning_effort
        return None

    def permission_argv(self) -> list[str]:
        """argv for this seat's permission level; pure data by default.

        Loud when the level has no mapping and mappings exist: silence here IS
        the accepted-and-dropped bug the vocabulary was built to end, and only
        callers that bypassed arm-time validation can reach it.
        """
        if self.PERMISSION_ARGV and self.permissions not in self.PERMISSION_ARGV:
            raise SystemExit(
                f"agora drive: '{self.name}' has no argv mapping for "
                f"--permissions {self.permissions} (expressible: "
                f"{'|'.join(self.PERMISSION_VOCAB)})")
        return list(self.PERMISSION_ARGV.get(self.permissions, ()))

    def extra_argv(self) -> list[str]:
        """Operator-supplied `--harness-arg k=v` pairs as `--k v`, sorted."""
        argv: list[str] = []
        for key in sorted(self.harness_args):
            argv += [f"--{key}", self.harness_args[key]]
        return argv

    def rotate_session(self, lane: str = "reception") -> None:
        """Flush this seat's harness-side context at a rotation boundary.

        `lane` is "reception" or "work" — only the lane that hit its own
        threshold is rotated. The hub holds the durable memory, so only scratch
        is lost. Base case: nothing to do — a vendor resume id dies with the
        pointer the caller already cleared. Overridden where the harness keeps
        its own state FILE, which a pointer clear does NOT rotate.
        """
        del lane
        return

    def preflight(self) -> None:
        if shutil.which(self.binary) is None:
            raise SystemExit(
                f"agora drive: selected harness '{self.name}', but "
                f"`{self.binary}` is not on PATH.")
        self.mcp_probe = probe_mcp_runtime(self.mcp.command)
        if not self.mcp_probe.ok:
            action = (
                "reinstall Agora and its supported MCP runtime with "
                "`uv tool install --force --reinstall agorahub` "
                "(development checkout: `uv tool install --force "
                "--reinstall .`), then restart this driver"
            )
            raise SystemExit(
                "agora drive: required MCP runtime failed preflight\n" +
                format_probe_failure(self.mcp_probe, action=action)
            )

    def build_command(self, prompt: str, session_id: str | None) -> list[str]:
        raise NotImplementedError

    def parse_session_id(self, raw: str, fallback: str | None) -> str | None:
        for obj in _json_objects(raw):
            found = _find_session_id(obj)
            if found:
                return found
        return fallback

    def assess_turn(self, stdout: str, stderr: str, returncode: int,
                    kind: str) -> TurnEvidence:
        del stdout, kind
        if returncode:
            return TurnEvidence(
                ok=False,
                stage="harness",
                reason="nonzero-exit",
                detail=_one_line(stderr or f"process exited {returncode}"),
            )
        return TurnEvidence(ok=True)

    def turn_notices(self, stdout: str, stderr: str) -> list[str]:
        """Operator-facing lines about what the harness DID to this turn, for
        facts that must not change the turn's verdict.

        The class this exists for: a harness silently refuses a tool and tells
        the MODEL a story about it ("the user rejected permission"). The turn
        is not a failure — the seat still reached the hub, and failing it
        would strike a seat for the operator's own configuration — but the
        operator must be able to see the refusal without reading a 30 MB
        turn log, and the seat's own account of it cannot be trusted.
        """
        del stdout, stderr
        return []

    def observed_tools(self, stdout: str) -> tuple[str, ...] | None:
        """Agora tools this (possibly PARTIAL) event stream shows were called,
        or None when this harness emits no tool evidence to read.

        Read on the timeout path, where the only question is whether the turn
        did anything at all: a turn killed at the timeout having called nothing
        never reached its provider, which is an infrastructure symptom rather
        than a poisonous wake. `EVIDENCE is None` means the harness reports
        prose only — absence of tools there proves nothing, so it stays None.
        """
        if self.EVIDENCE is None:
            return None
        with contextlib.suppress(Exception):
            return self.assess_turn(stdout, "", 0, "wake").tools
        return None


class CursorDriveAdapter(DriveAdapter):
    name = "cursor"
    binary = "cursor-agent"
    # Single-vendor, and reasoning rides the model name — no knob to forward.
    SUPPORTS = frozenset({"model", "permissions", "session"})
    # `--output-format json` returns a result envelope with no tool record, so
    # this adapter is scored on its exit code. DECLARED, not silent: an
    # undeclared evidence gap is what let the claude seat run blind for two
    # releases (see ClaudeDriveAdapter.EVIDENCE). Drop `UNMET` the moment a
    # tool-carrying stream is parsed here.
    UNMET = ("evidence",)
    PERMISSION_VOCAB = ("write", "all")
    PERMISSION_ARGV = {"write": ("--sandbox", "enabled"),
                       "all": ("--force",)}

    def build_command(self, prompt: str, session_id: str | None) -> list[str]:
        cmd = ["cursor-agent", "-p", "--output-format", "json", "--trust",
               "--approve-mcps"]
        if self.model:
            cmd += ["--model", self.model]
        cmd += self.permission_argv()
        if session_id:
            cmd += ["--resume", session_id]
        cmd.append(prompt)
        return cmd


def _mcp_result_error(result) -> str | None:
    """Return an application-level MCP failure carried in a normal result.

    FastMCP transports a tool return value successfully even when Agora's
    HTTP wrapper returns ``{"ok": false, ...}``.  Codex therefore reports
    ``status=completed`` and ``error=null`` for a request that did not
    happen.  Inspect only the result envelope (and exact JSON text blocks),
    never arbitrary message text, so semantic failure cannot look healthy.
    """

    def payload_error(value) -> str | None:
        if not isinstance(value, dict):
            return None
        if value.get("ok") is False:
            return _one_line(str(
                value.get("detail") or value.get("error")
                or "Agora request returned ok=false"
            ))
        if value.get("isError") is True or value.get("is_error") is True:
            return "MCP result was marked as an error"
        return None

    found = payload_error(result)
    if found or not isinstance(result, dict):
        return found
    structured = result.get("structured_content")
    if structured is None:
        structured = result.get("structuredContent")
    found = payload_error(structured)
    if found:
        return found
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        raw = block.get("text")
        if not isinstance(raw, str):
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        found = payload_error(payload)
        if found:
            return found
    return None


class CodexDriveAdapter(DriveAdapter):
    name = "codex"
    binary = "codex"
    SUPPORTS = frozenset({"model", "reasoning", "permissions", "session"})
    # Write-only ON PURPOSE: `all` would drop the OS sandbox, and shell network
    # access could then bypass MCP entirely — the boundary is the sandbox.
    PERMISSION_VOCAB = ("write",)
    PERMISSION_RATIONALE = ("Codex stays write-only on purpose: dropping the "
                            "OS sandbox would let shell network access bypass "
                            "MCP.")
    # Codex's CLI validates NOTHING — it carries a `Custom` passthrough and
    # forwards any string verbatim (`-c model_reasoning_effort=bogus` runs and
    # prints `reasoning effort: bogus`). So the binary's enum is NOT the gate;
    # the API is. Live probes on gpt-5.5 say: `none` rc=0; `minimal` 400
    # ("tools cannot be used with reasoning.effort 'minimal'"); `ultra` 400 —
    # and revealingly, its error names 'max', because the client translates
    # ultra->max on the wire, and max is rejected too. `models_cache.json`
    # independently advertises supported_reasoning_levels
    # ["low","medium","high","xhigh"] for every model this account can reach.
    REASONING_VOCAB = ("none", "low", "medium", "high", "xhigh")
    # Pinned deliberately: never inherit $CODEX_HOME/config.toml for an
    # unattended seat. `--model`/`--reasoning-effort` still override.
    HARNESS_DEFAULT_MODEL = "gpt-5.4"
    HARNESS_DEFAULT_REASONING = "medium"
    PROBE_ARGV = ("--version",)
    EVIDENCE = "ndjson: mcp_tool_call / turn.completed"

    def _mcp_overrides(self) -> list[str]:
        """Bind this driven seat through Codex's native per-run MCP config.

        Project config remains useful for interactive sessions. The driver
        cannot depend on project trust or global last-writer-wins state, so it
        supplies the same server contract at Codex's highest-precedence config
        layer. Only non-secret identity is present; agora-mcp reads the bearer
        key from the 0600 key cache under AGORA_HOME.
        """
        q = json.dumps
        env = ", ".join(
            f"{key}={q(value)}" for key, value in self.mcp.environment().items()
        )
        values = (
            f"mcp_servers.agora.command={q(self.mcp.command)}",
            "mcp_servers.agora.args=[]",
            "mcp_servers.agora.enabled=true",
            "mcp_servers.agora.required=true",
            'mcp_servers.agora.default_tools_approval_mode="approve"',
            f"mcp_servers.agora.env={{{env}}}",
        )
        return [part for value in values for part in ("-c", value)]

    def build_command(self, prompt: str, session_id: str | None) -> list[str]:
        # PERMISSION_VOCAB is ("write",): arm-time validation already refused
        # anything else, so no inline permission logic is needed here.
        resuming = bool(session_id)
        cmd = ["codex", "exec"]
        if resuming:
            cmd.append("resume")
        # `--skip-git-repo-check` on BOTH exec and exec-resume (live Codex
        # accepts it on each): a driven seat's workspace is the operator's
        # choice, not necessarily a git repo, and Codex otherwise refuses to
        # start with "Not inside a trusted directory" — rc=1 on EVERY wake,
        # i.e. a seat that is silently, permanently mute. The git-repo check is
        # a human "did you mean to edit here" guard; a driven seat's safety
        # boundary is the sandbox (enforced above), not repo detection.
        cmd += ["--json", "--skip-git-repo-check",
                "-c", "sandbox_workspace_write.network_access=false",
                *self._mcp_overrides()]
        if self.model:
            cmd += ["-m", self.model]
        if self.reasoning_effort:
            cmd += ["-c", "model_reasoning_effort=" +
                    json.dumps(self.reasoning_effort)]
        # Live Codex accepts `-s/--sandbox` on `codex exec`, but NOT on the
        # `codex exec resume` subcommand. A resumed thread keeps its existing
        # sandbox contract. MCP servers are launched by Codex's MCP host, not
        # through the model's shell sandbox, so no shell-network override is
        # needed or appropriate.
        if not resuming:
            cmd += ["-s", "workspace-write"]
        if resuming:
            cmd.append(session_id)
        cmd.append(prompt)
        return cmd


    @staticmethod
    def _mcp_calls(raw: str) -> tuple[list[str], list[tuple[str, str]]]:
        successful: list[str] = []
        # A model may correct a rejected call in the same turn, and may also
        # probe one invalid target after a valid call of the same tool (for
        # example describe a non-member channel). A successful use proves the
        # MCP capability worked; debt/claim verification separately proves the
        # required semantic outcome. Only tools with failures and no success
        # at all remain unresolved here.
        failed_by_tool: dict[str, str] = {}
        for event in _json_objects(raw):
            item = event.get("item")
            if not isinstance(item, dict):
                item = event
            if item.get("type") != "mcp_tool_call" or item.get("server") != "agora":
                continue
            tool = str(item.get("tool") or item.get("name") or "unknown")
            error = item.get("error")
            result_error = _mcp_result_error(
                item.get("result")
            )
            status = str(item.get("status") or "").lower()
            completed = (event.get("type") in {"item.completed", "mcp_tool_call.completed"}
                         or status in {"completed", "success", "ok"})
            if (completed and error in (None, "", False)
                    and result_error is None):
                if tool not in successful:
                    successful.append(tool)
            elif (error not in (None, "", False) or result_error is not None
                  or status in {"failed", "error"}):
                failed_by_tool[tool] = _one_line(str(
                    error or result_error or status or "tool failed"
                ))
        unresolved = [item for item in failed_by_tool.items()
                      if item[0] not in successful]
        return successful, unresolved

    @staticmethod
    def _codex_error(raw: str) -> str:
        """Extract Codex's structured failure instead of incidental stderr."""
        found = ""
        for event in _json_objects(raw):
            if event.get("type") == "error":
                found = str(event.get("message") or found)
            elif event.get("type") == "turn.failed":
                error = event.get("error")
                if isinstance(error, dict):
                    found = str(error.get("message") or error or found)
                elif error:
                    found = str(error)
        return _one_line(found) if found else ""

    def assess_turn(self, stdout: str, stderr: str, returncode: int,
                    kind: str) -> TurnEvidence:
        combined = f"{stderr}\n{stdout}"
        lowered = combined.lower()
        codex_error = self._codex_error(stdout)
        if returncode or codex_error:
            if ("required mcp servers failed to initialize" in lowered
                    or "mcp startup failed" in lowered
                    or "handshaking with mcp server" in lowered):
                stage, reason = "mcp-init", "required-server-unavailable"
            elif ("reasoning.effort" in lowered
                  and "unsupported value" in lowered):
                stage, reason = "harness-config", "model-reasoning-incompatible"
            elif "requires a newer version of codex" in lowered:
                # The configured model outruns the installed CLI — commonly a
                # `model = ...` in $CODEX_HOME/config.toml that a half-finished
                # upgrade left behind. Retrying cannot fix it, so it must be
                # fatal rather than three poison strikes and a silent quarantine.
                stage, reason = "harness-config", "model-needs-newer-codex"
            elif ("model" in lowered and "invalid_request_error" in lowered
                  and "not supported" in lowered):
                stage, reason = "harness-config", "model-unsupported"
            else:
                stage = "harness"
                reason = "turn-failed" if codex_error else "nonzero-exit"
            return TurnEvidence(
                ok=False, stage=stage, reason=reason,
                detail=(codex_error or
                        _one_line(stderr or stdout or f"process exited {returncode}")),
            )

        successful, failed = self._mcp_calls(stdout)
        if failed:
            tool, detail = failed[0]
            return TurnEvidence(ok=False, stage="mcp-call",
                                reason=f"{tool}-failed", detail=detail,
                                tools=tuple(successful))
        if not successful:
            return TurnEvidence(
                ok=False, stage="mcp-use", reason="no-agora-tool-call",
                detail="Codex exited 0 without a successful Agora MCP tool call",
            )
        if not any(event.get("type") == "turn.completed"
                   for event in _json_objects(stdout)):
            return TurnEvidence(
                ok=False, stage="harness", reason="missing-terminal-event",
                detail="Codex output ended without turn.completed",
                tools=tuple(successful),
            )
        if kind in {"boot", "wake"}:
            # Only `check_inbox` is REQUIRED — the one call that proves the
            # seat actually looked. `ack_inbox` is correctly absent when there
            # is nothing new to acknowledge, and `whoami` is absent on a resumed
            # thread that already knows who it is. Demanding the full ceremony
            # scored a CORRECT no-op reception pass as a failed turn, which
            # (before the fix in run_turn) cost a poison strike and destroyed
            # the resumable session. The AbstractCode adapter already carried
            # this exact relaxation and its reasoning: "requiring ceremonial
            # calls here caused successful work to be replayed as noise."
            # Whether the debt was really settled is proven by
            # `_verify_reception_debt` against /owed, not by tool bookkeeping.
            if "check_inbox" not in successful:
                return TurnEvidence(
                    ok=False, stage="mcp-use",
                    reason="incomplete-reception-pass",
                    detail="missing successful Agora MCP call(s): check_inbox",
                    tools=tuple(successful),
                )
        return TurnEvidence(ok=True, tools=tuple(successful))


class ClaudeDriveAdapter(DriveAdapter):
    name = "claude"
    binary = "claude"
    # Claude's thinking rides the model choice; there is no effort flag to map.
    SUPPORTS = frozenset({"model", "permissions", "session"})
    PERMISSION_VOCAB = ("write", "all")
    # THE MUTE SEAT (2026-08-04). This adapter shipped with no EVIDENCE and no
    # `assess_turn`, so it inherited the rc-only base and `EVIDENCE = None` —
    # the codebase's own marker for "this harness reports nothing". Measured
    # across the whole fleet history: 106 of 106 successful claude turns
    # recorded ZERO observability, and 12 of them made no tool call at all yet
    # scored ok. One was a reception wake whose own text read
    # "AGORA_MCP_UNAVAILABLE ... Reception pass cannot complete without agora
    # tools" — marked healthy, wake consumed, no backoff. Every `mcp-use`,
    # `mcp-call`, `mcp-init` and missing-terminal verdict was unreachable, and
    # for a WORK chunk the complete set of verdicts was `rc == 0`.
    # `stream-json` (which REQUIRES --verbose) carries the same tool_use /
    # tool_result / result events the other adapters key on.
    EVIDENCE = "ndjson: tool_use / result"

    def build_command(self, prompt: str, session_id: str | None) -> list[str]:
        # `--output-format json` returns ONE result envelope with no tool
        # record; `stream-json --verbose` emits the per-message events that
        # make a turn assessable. `--verbose` is mandatory with stream-json
        # (the CLI exits 1 without it) and `_find_session_id` still resolves
        # continuity, since every event carries `session_id`.
        cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        if self.model:
            cmd += ["--model", self.model]
        # Bind the seat's MCP server on the COMMAND LINE, exactly as the Codex
        # adapter does with `-c mcp_servers.agora.*`. A project `.mcp.json` is
        # not enough for a driven seat: Claude treats project-scoped servers as
        # untrusted until a human approves them once, and there is no human in
        # a `claude -p` turn — so the seat booted with NO agora tools, ran a
        # turn, and settled nothing. Only non-secret identity travels here;
        # agora-mcp reads the bearer from the 0600 key cache under AGORA_HOME.
        cmd += ["--mcp-config", json.dumps({"mcpServers": {"agora": {
            "command": self.mcp.command,
            "args": [],
            "env": self.mcp.environment(),
        }}})]
        # Loading the server is not enough — its TOOLS are still permission
        # gated, and a driven turn has nobody to answer the prompt. Live, the
        # seat spent a whole turn replying "I need your permission to access
        # the agora MCP tools" to a hub nobody was reading. Pre-allow exactly
        # the agora server (`mcp__agora`) and nothing else: the seat can talk
        # to its hub, while Bash/Edit/Write stay gated by the mode below.
        cmd += ["--allowedTools", "mcp__agora"]
        cmd += ["--permission-mode",
                "bypassPermissions" if self.permissions == "all" else "auto"]
        if session_id:
            cmd += ["--resume", session_id]
        cmd.append(prompt)
        return cmd

    def assess_turn(self, stdout: str, stderr: str, returncode: int,
                    kind: str) -> TurnEvidence:
        """Score a claude turn from its stream-json events (see EVIDENCE).

        Shape, probed live against claude 2.1.209:
          {"type":"system","subtype":"init","mcp_servers":[{"name","status"}]}
          {"type":"assistant","message":{"content":[{"type":"tool_use",
             "id","name":"mcp__agora__agora_post_message"}]}}
          {"type":"user","message":{"content":[{"type":"tool_result",
             "tool_use_id","is_error","content":[{"type":"text","text"}]}]}}
          {"type":"result","subtype":"success","is_error":false,...}
        """
        successful: list[str] = []
        failed: dict[str, str] = {}
        pending: dict[str, str] = {}    # tool_use id -> bare agora tool name
        mcp_down = ""
        terminal: dict[str, Any] | None = None
        for event in _json_objects(stdout):
            etype = event.get("type")
            if etype == "system" and event.get("subtype") == "init":
                for server in event.get("mcp_servers") or []:
                    if (isinstance(server, dict)
                            and server.get("name") == "agora"
                            and str(server.get("status") or "") != "connected"):
                        mcp_down = str(server.get("status") or "unavailable")
                continue
            if etype == "result":
                terminal = event
                continue
            message = event.get("message")
            content = (message.get("content")
                       if isinstance(message, dict) else None)
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = str(block.get("name") or "")
                    # MCP tools arrive as `mcp__<server>__<tool>`; only this
                    # seat's agora server counts as agora evidence.
                    if name.startswith("mcp__agora__"):
                        pending[str(block.get("id"))] = name.split("__", 2)[-1]
                elif block.get("type") == "tool_result":
                    tool = pending.pop(str(block.get("tool_use_id")), "")
                    if not tool:
                        continue
                    if block.get("is_error"):
                        failed[tool] = _one_line(str(block.get("content")))
                        continue
                    # A transport-successful call can still carry agora's
                    # {"ok": false} refusal (same class codex/opencode guard).
                    payload = block.get("content")
                    if isinstance(payload, list):
                        payload = next((p.get("text") for p in payload
                                        if isinstance(p, dict)
                                        and p.get("type") == "text"), None)
                    app_error = _mcp_result_error(payload)
                    if app_error is None:
                        try:
                            app_error = _mcp_result_error(json.loads(str(payload)))
                        except (TypeError, ValueError):
                            app_error = None
                    if app_error:
                        failed[tool] = app_error
                    elif tool not in successful:
                        successful.append(tool)

        if mcp_down:
            # rc=0 lies here: claude exits 0 with a dead MCP server, which is
            # how a seat spent a whole wake announcing it had no agora tools
            # and was scored healthy (live, 2026-08-01 editor).
            return TurnEvidence(
                ok=False, stage="mcp-init", reason="required-server-unavailable",
                detail=f"agora MCP server status={mcp_down}",
                tools=tuple(successful))
        if returncode or (terminal is not None
                          and (terminal.get("is_error")
                               or terminal.get("subtype") != "success")):
            detail = _one_line(str(
                (terminal or {}).get("result")
                or stderr or f"process exited {returncode}"))
            return TurnEvidence(
                ok=False, stage="harness",
                reason="nonzero-exit" if returncode else "turn-failed",
                detail=detail, tools=tuple(successful))
        if terminal is None:
            # The stream stopped before its terminal event: the turn was cut
            # off, not completed (codex guards the same class).
            return TurnEvidence(
                ok=False, stage="harness", reason="missing-terminal-event",
                detail="stream ended without a result event",
                tools=tuple(successful))
        unresolved = {t: d for t, d in failed.items() if t not in successful}
        if unresolved:
            tool, detail = next(iter(unresolved.items()))
            return TurnEvidence(
                ok=False, stage="mcp-call", reason=f"{tool}-failed",
                detail=f"{tool}: {detail}", tools=tuple(successful))
        if not successful:
            return TurnEvidence(
                ok=False, stage="mcp-use", reason="no-agora-tool-call",
                detail="no successful Agora MCP tool call in this turn",
                tools=())
        if kind in {"boot", "wake"} and "check_inbox" not in successful:
            # Only check_inbox is REQUIRED — the call that proves the seat
            # looked. Same relaxation and reasoning as codex/abstractcode:
            # demanding the full ceremony scores a correct no-op pass failed.
            return TurnEvidence(
                ok=False, stage="mcp-use", reason="incomplete-reception-pass",
                detail="missing successful Agora MCP call(s): check_inbox",
                tools=tuple(successful))
        return TurnEvidence(ok=True, tools=tuple(successful))


class AbstractCodeDriveAdapter(DriveAdapter):
    """Native AbstractCode headless adapter using its MCP and skill surfaces."""

    name = "abstractcode"
    binary = "abstractcode"
    SUPPORTS = frozenset({"model", "provider", "reasoning", "permissions",
                          "session"})
    # AbstractCode gates EVERY MCP tool below its bypass mode by design
    # (deny-by-default classification: MCP tools are "unknown", so they need
    # approval, and a headless run answers approval with denial — verified
    # live 2026-07-31: a `--permission-mode write` turn reached whoami and got
    # "requires approval ... and this is a headless run"). A driven seat lives
    # on MCP, so the ONLY level where it functions is the bypass mode. That is
    # a capability fact, declared: `all` is the vocabulary AND the default,
    # printed on the ready line. An explicit `--permissions write` is refused
    # with the levels that exist — never silently upgraded.
    PERMISSION_VOCAB = ("all",)
    PERMISSION_ARGV = {"all": ("--permission-mode", "full-auto")}
    HARNESS_DEFAULT_PERMISSIONS = "all"
    # AbstractCode's own set (`abstractcode exec --reasoning`), which matches
    # abstractcore's thinking vocabulary: auto|none|minimal|low|medium|high|
    # xhigh. No `ultra`, no `max`.
    REASONING_VOCAB = ("auto", "none", "minimal", "low", "medium", "high",
                       "xhigh")
    # An OpenAI-compatible endpoint (airelay, ollama, LM Studio) frequently
    # cannot enforce effort scaling and says so only on the harness's stderr.
    # Accepted and forwarded, but never asserted as guaranteed.
    ADVISORY = frozenset({"reasoning"})
    CONTINUITY = "state-file"
    EVIDENCE = "ndjson: tool_result / final"
    # Its built-in default is a small local model chosen for cheap interactive
    # use, not for sustaining an unattended reception pass.
    DEFAULT_MODEL_FIT_FOR_DRIVING = False

    def _lane_state_path(self, lane: str) -> Path:
        return _config.home() / (
            f"drive-{self.mcp.agent_id}.abstractcode.{lane}.state.json"
        )

    def effective_model(self) -> str:
        if self.model:
            return self.model
        path = self._lane_state_path("reception").with_suffix(".config.json")
        try:
            cfg = json.loads(path.read_text())
        except (OSError, ValueError):
            return "harness-default"
        if isinstance(cfg, dict) and cfg.get("model"):
            return f"{cfg['model']} (from {path.name})"
        return "harness-default"

    def rotate_session(self, lane: str = "reception") -> None:
        """AbstractCode's memory is the `--state-file`, not a vendor resume id.

        Clearing agora's session POINTER therefore rotated nothing: the state
        file carried the same `session_id` forever and context grew without
        bound (there is no headless self-compaction). Unlink the state file and
        keep the `.config.json` sidecar, which holds provider/model and the
        MCP server block — AbstractCode then mints a fresh session on the next
        turn with the seat's configuration intact.

        Only the lane that hit its threshold is rotated: unlinking the other
        lane's file while a turn holds it open would let that turn's final
        state write recreate it, silently un-rotating it.

        The `.state.d/` run ledgers are deliberately left alone. They are turn
        evidence, and quietly deleting an operator's audit trail to reclaim
        disk is not a trade agora makes silently.
        """
        path = self._lane_state_path(lane)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            _emit(f"AGORA_DRIVE warn=rotate-failed agent={self.mcp.agent_id} "
                  f"harness=abstractcode lane={lane} path={path} "
                  f"detail={exc!r} — context did NOT rotate; this seat's "
                  "transcript keeps growing")

    def _state_file(self, prompt: str) -> Path:
        lane = "work" if prompt.startswith("AGORA WORK CHUNK") else "reception"
        base = _config.home() / (
            f"drive-{self.mcp.agent_id}.abstractcode.{lane}.state.json"
        )
        config_path = base.with_suffix(".config.json")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if config_path.exists():
            try:
                loaded = json.loads(config_path.read_text())
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, ValueError):
                pass
        existing.setdefault("mcp_servers", {})["agora"] = {
            "transport": "stdio",
            "command": [self.mcp.command],
            "env": self.mcp.environment(),
        }
        # Persist provider/model into the sidecar too. AbstractCode resolves
        # these flag > config > env > builtin, so writing them here keeps a
        # driven seat on the operator's chosen model across every lane and
        # every restart instead of silently reverting to the builtin.
        if self.provider:
            existing["provider"] = self.provider
        if self.model:
            existing["model"] = self.model
        config_path.write_text(json.dumps(existing, indent=2) + "\n")
        config_path.chmod(0o600)
        return base

    def build_command(self, prompt: str, session_id: str | None) -> list[str]:
        del session_id  # persistence is the named state file, not a vendor id
        cmd = [
            "abstractcode", "exec", "--json",
            "--state-file", str(self._state_file(prompt)),
            *self.permission_argv(),
            "--skill", "agora-channels",
        ]
        if self.provider:
            cmd += ["--provider", self.provider]
        if self.model:
            cmd += ["--model", self.model]
        if self.reasoning_effort:
            cmd += ["--reasoning", self.reasoning_effort]
        cmd.append(prompt)
        return cmd

    def parse_session_id(self, raw: str, fallback: str | None) -> str | None:
        del raw
        return fallback or "state-file"

    @staticmethod
    def _agora_tool(name: str) -> str | None:
        normalized = name.replace("__", "::")
        marker = "::agora::"
        if marker in normalized:
            return normalized.rsplit("::", 1)[-1]
        if normalized.startswith("agora_"):
            return normalized[len("agora_"):]
        return None

    def assess_turn(self, stdout: str, stderr: str, returncode: int,
                    kind: str) -> TurnEvidence:
        if returncode:
            return TurnEvidence(
                ok=False, stage="harness", reason="nonzero-exit",
                detail=_one_line(stderr or stdout or f"process exited {returncode}"),
            )
        successful: list[str] = []
        final = None
        for event in _json_objects(stdout):
            if event.get("event") == "tool_result" and event.get("success", True):
                tool = self._agora_tool(str(event.get("tool") or ""))
                # A transport-successful call can still carry agora's
                # {"ok": false} refusal (same class the codex adapter guards).
                if tool and _mcp_result_error(event.get("result")) is None \
                        and tool not in successful:
                    successful.append(tool)
            if event.get("event") == "final":
                final = event
        if not isinstance(final, dict) or final.get("status") != "completed":
            detail = (final or {}).get("error") if isinstance(final, dict) else None
            return TurnEvidence(
                ok=False, stage="harness", reason="missing-successful-final",
                detail=_one_line(str(detail or stderr or "no completed final event")),
                tools=tuple(successful),
            )
        if not successful:
            return TurnEvidence(
                ok=False, stage="mcp-use", reason="no-agora-tool-call",
                detail="AbstractCode completed without a successful Agora MCP call",
            )
        if kind in {"boot", "wake"}:
            # check_inbox is the one meaningful reception primitive.  Any
            # successful authenticated Agora call already proves identity, and
            # ack_inbox is correctly unnecessary when there is nothing new to
            # acknowledge.  Semantic debt verification below proves that real
            # asks were answered or durably claimed; requiring ceremonial
            # calls here caused successful work to be replayed as noise.
            required = ("check_inbox",)
            missing = [name for name in required if name not in successful]
            if missing:
                return TurnEvidence(
                    ok=False, stage="mcp-use",
                    reason="incomplete-reception-pass",
                    detail="missing successful Agora MCP call(s): " + ",".join(missing),
                    tools=tuple(successful),
                )
        return TurnEvidence(ok=True, tools=tuple(successful))


class OpencodeDriveAdapter(DriveAdapter):
    """opencode headless adapter (`opencode run`).

    Two live findings (2026-07-31, 20 real runs) shape every line here:

    1. `opencode run` does NOT take its workspace from the spawned process's
       cwd — it resolves the parent shell's $PWD. A driven turn therefore ran
       in the WRONG directory: no project config, no AGENTS.md, and the
       operator's provider came back "Model not found". `--dir` with an
       absolute path is mandatory, not cosmetic.
    2. A tool whose permission evaluates to `ask` is AUTO-REJECTED headlessly
       and the process still exits 0 ("permission requested: agora_whoami;
       auto-rejecting"). A seat like that looks healthy forever and settles
       nothing — so the permission block is pinned per level below, and
       `assess_turn` treats a rejected agora tool as a failed turn instead of
       trusting the exit code.
    3. Out-of-workspace access is a SEPARATE permission named
       `external_directory`, and it is SYNTACTIC, not containment (probed
       2026-08-01, 22 runs, `--permissions write`). It fires only when
       opencode's shell parser can statically resolve a path outside `--dir`
       from an argument of a command it recognises as path-taking, or from
       the explicit path parameter of the read/write/edit tools:

           DENIED   cat/touch/cp/mkdir <outside>, read tool, write tool
           ALLOWED  echo hi > <outside>/f      (redirection, not an argv path)
                    sh -c 'mkdir <outside>/d'  (one wrapper defeats the parse)
                    nohup <outside>/bin/x ... &
                    <outside>/bin/x <outside>/f
                    python3 -c "open('<outside>/f','w')"   (path in a string)

       So `--permissions write` does NOT confine a seat to its workspace: an
       out-of-workspace file really lands through any indirect form. What the
       gate reliably does is stop the TIDY forms — which is a real safeguard
       against an absent-minded write, and nothing like a sandbox. It is
       pinned explicitly at every level below rather than inherited from
       opencode's default, because a default that changes under agora would
       change what an operator's `--permissions` word means.

       It is also the loudest failure agora has ever mis-narrated: opencode
       reports the auto-reject to the model as "The user rejected permission
       to use this specific tool call", so the seat concludes the OPERATOR
       refused it and files a false blocker (live 2026-08-01: a seat spent
       ~40 minutes and one blocked claim on it). `turn_notices` therefore
       says out loud what actually happened, on every turn where it happens.
    4. opencode persists session/project state in a USER-GLOBAL SQLite store
       under XDG data. Four seats booting together on one machine can crash
       before the first tool call with `database is locked`, which means the
       failure never even reaches the hub. The driven seat must pin opencode's
       data/cache/state roots to a stable PER-WORKSPACE tree so turns keep
       continuity without contending with other seats.
    """

    name = "opencode"
    binary = "opencode"
    SUPPORTS = frozenset({"model", "provider", "reasoning", "permissions",
                          "session"})
    PERMISSION_VOCAB = ("read", "write", "all")
    # opencode forwards effort verbatim to the endpoint; the endpoint is the
    # gate, so this is the OpenAI-compatible ladder agora already uses.
    REASONING_VOCAB = ("none", "minimal", "low", "medium", "high", "xhigh")
    # `--variant high` never reached an OpenAI-compatible provider (the wire
    # still said reasoning_effort=medium). Effort is expressible ONLY through
    # the model's `options.reasoningEffort`, written into the per-run config
    # below — and such an endpoint may still drop it.
    ADVISORY = frozenset({"reasoning"})
    PROBE_ARGV = ("--version",)
    CONTINUITY = "resume-id"
    EVIDENCE = "ndjson: tool_use / step_finish"
    # Its resolved default model comes from the operator's GLOBAL config — the
    # same ambient-leftover trap the codex adapter pins against. agora cannot
    # guess a provider id, so it warns instead of inventing one.
    DEFAULT_MODEL_FIT_FOR_DRIVING = False

    #: agora's levels -> opencode's `permission` block. MCP tools are matched
    #: by glob on their exposed names (`agora_*`), verified live in both the
    #: allow and deny directions.
    #: `external_directory` is stated at every level, never inherited: see
    #: finding 3 above. `all` already covers it through `*`, and says it
    #: anyway so the three rows can be read against each other.
    _PERMISSION = {
        "read": {"bash": "deny", "edit": "deny", "write": "deny",
                 "webfetch": "deny", "websearch": "deny",
                 "external_directory": "deny", "agora*": "allow"},
        "write": {"bash": "allow", "edit": "allow", "write": "allow",
                  "webfetch": "deny", "websearch": "deny",
                  "external_directory": "deny", "agora*": "allow"},
        "all": {"*": "allow", "external_directory": "allow",
                "agora*": "allow"},
    }

    #: opencode's headless auto-reject line, e.g.
    #: `! permission requested: external_directory (/a/*, /b/*); auto-rejecting`
    _REJECT_RE = re.compile(
        r"permission requested:\s*(?P<name>\S+)\s*"
        r"(?:\((?P<patterns>[^)]*)\))?\s*;\s*auto-rejecting")

    @classmethod
    def check_knob_combo(cls, *, model: str | None, provider: str | None,
                         reasoning_effort: str | None) -> str | None:
        if provider and not model:
            return ("opencode expresses the provider inside the model "
                    "(`-m provider/model`); --provider without --model has "
                    "nothing to attach to. Pass --model too.")
        if reasoning_effort and not model:
            return ("opencode expresses reasoning effort through the model's "
                    "own config entry (`options.reasoningEffort`); "
                    "--reasoning-effort without --model has nowhere to go. "
                    "Pass --model too.")
        return None

    def effective_model(self) -> str:
        """What will REALLY answer the hub.

        opencode resolves an unflagged model from its config layers, and the
        GLOBAL layer is the operator's ambient leftover — on 2026-07-31 that
        was a free tier, which rate-limited eight driven seats into silence at
        04:59. The workspace file is the one layer agora itself writes, so read
        it and say the answer out loud; a model resolvable only from the global
        layer still reads as `harness-default` and trips the unfit-default
        warning, which is exactly the truth an operator needs.
        """
        if self.model:
            return self.model
        path = Path(self.cwd) / "opencode.json"
        try:
            cfg = json.loads(path.read_text())
        except (OSError, ValueError):
            return "harness-default"
        if isinstance(cfg, dict) and isinstance(cfg.get("model"), str) and cfg["model"]:
            return f"{cfg['model']} (from {path.name})"
        return "harness-default"

    def _provider_and_model(self) -> tuple[str | None, str | None]:
        """(provider, bare model) — derived from `provider/model` when the
        operator used opencode's qualified form, so a knob that must attach to
        the model's config entry never silently misses it."""
        model = self.model
        provider = self.provider
        if model and "/" in model and not provider:
            provider, _, model = model.partition("/")
        elif model and "/" in model:
            _, _, model = model.partition("/")
        return provider, model

    def _run_config(self) -> dict:
        """The per-run config layer (`OPENCODE_CONFIG_CONTENT`): opencode's
        highest-precedence layer, and a DEEP merge over global+project — so an
        operator's provider endpoint survives while agora adds only what a
        driven seat requires."""
        cfg: dict = {
            "mcp": {"agora": {
                "type": "local",
                "command": [self.mcp.command],
                "enabled": True,
                "timeout": 30000,
                # Non-secret identity only; agora-mcp reads the bearer from
                # the 0600 key cache under AGORA_HOME.
                "environment": self.mcp.environment(),
            }},
            "permission": dict(self._PERMISSION[self.permissions]),
        }
        provider, model = self._provider_and_model()
        if self.reasoning_effort and provider and model:
            cfg["provider"] = {provider: {"models": {model: {
                "options": {"reasoningEffort": self.reasoning_effort}}}}}
        return cfg

    def _state_roots(self) -> tuple[Path, Path, Path]:
        """Per-workspace XDG roots for opencode's own mutable local state.

        Keep the workspace stable across turns so the seat can resume its own
        opencode sessions, while preventing several seats from fighting over
        the operator's one global opencode.db.
        """
        root = Path(self.cwd).resolve() / ".agora" / "opencode"
        data = root / "data"
        cache = root / "cache"
        state = root / "state"
        for path in (data, cache, state):
            path.mkdir(parents=True, exist_ok=True)
        return data, cache, state

    def environment(self) -> dict[str, str]:
        # opencode has no argv equivalent of codex's `-c`; its per-run layer
        # is this env var. Riding it here also puts the binding in the surface
        # `agora harness-check` inspects (C4/C5/C8).
        data, cache, state = self._state_roots()
        return {
            "OPENCODE_CONFIG_CONTENT": json.dumps(self._run_config()),
            "XDG_DATA_HOME": str(data),
            "XDG_CACHE_HOME": str(cache),
            "XDG_STATE_HOME": str(state),
        }

    def build_command(self, prompt: str, session_id: str | None) -> list[str]:
        cmd = ["opencode", "run",
               # NOT optional: without it the turn runs in whatever $PWD the
               # parent shell had, silently losing the workspace, the project
               # config and AGENTS.md.
               "--dir", str(Path(self.cwd).resolve()),
               # opencode otherwise spends a whole extra model round-trip just
               # to mint a session title before the real turn. On free/shared
               # tiers that extra request is often the difference between a
               # seat that starts and one that hits provider 429 before its
               # first tool call.
               "--title", f"agora:{self.mcp.agent_id}:{'resume' if session_id else 'turn'}",
               "--format", "json"]
        model = self.model
        if model and self.provider and "/" not in model:
            model = f"{self.provider}/{model}"
        if model:
            cmd += ["-m", model]
        if session_id:
            cmd += ["--session", session_id]
        cmd += self.extra_argv()
        cmd.append(prompt)
        return cmd

    def parse_session_id(self, raw: str, fallback: str | None) -> str | None:
        for obj in _json_objects(raw):
            found = obj.get("sessionID")
            if isinstance(found, str) and found:
                return found
        return super().parse_session_id(raw, fallback)

    def turn_notices(self, stdout: str, stderr: str) -> list[str]:
        """Name every tool call opencode refused, on either refusal path.

        Neither path is visible to an operator otherwise: a refused `bash`
        does not fail an opencode turn (only a refused agora tool does), so
        the driver log stays green while the seat gets stuck. And what the
        MODEL is told is not what happened —

          ask  -> "The user rejected permission to use this specific tool
                   call." A sentence NO user typed. A live seat believed it,
                   burned ~40 minutes, and filed a blocked claim asking the
                   operator for permission the operator had already granted
                   (2026-08-01).
          deny -> "The user has specified a rule which prevents you..." —
                   true, and the reason agora pins `external_directory`
                   rather than leaving it on opencode's `ask` default.
        """
        seen: dict[str, list[str]] = {}
        for m in self._REJECT_RE.finditer(stderr or ""):
            patterns = seen.setdefault(f"ask:{m.group('name')}", [])
            for pattern in (m.group("patterns") or "").split(","):
                if pattern.strip() and pattern.strip() not in patterns:
                    patterns.append(pattern.strip())
        for event in _json_objects(stdout or ""):
            part = event.get("part")
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            # Substring, not prefix: the sentence is opencode's wording and
            # a reworded prefix must not silently switch this notice off.
            if "specified a rule which prevents you" not in str(
                    state.get("error") or ""):
                continue
            calls = seen.setdefault(f"rule:{part.get('tool') or 'unknown'}", [])
            call = _one_line(str((state.get("input") or {}).get("command")
                                 or (state.get("input") or {}).get("filePath")
                                 or ""))[:120]
            if call and call not in calls:
                calls.append(call)
        notices = []
        for key, details in sorted(seen.items()):
            path, _, name = key.partition(":")
            shown = f" {'paths' if path == 'ask' else 'calls'}=" \
                    f"{' | '.join(details)}" if details else ""
            if path == "ask":
                what = (f"permission={name} was left on opencode's `ask` "
                        "default and every ask is auto-rejected headlessly — "
                        "the model was told a USER refused it, which no user "
                        "did")
            else:
                what = (f"tool={name} was refused by the permission block "
                        f"agora writes for --permissions {self.permissions}")
            notices.append(
                f"AGORA_DRIVE warn=harness-refused-tool harness=opencode "
                f"level={self.permissions}{shown} — {what}. To allow it: "
                "raise this seat to --permissions all, or keep the work "
                f"inside {Path(self.cwd).resolve()}.")
        return notices

    def assess_turn(self, stdout: str, stderr: str, returncode: int,
                    kind: str) -> TurnEvidence:
        del kind
        successful: list[str] = []
        failed: dict[str, str] = {}
        harness_error = ""
        for event in _json_objects(stdout):
            if event.get("type") == "error":
                err = event.get("error") or {}
                harness_error = _one_line(str(
                    (err.get("data") or {}).get("message")
                    or err.get("name") or err))
                continue
            part = event.get("part")
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            tool = str(part.get("tool") or "unknown")
            if not tool.startswith("agora"):
                continue
            state = (part.get("state")
                     if isinstance(part.get("state"), dict) else {})
            status = str(state.get("status") or "").lower()
            if status == "completed":
                # A transport-successful call can still carry agora's
                # {"ok": false} refusal — a 403'd post_message must not score
                # as a healthy pass (same class the codex adapter guards).
                app_error = _mcp_result_error(state.get("output"))
                if app_error is None:
                    try:
                        app_error = _mcp_result_error(
                            json.loads(str(state.get("output"))))
                    except (TypeError, ValueError):
                        app_error = None
                if app_error:
                    failed[tool] = app_error
                elif tool not in successful:
                    successful.append(tool)
            elif status in {"error", "failed"}:
                # rc=0 lies here: a permission auto-reject lands EXACTLY like
                # this, and a turn that never reached the hub must not be
                # reported as a healthy pass.
                failed[tool] = _one_line(str(state.get("error") or status))

        if returncode or harness_error:
            lowered = f"{stderr}\n{stdout}".lower()
            # A provider failure carried in `harness_error` is re-staged
            # centrally (Driver._classify_provider_failure) for every harness.
            if "mcp" in lowered and ("failed" in lowered
                                     or "connect" in lowered):
                stage, reason = "mcp-init", "required-server-unavailable"
            elif "providermodelnotfounderror" in lowered:
                # Almost always a missing `--dir`, or a provider declared in a
                # config layer this run never loaded.
                stage, reason = "harness-config", "model-not-resolved"
            else:
                stage, reason = "harness", "nonzero-exit"
            return TurnEvidence(
                ok=False, stage=stage, reason=reason,
                detail=harness_error or _one_line(
                    stderr or f"process exited {returncode}"),
                tools=tuple(successful))

        unresolved = {t: d for t, d in failed.items() if t not in successful}
        if unresolved:
            tool, detail = next(iter(unresolved.items()))
            return TurnEvidence(
                ok=False, stage="tool", reason="agora-tool-rejected",
                detail=f"{tool}: {detail}", tools=tuple(successful))
        return TurnEvidence(ok=True, tools=tuple(successful))


class PiDriveAdapter(DriveAdapter):
    """pi headless adapter (`pi -p --mode json`).

    pi ships NO MCP client — a stated design choice ("No MCP." — its README),
    not a gap. So agora supplies the client: a small extension
    (`agora/pi_ext/agora.js`) spawns `agora-mcp`, enumerates its tools over
    JSON-RPC, and re-registers each as a native pi tool. Verified live
    2026-07-31: all 43 agora tools surfaced and a model called `whoami`
    through them, with the hub answering as the right seat.

    Two live constraints:

    * The extension MUST spawn the MCP subprocess in `session_start` and
      dispose it in `session_shutdown`. Spawning in the extension factory left
      node's event loop non-empty and `pi -p` hung forever — a 240s timeout on
      a turn whose work had actually completed.
    * pi enforces nothing at tool time: no sandbox, no approval gate, by
      design ("Run in a container"). agora's `write` on pi is therefore
      genuinely unsandboxed, and `read` (built-ins off, agora tools only) is
      the level a pure reception seat should prefer.
    """

    name = "pi"
    binary = "pi"
    SUPPORTS = frozenset({"model", "provider", "reasoning", "permissions",
                          "session"})
    # No `all`: pi has no elevated mode distinct from its default, and
    # declaring a level the harness cannot express is exactly the
    # accepted-and-dropped knob C8 exists to catch.
    PERMISSION_VOCAB = ("read", "write")
    PERMISSION_ARGV = {
        # Built-ins off, extension (= agora) tools kept: verified 43/43.
        "read": ("--no-builtin-tools",),
        "write": (),
    }
    # pi's own ladder (`--thinking`), verified to reach the wire as
    # reasoning_effort. pi has `max`; the OpenAI ladder does not — the
    # provider is the final gate either way.
    REASONING_VOCAB = ("off", "minimal", "low", "medium", "high", "xhigh",
                       "max")
    # `permissions` is ADVISORY here: pi enforces NOTHING at tool time (no
    # sandbox, no approval gate — its docs say "run in a container"). agora's
    # `write` on pi is a declaration, not containment, and the ready line must
    # not let it read as one.
    ADVISORY = frozenset({"reasoning", "permissions"})
    PROBE_ARGV = ("--version",)
    CONTINUITY = "resume-id"          # `--session-id`, and the id is OURS
    EVIDENCE = "ndjson: tool_execution_end / agent_settled"
    TOOL_REACH = "stdio-mcp"          # via the extension agora ships
    # pi's built-in default provider needs a cloud key this seat may not
    # have; an unattended seat must not discover that one wake at a time.
    DEFAULT_MODEL_FIT_FOR_DRIVING = False

    @staticmethod
    def _extension_path() -> Path:
        """The MCP bridge agora ships alongside its Python package."""
        return Path(__file__).resolve().parent / "pi_ext" / "agora.js"

    def environment(self) -> dict[str, str]:
        env = {
            # Read by the bridge extension, not by pi itself.
            "AGORA_MCP_COMMAND": self.mcp.command,
            # pi discovers extensions/settings/sessions under this root, so
            # each seat gets its own and never inherits an operator's ~/.pi.
            "PI_CODING_AGENT_DIR": str(
                (_config.home() / f"pi-{self.mcp.agent_id}").resolve()),
            # No update pings or catalog fetches on an unattended wake.
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
        }
        # Non-secret identity for the bridge's MCP subprocess; the bearer
        # stays in the 0600 key cache (the bridge forces empty key vars).
        env.update(self.mcp.environment())
        env["AGORA_API_KEY"] = ""
        env["AGORA_ADMIN_KEY"] = ""
        return env

    def build_command(self, prompt: str, session_id: str | None) -> list[str]:
        cmd = ["pi", "-p", "--mode", "json",
               # Project-local settings/extensions are ignored
               # non-interactively unless trust is explicit; a driven seat
               # must not depend on a trust decision no human is present to
               # make.
               "--approve",
               # --no-extensions is LOAD-BEARING (verified live): with
               # --approve alone, pi loads the workspace's .pi/extensions/*.js
               # TOO — the bridge ran twice (two agora-mcp children, every
               # tool registered twice), and worse, ANY project-local JS
               # executed inside the pi process before tool policy applies. A
               # driven seat writes files on peer instruction, so a dropped
               # .pi/extensions/x.js would execute on the next wake. The
               # explicit -e below survives --no-extensions.
               "--no-extensions",
               "-e", str(self._extension_path())]
        model = self.model
        if model and self.provider and "/" not in model:
            model = f"{self.provider}/{model}"
        if model:
            cmd += ["--model", model]
        elif self.provider:
            # No model named: the provider must still reach the turn (`pi
            # --provider` exists) — dropping it silently ran the seat on pi's
            # built-in default provider, the accepted-and-dropped defect.
            cmd += ["--provider", self.provider]
        if self.reasoning_effort:
            cmd += ["--thinking", self.reasoning_effort]
        cmd += self.permission_argv()
        if session_id:
            # pi CREATES the id if absent, so agora owns the namespace and a
            # resume can never silently fork into a fresh transcript.
            cmd += ["--session-id", session_id]
        cmd += self.extra_argv()
        cmd.append(prompt)
        return cmd

    def parse_session_id(self, raw: str, fallback: str | None) -> str | None:
        for obj in _json_objects(raw):
            if obj.get("type") == "session" and isinstance(obj.get("id"), str):
                return obj["id"]
        return super().parse_session_id(raw, fallback)

    def assess_turn(self, stdout: str, stderr: str, returncode: int,
                    kind: str) -> TurnEvidence:
        del kind
        successful: list[str] = []
        failed: dict[str, str] = {}
        settled = False
        for event in _json_objects(stdout):
            etype = event.get("type")
            if etype in {"agent_settled", "agent_end"}:
                settled = True
            if etype != "tool_execution_end":
                continue
            tool = str(event.get("toolName") or "unknown")
            if not tool.startswith("agora"):
                continue
            if event.get("isError"):
                failed[tool] = _one_line(json.dumps(event.get("result"))[:300])
            else:
                app_error = _mcp_result_error(event.get("result"))
                if app_error:
                    failed[tool] = app_error
                elif tool not in successful:
                    successful.append(tool)

        if returncode:
            lowered = f"{stderr}\n{stdout}".lower()
            if "mcp" in lowered and "timeout" in lowered:
                stage, reason = "mcp-init", "required-server-unavailable"
            else:
                stage, reason = "harness", "nonzero-exit"
            return TurnEvidence(
                ok=False, stage=stage, reason=reason,
                detail=_one_line(stderr or f"process exited {returncode}"),
                tools=tuple(successful))

        unresolved = {t: d for t, d in failed.items() if t not in successful}
        if unresolved:
            tool, detail = next(iter(unresolved.items()))
            return TurnEvidence(ok=False, stage="tool",
                                reason="agora-tool-failed",
                                detail=f"{tool}: {detail}",
                                tools=tuple(successful))
        if not settled:
            # pi emits agent_settled last on a healthy turn. Its absence at
            # rc=0 means the stream was truncated — usually a resource holding
            # the event loop open until the driver's own timeout fired.
            return TurnEvidence(ok=False, stage="harness",
                                reason="stream-truncated",
                                detail="no agent_settled in the event stream",
                                tools=tuple(successful))
        return TurnEvidence(ok=True, tools=tuple(successful))


class UndrivableHarness(DriveAdapter):
    """A harness agora can wire IN-SESSION but not drive yet.

    It declares which CONTRACT capabilities it lacks and carries no vendor
    knowledge whatsoever — the only thing agora knows about such a framework is
    the same handful of words it would say about any other.
    """

    binary = ""

    def build_command(self, prompt: str, session_id: str | None) -> list[str]:
        raise SystemExit(
            f"agora drive: '{self.name}' declares unmet contract capabilities "
            f"({', '.join(self.UNMET)}); no turn can be built.")


class AbstractCodeTuiDriveAdapter(DriveAdapter):
    """AbstractCode-TUI: a headless `exec` turn whose tools run server-side.

    Verified 2026-07-30 with a real hub turn (check_inbox -> post_message ->
    ack_inbox, hub receipt confirmed) and NO code change in any package. Two
    contract facts differ from every other harness here, and both are declared
    rather than assumed:

    - Its tools are reached through its own server, not through a stdio MCP
      server agora launches. `tool-reach` is therefore satisfied by the
      OPERATOR's server configuration, which agora neither writes nor inspects.
    - Identity is PROCESS-scoped: the turn cannot say which seat it is, so the
      server's configured identity is the one that posts. agora drives it and
      says so loudly, because a second seat on the same server process would
      post under the first one's name.

    Which workflow to run is the operator's choice, supplied with
    `--harness-arg workflow=<value>`; agora has no opinion about another
    product's workflows and will not carry one.
    """

    name = "abstractcode-tui"
    binary = "abstractcode-tui"
    SUPPORTS = frozenset({"model", "provider", "reasoning", "permissions",
                          "session"})
    PERMISSION_VOCAB = ("read", "write", "all")
    PERMISSION_ARGV = {"read": ("--permissions", "read"),
                       "write": ("--permissions", "write"),
                       "all": ("--permissions", "all")}
    # `all` is the only level a live hub turn has been verified at
    # (2026-07-30); its tools execute behind its own server's policy, so this
    # is a declared default, not a sandbox bypass on this machine.
    HARNESS_DEFAULT_PERMISSIONS = "all"
    REASONING_VOCAB = ("none", "minimal", "low", "medium", "high", "xhigh",
                       "auto", "on")
    ADVISORY = frozenset({"reasoning"})
    PROBE_ARGV = ("--version",)
    CONTINUITY = "resume-id"          # `--session <id>`, caller-chosen
    EVIDENCE = None                  # prose only; no --json yet
    IDENTITY_SCOPE = "process"
    TOOL_REACH = "external"
    UNMET = ("evidence",)
    DEFAULT_MODEL_FIT_FOR_DRIVING = False

    def build_command(self, prompt: str, session_id: str | None) -> list[str]:
        cmd = [self.binary, "exec", prompt,
               "--workspace", str(self.cwd), "--workspace-mode",
               "workspace_only", *self.permission_argv(),
               "--timeout", str(int(getattr(self, "_reception_timeout",
                                            None) or RECEPTION_TURN_TIMEOUT))]
        if self.model:
            cmd += ["--model", self.model]
        if self.provider:
            cmd += ["--provider", self.provider]
        if self.reasoning_effort:
            cmd += ["--reasoning", self.reasoning_effort]
        if session_id:
            cmd += ["--session", session_id]
        # The operator's own framework choices (notably `workflow=...`).
        cmd += self.extra_argv()
        return cmd

    def parse_session_id(self, raw: str, fallback: str | None) -> str | None:
        # A caller-chosen id: agora keeps the one it passed, and mints one on
        # the first turn so continuity survives a driver restart.
        return fallback or f"agora-{self.mcp.agent_id}"


#: Capabilities with no degraded mode: a seat cannot exist without them.
#: Everything else in the contract degrades to a NAMED limitation.
_HARD_CAPABILITIES = ("single-turn", "tool-reach", "identity")

#: Harnesses agora can spawn a turn for — all seven declared harnesses.
#: (`abstractcode-tui` drives with named limitations: process-scoped identity,
#: external tool reach.)
_DRIVE_ADAPTERS: dict[str, type[DriveAdapter]] = {
    "cursor": CursorDriveAdapter,
    "codex": CodexDriveAdapter,
    "claude": ClaudeDriveAdapter,
    "abstractcode": AbstractCodeDriveAdapter,
    "abstractcode-tui": AbstractCodeTuiDriveAdapter,
    "opencode": OpencodeDriveAdapter,
    "pi": PiDriveAdapter,
}


#: The pre-0.12.60 `--sandbox` tri-state, mapped to the permission level it
#: always meant. Kept for one release so existing driver invocations survive.
_LEGACY_SANDBOX_TO_PERMISSIONS = {"enabled": "write", "disabled": "all",
                                  "none": "all"}


def _resolve_permissions(permissions: str | None,
                         sandbox: str | None) -> str | None:
    """The OPERATOR's requested level, or None when they named none: the new
    flag wins and the legacy tri-state maps. Defaults (per-harness, else
    `write`) are applied by the caller, so a declared harness default can
    never override an explicit request."""
    if permissions:
        return permissions
    if sandbox:
        return _LEGACY_SANDBOX_TO_PERMISSIONS.get(sandbox, sandbox)
    return None


def _effective_permissions(harness: str | None, permissions: str | None,
                           sandbox: str | None) -> str:
    """Explicit request > legacy alias > harness's declared default > write."""
    cls = _DRIVE_ADAPTERS.get(harness or "")
    return (_resolve_permissions(permissions, sandbox)
            or (cls.HARNESS_DEFAULT_PERMISSIONS if cls else None)
            or "write")


def _validate_drive_request(harness: str | None, *, model: str | None,
                            provider: str | None,
                            reasoning_effort: str | None,
                            permissions: str = "write") -> None:
    """Refuse an impossible drive request BEFORE anything is armed.

    Called from both `run_drive` (earliest possible point, so the operator is
    never sent to fix workspace wiring for a request that could not work) and
    `_make_adapter` (so direct API callers get the same contract). A `None` or
    "auto" harness is validated later, once resolution has picked one.
    """
    if harness in (None, "auto"):
        return
    cls = _DRIVE_ADAPTERS.get(harness)
    if cls is None:
        return                      # unknown name: _make_adapter reports it
    hard = [c for c in cls.UNMET if c in _HARD_CAPABILITIES]
    if hard:
        # No vendor name appears here. A harness DECLARES what it cannot do, in
        # the contract's own vocabulary, and generic code reports it — so this
        # message is as useful to the fifth framework as to the first.
        raise SystemExit(
            f"agora drive: the '{harness}' harness does not meet the agora "
            "harness contract for a DRIVEN seat.\n"
            "  unmet: " + ", ".join(hard) + "\n"
            f"  Run `agora harness-check {harness}` for the per-item verdict "
            "(docs/harness_contract.md).\n"
            "  A seat on this harness still works IN-SESSION: "
            f"`agora setup <id> --harness {harness}`.")
    for knob, flag, value in (("provider", "--provider", provider),
                              ("reasoning", "--reasoning-effort",
                               reasoning_effort)):
        if value and knob not in cls.SUPPORTS:
            able = sorted(name for name, a in _DRIVE_ADAPTERS.items()
                          if knob in a.SUPPORTS)
            raise SystemExit(
                f"agora drive: {flag} is not supported by the '{harness}' "
                f"harness (supported by: {', '.join(able) or 'none'})")
    if permissions and permissions not in cls.PERMISSION_VOCAB:
        # Same mechanism as reasoning: an inexpressible level is refused NOW,
        # naming the levels that exist — never accepted-and-mistranslated.
        # (The old `--sandbox` tri-state let an operator asking for LESS
        # permission silently get MORE on some harnesses.)
        raise SystemExit(
            f"agora drive: the '{harness}' harness accepts --permissions "
            f"{'|'.join(cls.PERMISSION_VOCAB)} — got '{permissions}'."
            + (f" {cls.PERMISSION_RATIONALE}" if cls.PERMISSION_RATIONALE
               else ""))
    combo_refusal = cls.check_knob_combo(model=model, provider=provider,
                                         reasoning_effort=reasoning_effort)
    if combo_refusal:
        raise SystemExit(f"agora drive: {combo_refusal}")
    if reasoning_effort and reasoning_effort not in cls.REASONING_VOCAB:
        # Refuse NOW rather than arm green and fail every wake: agora's flag
        # spans several vendors' vocabularies, so `max` on a harness that stops
        # at `xhigh` produced a seat that reported status=ok at boot and then
        # died rc=1 on every single turn — permanently mute, and healthy in the
        # one line the operator reads first.
        raise SystemExit(
            f"agora drive: the '{harness}' harness accepts --reasoning-effort "
            f"{'|'.join(cls.REASONING_VOCAB)} — got '{reasoning_effort}'. "
            "Agora's flag spans several vendors' vocabularies; this value is "
            "not in this harness's.")


def _make_adapter(harness: str, *, model: str | None,
                  provider: str | None, permissions: str | None = None,
                  sandbox: str | None = None,
                  cwd: Path, mcp: MCPBinding,
                  reasoning_effort: str | None = None,
                  harness_args: dict[str, str] | None = None) -> DriveAdapter:
    adapters = _DRIVE_ADAPTERS
    level = _effective_permissions(harness, permissions, sandbox)
    _validate_drive_request(harness, model=model, provider=provider,
                            reasoning_effort=reasoning_effort,
                            permissions=level)
    try:
        cls = adapters[harness]
    except KeyError as exc:
        raise SystemExit(f"agora drive: unsupported harness '{harness}'") from exc
    # An unattended seat's model is agora's decision, never a leftover in the
    # harness's own ambient config. An explicit flag still wins.
    model = model or cls.HARNESS_DEFAULT_MODEL
    reasoning_effort = reasoning_effort or cls.HARNESS_DEFAULT_REASONING
    return cls(model=model, provider=provider, permissions=level, cwd=cwd,
               mcp=mcp, reasoning_effort=reasoning_effort,
               harness_args=harness_args)


class Driver:
    """One seat's reception loop. Stateful across wakes: the harness session
    id (for resume/state), the per-hour turn budget, the consecutive-failure
    backoff (the ONE failure mechanism — see _hold), and the session-rotation
    counter."""

    def __init__(self, agent_id: str, hub: str, *, harness: str = "cursor",
                 model: str | None = DEFAULT_MODEL,
                 provider: str | None = None,
                 reasoning_effort: str | None = None,
                 harness_args: dict[str, str] | None = None,
                 max_wait: float = DEFAULT_MAX_WAIT,
                 permissions: str | None = None, sandbox: str | None = None,
                 turn_budget: int = DEFAULT_TURN_BUDGET,
                 broadcast_turn_budget: int = DEFAULT_BROADCAST_TURN_BUDGET,
                 session_rotate: int = DEFAULT_SESSION_ROTATE,
                 work_timeout: float = TURN_TIMEOUT,
                 reception_timeout: float = RECEPTION_TURN_TIMEOUT,
                 work_budget: int = DEFAULT_WORK_BUDGET,
                 force: bool = False,
                 turn_log: str | None = None,
                 cwd: Path | None = None,
                 spawn=None) -> None:
        self.agent_id = agent_id
        self.hub = hub
        self.harness = harness
        self.model = model
        self.provider = provider
        self.reasoning_effort = reasoning_effort
        self.max_wait = max_wait
        self.permissions = _effective_permissions(harness, permissions,
                                                  sandbox)
        self.turn_budget = turn_budget
        self.broadcast_turn_budget = broadcast_turn_budget
        self.session_rotate = session_rotate
        # Cap: a chunk longer than half the driver-staleness bound would
        # let a second driver "take over" mid-chunk (review F8).
        self.work_timeout = min(work_timeout, _DRIVER_STALE_S / 2)
        self.reception_timeout = max(60.0, float(reception_timeout))
        self.work_budget = work_budget
        self.force = force
        self.cwd = Path(cwd) if cwd else Path.cwd()
        identity = resolve_workspace_identity(self.cwd, harness=harness) or {}
        self._mcp_binding = MCPBinding(
            command=resolve_mcp_command(),
            agent_id=agent_id,
            url=hub,
            home=_config.home(),
            about=identity.get("AGORA_ABOUT", ""),
            download_dir=(os.environ.get("AGORA_DOWNLOAD_DIR")
                          or identity.get("AGORA_DOWNLOAD_DIR")),
        )
        self._adapter = _make_adapter(harness, model=model, provider=provider,
                                      permissions=self.permissions,
                                      harness_args=harness_args,
                                      cwd=self.cwd, mcp=self._mcp_binding,
                                      reasoning_effort=reasoning_effort)
        # The adapter spawns the turn, so it needs the ceiling the operator
        # set — a local model can need far longer than cloud latency.
        self._adapter._reception_timeout = self.reception_timeout
        # `spawn` is injectable so the loop is unit-testable without a real
        # harness process: spawn(prompt, session_id) -> (new_session_id|None, ok).
        self._spawn = spawn or self._spawn_turn
        # Debt verification asks the HUB what is still owed after a turn, so
        # it is only meaningful when the turn really talked to one. Explicit
        # rather than inferred from the spawn identity, so an end-to-end
        # harness can drive a scripted seat against a real hub and still get
        # the real verdict path.
        self.verify_reception_debt = spawn is None
        home = _config.home()
        # Protocol-v2 sessions deliberately ignore the old shared
        # drive-<id>.session file. Reception and initiative have different
        # contracts and must never train or resume each other's histories.
        if harness == "cursor":
            # Keep the original Cursor filenames so existing driven seats
            # resume their live sessions across upgrades; non-Cursor
            # harnesses get qualified names so session ids never cross.
            self._reception_session_path = home / f"drive-{agent_id}.reception-v2.session"
            self._work_session_path = home / f"drive-{agent_id}.work-v2.session"
            self._work_claim_path = home / f"drive-{agent_id}.work-v2.claim"
        else:
            self._reception_session_path = (
                home / f"drive-{agent_id}.{harness}.reception-v2.session")
            self._work_session_path = (
                home / f"drive-{agent_id}.{harness}.work-v2.session")
            self._work_claim_path = home / f"drive-{agent_id}.{harness}.work-v2.claim"
        # THE driver-ownership signal (2026-07-28 unification): while this
        # file holds a LIVE pid, `agora listen` refuses to arm a second
        # reception surface for the seat, the stop hook stays quiet, and
        # `agora status` shows a `driver` column — one file, one truth.
        self._drive_pid_path = home / f"drive-{agent_id}.pid"
        # Always written, never opt-in: the only durable record a silently
        # failing driver leaves behind.
        self._failures_path = home / f"drive-{agent_id}.failures.jsonl"
        self.reception_session_id = self._read_session(self._reception_session_path)
        self.work_session_id = self._read_session(self._work_session_path)
        self._reception_turns_on_session = 0
        self._work_turns_on_session = 0
        self._work_claim_ref = self._read_session(self._work_claim_path)
        self._turn_times: list[float] = []       # spawn timestamps in the last hour
        self._broadcast_turn_times: list[float] = []  # unowned room-wide wakes
        # THE ONE failure mechanism: consecutive turns that never reached the
        # hub, and when the next one may run. No second ledger, nothing on
        # disk, nothing that can outlive the process that observed it.
        self._fail_streak = 0
        self._retry_at = 0.0
        self._fail_reason = ""                    # one word for the state line
        # The OPERATOR's ceiling, not the constant: --reception-timeout must
        # reach every harness, not only the one adapter that reads it as an
        # argv flag. Shipping it half-wired made the knob a lie — accepted,
        # documented, and silently ignored (2026-08-08).
        self._turn_timeout = self.reception_timeout  # work turns raise it
        self._work_times: list[float] = []        # work-chunk spawns, rolling hour
        self._work_strikes: dict[str, int] = {}   # claim-version -> receipt-less chunks
        self._work_strike_at: dict[str, float] = {}  # claim-version -> last strike ts
        self._has_work = False                    # last KNOWN continuation answer
        self._scan_ok = False                     # ...and whether it was READ
        self._pending_wake = False                # a wake we could not run YET
        self._pending_wake_has_debt = False       # addressed/owed beats broadcast fuse
        self._reception_debt_before: ReceptionDebt | None = None
        self._reception_debt_verification_required = False
        self._last_turn_ok: bool | None = None
        # Stage of the most recent failed turn. "reception" alone means the
        # turn REACHED the hub and left debt (a diagnosis); every other stage
        # means it did not get there, and only those back off.
        self._last_turn_stage: str | None = None
        self._last_turn_detail: str = ""
        # The flight recorder (--turn-log, 2026-07-28): the FULL event
        # stream of every spawned turn, appended as JSONL. Off by default;
        # "default" resolves beside the seat's other driver state. Contents
        # are the model's own transcript stream — operator eyes only, so
        # the file is clamped 0600 like the notify files.
        self._turn_log: Path | None = None
        if turn_log:
            self._turn_log = (home / f"drive-{agent_id}.turns.jsonl"
                              if turn_log == "default"
                              else Path(turn_log).expanduser())
            if not self._turn_log.is_absolute():
                # A relative path resolves against the driver's cwd — the
                # seat's own WORKSPACE, where the sandboxed agent can read
                # (and commit) its own transcript. Deliberate use stays
                # possible; silence does not (review F5).
                _emit(f"AGORA_DRIVE warn=turn-log-in-workspace "
                      f"agent={agent_id} path={self._turn_log} "
                      "(relative: lands in the seat's own cwd)")
        self._turn_log_warned = False
        self._turn_log_secured = False

    # -- one driver per seat (the ownership file) ------------------------------

    def _acquire_drive_pid(self) -> int:
        """One driver per seat: refuse while a LIVE driver holds the pidfile
        (double drivers double every turn and fork the --resume session); a
        dead or ancient holder (crash, reboot with pid reuse) is taken over
        silently. A LIVE holder refuses even under --force — two live
        drivers is never what anyone wants, and the previous driver never
        re-reads the file, so an overwrite would RUN ALONGSIDE it, not
        replace it (review F1): stopping the live one is the operator's
        act. Returns the previous holder's pid (0 if none) so the
        foreign-listener check can ignore that driver's own embedded
        listen pidfile."""
        try:
            pid = int(self._drive_pid_path.read_text().strip() or "0")
            mtime = self._drive_pid_path.stat().st_mtime
        except (OSError, ValueError):
            pid, mtime = 0, 0.0
        recent = (time.time() - mtime) < _DRIVER_STALE_S
        if pid > 0 and pid != os.getpid() and pid_alive(pid) and recent:
            raise SystemExit(
                f"agora drive: a driver for '{self.agent_id}' is already "
                f"running (pid {pid}, {self._drive_pid_path}). One driver "
                f"per seat — stop that one yourself (kill {pid}) and "
                "re-run; --force cannot take over a LIVE driver (it would "
                "run alongside it, doubling every turn).")
        with contextlib.suppress(OSError):
            self._drive_pid_path.write_text(str(os.getpid()))
        return pid

    def _touch_drive_pid(self) -> None:
        with contextlib.suppress(OSError):
            os.utime(self._drive_pid_path)

    def _clear_drive_pid(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            if int(self._drive_pid_path.read_text().strip() or "0") == os.getpid():
                self._drive_pid_path.unlink()

    def _clear_stale_owed_signature(self) -> None:
        """A fresh driver instance must not inherit another reception owner's
        debt wake watermark.

        `listen-<id>.owedsig` only proves that some prior listener/driver
        already ANNOUNCED a debt wake. If that owner died or restarted before
        the spawned turn actually drained the inbox, carrying the file forward
        suppresses the startup backlog wake forever: the debt is still owed in
        the hub, but the new driver sees an "unchanged" signature and sleeps
        until a newer message or an escalation-band flip arrives.

        Clear only the debt signature here. The notify-file offset remains
        valuable and still prevents replaying stale non-debt traffic.
        """
        sig_path = _config.home() / f"listen-{self.agent_id}.owedsig"
        with contextlib.suppress(OSError):
            sig_path.unlink()

    def _preflight_spawner(self) -> None:
        """Refuse a missing harness binary AT ARM, not at the first 3am wake
        (the old FileNotFoundError killed the driver with the obligation
        undelivered). Injected spawns skip it."""
        if self._spawn != self._spawn_turn:
            return
        self._adapter.preflight()

    def _check_foreign_listener(self, prev_driver_pid: int = 0) -> None:
        """An interactive tab's listener and a driver share the seat's
        offset/owedsig files and starve each other (the dual-surface
        hazard). A listen pidfile that is FRESH marks a live interactive
        surface even when its recorded pid is momentarily dead (tab loops
        re-exec every ~245s): refuse a live one, warn on a fresh-but-dead
        one, ignore stale ones — and ignore the PREVIOUS DRIVER's own
        embedded listen (its pidfile carries the driver pid; without the
        exemption a crashed driver's fresh listen pidfile would misdiagnose
        as an interactive tab, review F1)."""
        pidfile = _config.home() / f"listen-{self.agent_id}.pid"
        try:
            pid = int(pidfile.read_text().strip() or "0")
            age = time.time() - pidfile.stat().st_mtime
        except (OSError, ValueError):
            return
        if (pid <= 0 or pid == os.getpid() or pid == prev_driver_pid
                or age > _LISTENER_FRESH_S):
            return
        if pid_alive(pid) and not self.force:
            raise SystemExit(
                f"agora drive: an interactive listener for "
                f"'{self.agent_id}' is live (pid {pid}, touched "
                f"{age:.0f}s ago) — two reception surfaces on one seat "
                "starve each other (shared offset/owedsig). Close that "
                "session or its reception shell and retry (a just-closed "
                "tab drains within ~4 min), or pass --force if you know "
                "it is gone.")
        if not pid_alive(pid):
            _emit(f"AGORA_DRIVE warn=recent-listener agent={self.agent_id} "
                  "(a listener surface was active moments ago; if an "
                  "interactive tab is open for this seat, close it)")

    # -- persistence ---------------------------------------------------------

    @staticmethod
    def _read_session(path: Path) -> str | None:
        try:
            return path.read_text().strip() or None
        except OSError:
            return None

    @staticmethod
    def _write_session(path: Path, sid: str | None) -> None:
        try:
            if sid:
                path.write_text(sid)
            elif path.exists():
                path.unlink()
        except OSError:
            pass

    # -- the ONE failure mechanism: backoff ----------------------------------

    def _note_failure(self, reason: str, detail: str) -> float:
        """A turn did not reach the hub. Space the next attempt, keep the wake.

        Every such failure — a 429, a 5xx, a harness crash, a stream that
        ended without its terminal event — is about the PATH to the hub, not
        about the obligation waiting at the other end. So there is one
        response: wait longer, say so, and try the same wake again. Nothing is
        ever dropped, and one healthy turn clears the whole streak.

        Callers set `_pending_wake` BEFORE calling, because the line says
        whether a wake is being held and a work chunk holds none.
        """
        self._fail_streak += 1
        self._fail_reason = reason
        wait = min(BACKOFF_BASE * (2 ** (self._fail_streak - 1)), BACKOFF_MAX)
        self._retry_at = time.time() + wait
        kept = ("wake=held — the turn never reached the hub; the obligation "
                "is kept and still escalates hub-side."
                if self._pending_wake else
                "wake=none — a work chunk never reached the hub; no "
                "obligation was pending, and the next chunk is spaced too.")
        _emit(f"AGORA_DRIVE state=backoff agent={self.agent_id} "
              f"reason={reason} consecutive={self._fail_streak} "
              f"next={wait:.0f}s {kept} "
              f"detail={_one_line(detail, limit=200) or 'none'}")
        return wait

    def _clear_backoff(self) -> None:
        if self._fail_streak:
            _emit(f"AGORA_DRIVE recovered agent={self.agent_id} "
                  f"after={self._fail_streak} failed turn(s) "
                  f"reason={self._fail_reason or 'unknown'}")
        self._fail_streak = 0
        self._retry_at = 0.0
        self._fail_reason = ""

    def _backoff_retry_after(self) -> float:
        return max(0.0, self._retry_at - time.time())

    def _hold(self, *, has_debt: bool) -> tuple[str, str, float] | None:
        """Why a reception turn cannot run RIGHT NOW: (state, reason, next).

        The single admissibility predicate — run_turn refuses on it, the loop
        holds the wake on it, and the state line prints it. Two holds exist
        and they are genuinely different questions: `backoff` is "the path to
        the hub is broken", `parked` is "this seat has spent its hourly
        allowance". Both are bounded and both name the second they release.
        """
        retry = self._backoff_retry_after()
        if retry > 0:
            return "backoff", (self._fail_reason or "harness-failing"), retry
        now = self._prune_turn_times()
        hard = self._retry_after(self._turn_times, self.turn_budget, now)
        if hard > 0:
            return "parked", "turn-budget", hard
        if not has_debt:
            fuse = self._retry_after(self._broadcast_turn_times,
                                     self.broadcast_turn_budget, now)
            if fuse > 0:
                return "parked", "broadcast-budget", fuse
        return None

    def _state(self, state: str, *, reason: str = "",
               next_s: float | None = None, **extra) -> None:
        """One line per loop pass naming where the seat IS and what unblocks it.

        This is the whole observability contract: a stall is diagnosable from
        stdout without a turn log, a debugger, or a hub query.
        """
        parts = [f"AGORA_DRIVE state={state}", f"agent={self.agent_id}"]
        if reason:
            parts.append(f"reason={reason}")
        if next_s is not None:
            parts.append(f"next={next_s:.0f}s")
        parts += [f"{k}={v}" for k, v in extra.items() if v not in (None, "")]
        _emit(" ".join(parts))

    @contextlib.contextmanager
    def _long_turn_notice(self, kind: str):
        """Announce a blocking turn, and repeat every LONG_TURN_NOTICE.

        The loop is single-threaded: for this whole span the seat cannot arm its
        listener and is deaf to every obligation. A work chunk may hold that for
        --work-timeout, so the blindness is at least VISIBLE. (Making reception
        concurrent with a chunk needs a second thread — not this pass.)
        """
        done = threading.Event()
        t0 = time.time()
        _emit(f"AGORA_DRIVE turn-start agent={self.agent_id} kind={kind} "
              f"timeout={self._turn_timeout:.0f}s — reception is BLOCKED "
              "until it returns (single-threaded loop)")

        def tick() -> None:
            while not done.wait(LONG_TURN_NOTICE):
                _emit(f"AGORA_DRIVE turn-running agent={self.agent_id} "
                      f"kind={kind} elapsed={time.time() - t0:.0f}s — this "
                      "seat is deaf to obligations until it returns")

        watcher = threading.Thread(target=tick, daemon=True)
        watcher.start()
        try:
            yield
        finally:
            done.set()

    # -- the failure ledger (always on) --------------------------------------

    def _record_failure(self, *, kind: str, stage: str | None,
                        reason: str | None, detail: str) -> None:
        """Append one failed turn to `drive-<id>.failures.jsonl`.

        UNCONDITIONAL, unlike --turn-log: the 2026-07-31 outage had to be
        reconstructed from attempt-file byte ladders and hook mtimes because a
        driver that fails every turn for four hours left no durable trace at
        all. Best-effort and size-capped — recording must never break a turn.
        """
        line = json.dumps({
            "ts": round(time.time(), 3), "agent": self.agent_id,
            "harness": self.harness, "kind": kind,
            "stage": stage or "unknown", "reason": reason or "unknown",
            "detail": _redact(_one_line(detail, limit=200)),
        }, ensure_ascii=False)
        try:
            if self._failures_path.exists():
                size = self._failures_path.stat().st_size
                if size + len(line) > FAILURE_LEDGER_MAX_BYTES:
                    # Truncate oldest, by BYTES: a line-count cap cannot bound
                    # a file whose lines are unbounded. The first surviving
                    # line is dropped because it is probably a fragment.
                    keep = FAILURE_LEDGER_MAX_BYTES // 2
                    _, _, tail = self._failures_path.read_bytes()[-keep:] \
                        .partition(b"\n")
                    self._failures_path.write_bytes(tail)
                    os.chmod(self._failures_path, 0o600)
            fd = os.open(self._failures_path,
                         os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            pass

    # -- budget --------------------------------------------------------------

    def _prune_turn_times(self, now: float | None = None) -> float:
        """Prune both rolling windows using one clock boundary."""
        now = time.time() if now is None else now
        self._turn_times = [
            t for t in self._turn_times if now - t < TURN_BUDGET_WINDOW
        ]
        self._broadcast_turn_times = [
            t for t in self._broadcast_turn_times
            if now - t < TURN_BUDGET_WINDOW
        ]
        return now

    @staticmethod
    def _retry_after(times: list[float], limit: int, now: float) -> float:
        if len(times) < limit:
            return 0.0
        if not times:
            return TURN_BUDGET_WINDOW
        return max(0.0, min(times) + TURN_BUDGET_WINDOW - now)

    def _wake_admissible(self, *, has_debt: bool) -> bool:
        """A reception turn may run now. The negation is _hold's reason."""
        return self._hold(has_debt=has_debt) is None

    # -- the flight recorder (--turn-log) --------------------------------------

    @staticmethod
    def _prompt_kind(prompt: str) -> str:
        if prompt.startswith("AGORA WAKE"):
            return "wake"
        if prompt.startswith("AGORA WORK CHUNK"):
            return "work"
        return "boot"

    def _log_lines(self, lines: list[str]) -> None:
        """Best-effort JSONL append: recording must NEVER break a turn.
        A failure warns ONCE (the operator asked for these logs; silent
        loss would be worse than the noise) and the turn proceeds.

        Written the notify-sink way (review F2/F3): O_CREAT at 0600 (no
        world-readable create window), fchmod once per process (repairs a
        pre-existing looser file — transcripts carry peer content and tool
        output, operator eyes only), and one os.write PER LINE so every
        line lands atomically under O_APPEND (a custom path shared across
        seats may still interleave BLOCKS, never tear a line)."""
        if self._turn_log is None or not lines:
            return
        try:
            fd = os.open(self._turn_log,
                         os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                if not self._turn_log_secured:
                    os.fchmod(fd, 0o600)
                    self._turn_log_secured = True
                for line in lines:
                    safe = _redact(line)
                    os.write(fd, (safe if safe.endswith("\n")
                                  else safe + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            if not self._turn_log_warned:
                self._turn_log_warned = True
                _emit(f"AGORA_DRIVE warn=turn-log-unwritable "
                      f"agent={self.agent_id} path={self._turn_log}")

    def _log_event(self, **fields) -> None:
        if self._turn_log is None:
            return  # recorder off = zero work, not even the dumps
        self._log_lines([json.dumps(fields, ensure_ascii=False)])

    # -- the spawn (real) ----------------------------------------------------

    def _reception_debt(self) -> ReceptionDebt | None:
        """Current trusted hub debt IDs, or None when they are unknowable."""

        _, _, raw = _owed_snapshot(self.hub, self.agent_id)
        if raw is None:
            return None
        return ReceptionDebt(
            to_answer=frozenset(
                str(row.get("id")) for row in raw.get("to_answer", [])
                if isinstance(row, dict) and row.get("id")
            ),
            refs=tuple(
                (str(row.get("id")), f"{row.get('channel')}#{row.get('seq')}")
                for row in raw.get("to_answer", [])
                if isinstance(row, dict) and row.get("id")
                and row.get("channel") and row.get("seq")
            ),
            structured=tuple(
                (
                    str(row.get("channel")),
                    int(row.get("seq", 0)),
                    str(row.get("id")),
                    frozenset(str(v) for v in row.get("pending_asks", []) if v),
                )
                for row in raw.get("to_answer", [])
                if isinstance(row, dict) and row.get("channel") and row.get("seq")
                and row.get("id")
                and row.get("pending_asks")
            ),
        )

    def _message_pending_asks(self, channel: str, seq: int,
                              message_id: str) -> frozenset[str] | None:
        """This seat's still-pending ask ids on one source message.

        `pending_asks` on the hub row is MESSAGE-global — every ask still
        open, whoever owns it (`/owed` says so itself: "Replying at all drops
        the row (the remaining debt is other seats')"). So a decomposition
        that fans four addressed asks to four seats leaves three pending the
        instant any one seat answers its own, and scoring that global set as
        THIS seat's unsettled debt failed the turn — and muted the seat — for
        doing exactly the addressed work it was handed. Live at-test#446 hit
        it on 5 of 7 seats at once. Narrow to the asks whose per-ask `to`
        (0077) names this seat; an ask addressed to nobody is everyone's.
        """
        api_key = _config.get_cached_key(self.hub, self.agent_id)
        if not api_key:
            return None
        import urllib.parse
        import httpx
        try:
            row = httpx.get(
                f"{self.hub.rstrip('/')}/channels/"
                f"{urllib.parse.quote(channel, safe='')}/messages/by-seq/{seq}",
                headers={"Authorization": f"Bearer {api_key}"}, timeout=5.0,
            ).json()
            if str(row.get("id") or "") != message_id:
                return None
            pending = frozenset(
                str(v) for v in (row.get("pending_asks") or []))
            asks = ((row.get("data") or {}).get("asks") or [])
            addressed = frozenset(
                str(a.get("id")) for a in asks if isinstance(a, dict)
                and a.get("id") is not None
                and (not a.get("to") or self.agent_id in (a.get("to") or []))
            )
            # No structured asks at all: the whole message is the obligation,
            # exactly as before. Structured asks present: only mine count.
            return pending & addressed if asks else pending
        except Exception:
            return None

    def _verify_reception_debt(self, evidence: TurnEvidence,
                               kind: str) -> TurnEvidence:
        """Report debt that was owed BEFORE this turn and still is.

        Hub semantics already encode the right abstraction: an original
        ``to_answer`` row disappears after any valid reply/claim/refusal.
        Comparing IDs avoids parsing model prose and ignores new debt arriving
        mid-turn.

        This verdict is a DIAGNOSIS, never a penalty (see run_turn): the turn
        reached the hub and looked. Re-spawning it changes no input, and the
        live record shows what that costs — on 2026-08-03 every `debt-remains`
        verdict respawned an identical turn within the same second (00:26:34,
        00:27:49, 00:30:58, 00:32:10, 01:00:52, 01:03:55), and while that
        held wake sat there the seat was also barred from running the work
        chunk the debt was actually asking for.
        """

        # VERIFICATION FAILS OPEN. Every `verification-unavailable` path below
        # means the hub did not answer a post-hoc GET (restart, slow response,
        # 5s timeout, missing cached key) — that is a fact about the NETWORK,
        # never evidence about the agent's turn. Scoring it as a failed turn
        # laundered transient hub blips into permanent deafness: a failure
        # bumps the poison ledger, and 3 strikes quarantine the wake key
        # forever (live: 18 quarantined keys on one seat, 6 on another).
        before = self._reception_debt_before
        if (kind not in {"boot", "wake"}
                or not self._reception_debt_verification_required):
            return evidence
        if before is None:
            _emit(f"AGORA_DRIVE verify agent={self.agent_id} status=skipped "
                  "reason=no-owed-snapshot-before-turn")
            return evidence
        if before.empty:
            return evidence
        after = self._reception_debt()
        if after is None:
            _emit(f"AGORA_DRIVE verify agent={self.agent_id} status=skipped "
                  "reason=owed-unreadable-after-turn")
            return evidence
        unanswered = sorted(before.to_answer & after.to_answer)
        linked_sources: set[str] | None = None
        if unanswered:
            # A standing row whose work this seat has CLAIMED is not an
            # ignored debt — since 2026-08-04 an operator commission stays
            # in to_answer until the completion report (`resolved`), by
            # design, so "row survived the turn" is the CORRECT state for a
            # delegate mid-delivery ONLY AFTER the delegate has engaged the
            # operator thread itself. The claim (any non-done status;
            # blocked-on-operator included) is the receipt that the plan is
            # materialized; without it the row still fails the turn.
            linked_sources = self._linked_claim_sources()
            unanswered = [i for i in unanswered
                          if i not in linked_sources
                          and before.ref_of(i) not in linked_sources]
        unresolved_structured: list[str] = []
        for channel, seq, message_id, original_pending in before.structured:
            pending = self._message_pending_asks(channel, seq, message_id)
            if pending is None:
                # Fails open, as above: an unreadable message row is a hub
                # fact, not a verdict on the agent.
                _emit(f"AGORA_DRIVE verify agent={self.agent_id} "
                      f"status=skipped reason=asks-unreadable "
                      f"ref={channel}/{message_id}")
                continue
            if original_pending & pending:
                if linked_sources is None:
                    linked_sources = self._linked_claim_sources()
                if (message_id not in linked_sources
                        and f"{channel}#{seq}" not in linked_sources):
                    unresolved_structured.append(message_id)
        if not unanswered and not unresolved_structured:
            return evidence
        parts = []
        if unanswered:
            parts.append("to_answer=" + ",".join(unanswered[:10]))
        if unresolved_structured:
            parts.append(
                "pending_without_linked_claim=" + ",".join(unresolved_structured[:10])
            )
        return TurnEvidence(
            ok=False, stage="reception", reason="debt-remains",
            detail="original debt remains after turn: " + " ".join(parts),
            tools=evidence.tools,
        )

    @staticmethod
    def _classify_provider_failure(evidence: TurnEvidence,
                                   stderr: str) -> TurnEvidence:
        """Re-stage a failed turn as INFRASTRUCTURE when the endpoint named
        itself — for every harness, in one place.

        Only one of five adapters used to do this, in its own rc!=0 branch, so
        the same 429 was `infrastructure` under opencode and `harness` under
        the rest. Read only the harness's own words (stderr and the detail the
        adapter extracted), never model prose, so a turn that merely mentions
        a rate limit is not laundered into an infra excuse.
        """
        if evidence.ok or evidence.stage in ("infrastructure", "harness-config"):
            return evidence
        provider = _provider_failure(stderr, evidence.detail or "")
        if not provider:
            return evidence
        return dataclasses.replace(
            evidence, stage="infrastructure", reason="provider-failure",
            detail=f"{provider}: {evidence.detail or 'provider failure'}")

    def _spawn_cursor_agent(self, prompt: str, session_id: str | None):
        """Compatibility shim for the legacy Cursor-only tests/callers."""
        return self._spawn_turn(prompt, session_id)

    def _is_delegate_seat(self) -> bool:
        """Does this seat hold a live delegation? Read from the hub, never
        remembered: powers change without the seat taking a turn."""
        api_key = _config.get_cached_key(self.hub, self.agent_id)
        if not api_key:
            return False
        import httpx
        try:
            me = httpx.get(f"{self.hub}/whoami", timeout=5.0,
                           headers={"Authorization": f"Bearer {api_key}"}).json()
        except Exception:
            return False
        return any(d.get("agent_id") == self.agent_id
                   for d in (me.get("delegations") or []))

    def _spawn_turn(self, prompt: str, session_id: str | None):
        """Run ONE headless harness turn. Returns (session_id, ok)."""
        cmd = self._adapter.build_command(prompt, session_id)
        kind = self._prompt_kind(prompt)
        t0 = time.time()
        # turn_start BEFORE the spawn: a wedged turn still shows it began.
        self._log_event(event="turn_start", ts=round(t0, 3),
                        agent=self.agent_id, kind=kind,
                        harness=self.harness,
                        session=session_id, model=self.model)
        try:
            # KNOWN, MEASURED LIMIT: this timeout kills the direct child, then
            # blocks in communicate() while any grandchild still holds the
            # pipes. Live record 2026-07-30..08-03: 3 turns out of 1338 outran
            # their cap that way (worst: a work chunk capped at 3600s ran
            # 5069s; a reception turn capped at 600s ran 3052s). The seat is
            # deaf for the overrun, so _long_turn_notice announces every turn
            # at LONG_TURN_NOTICE intervals. Closing it for real means giving
            # the child a file instead of a pipe, which changes capture for
            # every adapter — a deliberate change, not a side effect of this
            # pass.
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self._turn_timeout, cwd=str(self.cwd),
                                  stdin=subprocess.DEVNULL,
                                  env=self._harness_env())
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            err = exc.stderr or ""
            if isinstance(err, bytes):
                err = err.decode("utf-8", "replace")
            sid = self._adapter.parse_session_id(out, session_id)
            # Said on the timeout path too: a seat that spent the whole window
            # retrying a call the harness kept refusing looks identical to a
            # hung provider, and the refusal is the actual diagnosis.
            for notice in self._adapter.turn_notices(out, err):
                _emit(f"{notice} agent={self.agent_id} kind={kind}")
            # A turn killed at the timeout having called NOTHING never reached
            # its provider — the 2026-07-31 shape, where every seat booted a
            # session, made zero tool calls, and was killed at 600s. Naming it
            # turns a four-hour silent outage into a one-line diagnosis, and
            # routes it to backoff rather than to the poison ledger.
            tools = self._adapter.observed_tools(out)
            starved = tools is not None and not tools
            stage = "infrastructure" if starved else "harness"
            reason = "no-tool-calls" if starved else "timeout"
            detail = _one_line(err) or (
                f"timed out after {self._turn_timeout:.0f}s"
                + (f" with no agora tool call "
                   f"(model={self._adapter.effective_model()})"
                   if starved else ""))
            provider = _provider_failure(err, out)
            if provider:
                # The endpoint named itself. Live 2026-08-03 02:37 this exact
                # shape was recorded as `stage=harness reason=timeout` with
                # `detail="429: timed out after 3600s"` — the diagnosis was
                # right there in the detail while the STAGE said the seat's
                # own harness had crashed. Naming the stage is what routes it
                # away from the per-wake response and into plain backoff.
                stage, reason = "infrastructure", "provider-failure"
                detail = f"{provider}: {detail}"
            if self._turn_log is not None:
                self._log_lines(out.splitlines())
                if err:
                    self._log_event(event="turn_stderr",
                                    ts=round(time.time(), 3),
                                    agent=self.agent_id, text=err)
                self._log_event(event="turn_end", ts=round(time.time(), 3),
                                agent=self.agent_id, kind=kind, ok=False,
                                harness=self.harness, stage=stage,
                                reason=reason,
                                dur_s=round(time.time() - t0, 1),
                                session=sid)
            self._last_turn_stage = stage
            self._last_turn_detail = detail
            self._record_failure(kind=kind, stage=stage, reason=reason,
                                 detail=detail)
            _emit("AGORA_DRIVE event=turn_end status=error "
                  f"agent={self.agent_id} harness={self.harness} kind={kind} "
                  f"stage={stage} reason={reason} "
                  f"detail={_redact(json.dumps(detail, ensure_ascii=False))}")
            return sid, False
        except FileNotFoundError:
            self._adapter.preflight()
            raise AssertionError("unreachable: preflight should already fail")
        stdout_text = getattr(proc, "stdout", "") or ""
        stderr_text = getattr(proc, "stderr", "") or ""
        if self._turn_log is not None:
            self._log_lines(stdout_text.splitlines())
            if stderr_text:
                self._log_event(event="turn_stderr",
                                ts=round(time.time(), 3),
                                agent=self.agent_id, text=stderr_text)
        # Emitted BEFORE the verdict and on both paths: a harness that
        # refused a tool call must be visible whether or not the turn
        # otherwise passed (a rejected `bash` never fails an opencode turn —
        # only a rejected agora tool does — so this is the ONLY place the
        # refusal surfaces).
        for notice in self._adapter.turn_notices(stdout_text, stderr_text):
            _emit(f"{notice} agent={self.agent_id} kind={kind}")
        evidence = self._adapter.assess_turn(
            stdout_text, stderr_text, proc.returncode, kind
        )
        evidence = self._classify_provider_failure(evidence, stderr_text)
        if evidence.ok:
            evidence = self._verify_reception_debt(evidence, kind)
        new_sid = self._adapter.parse_session_id(stdout_text, session_id)
        if not evidence.ok:
            if self._turn_log is not None:
                self._log_event(event="turn_end", ts=round(time.time(), 3),
                                agent=self.agent_id, kind=kind, ok=False,
                                harness=self.harness, rc=proc.returncode,
                                stage=evidence.stage, reason=evidence.reason,
                                detail=evidence.detail,
                                mcp_tools=list(evidence.tools),
                                dur_s=round(time.time() - t0, 1),
                                session=new_sid)
            self._last_turn_stage = evidence.stage
            self._last_turn_detail = evidence.detail or ""
            self._record_failure(kind=kind, stage=evidence.stage,
                                 reason=evidence.reason,
                                 detail=evidence.detail or "")
            detail = json.dumps(evidence.detail or "no detail", ensure_ascii=False)
            _emit("AGORA_DRIVE event=turn_end status=error "
                  f"agent={self.agent_id} harness={self.harness} kind={kind} "
                  f"stage={evidence.stage or 'unknown'} "
                  f"reason={evidence.reason or 'unknown'} rc={proc.returncode} "
                  f"detail={_redact(detail)}")
            return new_sid, False
        if self._turn_log is not None:
            self._log_event(event="turn_end", ts=round(time.time(), 3),
                            agent=self.agent_id, kind=kind, ok=True, rc=0,
                            harness=self.harness,
                            mcp_tools=list(evidence.tools),
                            dur_s=round(time.time() - t0, 1),
                            session=new_sid)
        # Success is auditable: without this line a healthy driver log shows
        # only arms and wakes, and the operator cannot tell turns from noise.
        tools = ",".join(evidence.tools) or "unobservable"
        _emit("AGORA_DRIVE event=turn_end status=ok "
              f"agent={self.agent_id} harness={self.harness} kind={kind} "
              f"dur={time.time() - t0:.0f}s session={new_sid or '-'} "
              f"mcp_tools={tools}")
        return new_sid, True

    # -- one wake ------------------------------------------------------------

    def _harness_env(self) -> dict[str, str]:
        """Base harness env plus the adapter's per-seat additions.

        The adapter may not smuggle a credential in: an ambient `AGORA_*` key
        in a harness process is how a tool ends up posting under a foreign
        identity (the bearer belongs to the MCP server's 0600 cache).
        """
        env = _harness_environment()
        extra = self._adapter.environment() or {}
        # The boundary is CREDENTIALS, not identity: a seat id or hub URL in
        # env is the same non-secret data other adapters put in argv, and the
        # pi bridge needs it there. A bearer in a harness process is how a
        # tool ends up posting under a foreign identity — still refused, with
        # one deliberate exception: the EMPTY string, which is the documented
        # way to force agora-mcp onto the 0600 key cache.
        bad = sorted(k for k in ("AGORA_API_KEY", "AGORA_ADMIN_KEY")
                     if extra.get(k))
        if bad:
            raise SystemExit(
                f"agora drive: harness adapter '{self.harness}' tried to set "
                f"{', '.join(bad)} in the harness environment. Agora "
                "credentials never travel in a harness process.")
        env.update(extra)
        return env

    def _listen_window(self, snap: tuple[str, str, int] | None) -> float:
        """How long to block in the listener: the chain cadence when a work
        chunk is READY to run, the release instant when it is blocked but
        will free itself, else the full idle ceiling.

        This is the difference between a delegate that finishes a job and one
        that waits for a human. A work chunk only starts at an idle boundary
        (rc=0), so the window IS the initiative clock. Measured on 2026-08-03
        with the old rule (`chain cadence only once a chunk has already run`):
        the editor's single chunk that hour started at 00:50:07, exactly
        1200.0s after its previous turn ended at 00:30:07 — a chunk needs a
        FULL uninterrupted idle ceiling to happen. The delegate never got one
        (its quiet gaps were 908s, 111s, 440s, 771s), so it held an open claim
        for 43 minutes and took no chunk, and the operator had to post twice
        to move it. At the chain cadence the same seat gets a boundary every
        20s and any obligation still preempts at the arm.
        """
        if snap is not None:
            block = self._chain_block(snap)
            if block is None:
                return DRIVE_CHAIN_WAIT          # a chunk is ready to run
            return min(self.max_wait, block[1]) if block[1] > 0 else self.max_wait
        if not self._scan_ok and self._has_work:
            # The scan FAILED (hub blip). Keep the previous answer and retry
            # soon: a transient 500 must not put a working seat to sleep for
            # twenty minutes.
            return DRIVE_CHAIN_WAIT
        return self.max_wait

    def run_turn(self, *, broadcast: bool = False) -> bool:
        """Drive ONE reception turn. Returns True if a turn ran."""
        self._last_turn_ok = None
        self._last_turn_stage = None
        hold = self._hold(has_debt=not broadcast)
        if hold is not None:
            # No sleep here (the old 300s nap was a deaf window): the LOOP
            # holds the wake (_pending_wake) and keeps listening; direct
            # callers just get the False.
            state, reason, retry = hold
            self._state(state, reason=reason, next_s=retry, wake="held")
            return False
        sid = self.reception_session_id
        prompt = WAKE_PROMPT if sid else BOOT_PROMPT
        verify_debt = self.verify_reception_debt
        debt_before = self._reception_debt() if verify_debt else None
        self._reception_debt_verification_required = verify_debt
        self._reception_debt_before = debt_before
        now = time.time()
        self._turn_times.append(now)
        if broadcast:
            self._broadcast_turn_times.append(now)
        try:
            # Announced like a work chunk: a reception turn blocks the same
            # single-threaded loop, and its 600s cap is not always honoured
            # (measured: one ran 3052s on 2026-08-01) — 51 minutes in which
            # nothing said the seat was still in there.
            with self._long_turn_notice(self._prompt_kind(prompt)):
                new_sid, ok = self._spawn(prompt, sid)
        finally:
            self._reception_debt_before = None
            self._reception_debt_verification_required = False
        self._last_turn_ok = ok
        if not ok:
            # A CONFIGURATION error can never succeed, so retrying it is pure
            # waste: the model does not support this reasoning effort, the
            # sandbox contract is impossible, etc. Abort loudly, quoting the
            # harness's own message, which names the fix.
            if self._last_turn_stage == "harness-config":
                raise SystemExit(
                    f"agora drive: {self.harness} refused this seat's "
                    f"configuration, and no retry can fix it:\n  "
                    f"{_one_line(self._last_turn_detail) or 'no detail'}\n"
                    "  Fix the flag (commonly --model / --reasoning-effort) and "
                    "restart the driver.")
            # THE ONE DISTINCTION THAT MATTERS: did the turn reach the hub?
            #
            # `reception` means yes — it ran a pass, looked at the inbox, and
            # left debt behind. That is a DIAGNOSIS, not a failure to retry:
            # the inputs to the next identical turn are identical, so a retry
            # is a second identical answer. Live 2026-08-03 shows exactly that
            # — six `debt-remains` verdicts each respawned a turn within the
            # same second, and three of them were the DELEGATE being failed
            # for `to_consume` rows created by the peers it had dispatched.
            # Worse, the held wake barred the work chunk the debt was asking
            # for, so the seat re-answered instead of working. The debt stays
            # owed, keeps escalating hub-side, and any new message, any
            # escalation band flip, or the next work chunk moves it.
            if self._last_turn_stage in _SEMANTIC_STAGES:
                self._clear_backoff()
                return True
            # `mcp-use` and `mcp-call` say the same thing in the adapters'
            # voice: the turn REACHED the hub and its calls were judged. They
            # are diagnoses too, and the module's own `_TRANSPORT_STAGES`
            # (which the work lane checks) already says so — this lane did
            # not, so giving the claude adapter evidence for the first time
            # would have parked ~11% of its turns in 900s backoff for a
            # verdict about content rather than transport.
            #
            # Anything else never got there (crash, timeout, MCP init, a
            # stream without its terminal event, a 429). One response: back
            # off, KEEP the wake — including broadcasts, since an unowned
            # room-wide ask dropped on its first imperfect turn is how
            # #commons went quiet.
            # Hold the wake FIRST: the backoff line reports whether one is
            # being kept, and on this path one always is.
            self._pending_wake = True
            self._pending_wake_has_debt = not broadcast
            self._note_failure(self._last_turn_stage or "harness",
                               self._last_turn_detail)
            # Only a real resume failure invalidates the session. Dropping it
            # on a semantic verdict threw away the resumable thread and paid a
            # full cold-start BOOT_PROMPT on every subsequent wake.
            if self._last_turn_stage in (None, "harness") and self.reception_session_id:
                self.reception_session_id = None
                self._write_session(self._reception_session_path, None)
                self._reception_turns_on_session = 0
            return True
        self._clear_backoff()
        self.reception_session_id = new_sid
        self._write_session(self._reception_session_path, new_sid)
        self._reception_turns_on_session += 1
        if self._reception_turns_on_session >= self.session_rotate:
            # Fresh session: flush context bloat and injection residue; the
            # hub holds the durable memory, so only scratch is lost. The
            # adapter hook is what makes this real for harnesses whose memory
            # is a state FILE rather than a vendor resume id.
            self._adapter.rotate_session()
            self.reception_session_id = None
            self._write_session(self._reception_session_path, None)
            self._reception_turns_on_session = 0
        return True

    # -- work-gated chunks (work continuation, 2026-07-28; widened to
    #    stewarded phases 2026-08-01) ---------------------------------------

    #: Status words that mean "this row is NOT continuable". `blocked`/`parked`
    #: belong here on purpose: chaining chunks against a declared blocker spins
    #: without progress. What must NOT follow is that the SEAT is finished —
    #: see _continuation_snapshot.
    _TERMINAL_STATUS = frozenset({
        "done", "shipped", "delivered", "complete", "completed",
        "closed", "landed", "merged", "released", "resolved",
        "parked", "paused", "blocked", "on-hold", "onhold",
        "hold", "deferred", "cancelled", "canceled", "abandoned",
    })

    @classmethod
    def _is_terminal(cls, *words: object) -> bool:
        """True when the first word of any given status field is terminal."""
        for word in words:
            text = str(word or "").strip().lower()
            head = text.split()[0].rstrip(".,;:!—-") if text.split() else ""
            if head in cls._TERMINAL_STATUS:
                return True
        return False

    def _work_rows(self) -> list[tuple[float, str, str]]:
        """Every claim:/phase: key in every joined channel, NEWEST FIRST.

        The store listing already carries `updated_at`, so ordering is free —
        and it is the whole fix for the starvation measured on 2026-08-03: the
        old walk returned rows in channel-then-key order and took the FIRST
        one, so the delegate chained on `dm:editor--reader/claim:msg-11` (v1,
        `in_progress`, untouched since 08-01 07:34) while its live work was
        `at-test/claim:msg-445` (v26). Three chunks could not move the stale
        row, the chain parked on it — `AGORA_DRIVE initiative=parked
        key=dm:editor--reader/claim:msg-11@1 reason=no-receipt` — and the seat
        stopped working entirely. The seat owns three more rows in that state
        (`hub-alerts/claim:msg-757|762|764`, v1, `waiting_on_owners`), so the
        trap was permanent, not a coincidence.

        Newest-first means the row you last touched is the work you are on.
        """
        self._scan_ok = False
        api_key = _config.get_cached_key(self.hub, self.agent_id)
        if not api_key:
            return []
        import httpx
        hdrs = {"Authorization": f"Bearer {api_key}"}
        base = self.hub.rstrip("/")
        found: list[tuple[float, str, str]] = []
        try:
            chans = httpx.get(f"{base}/channels", headers=hdrs, timeout=5.0).json()
            for ch in chans if isinstance(chans, list) else []:
                name = ch.get("name") if isinstance(ch, dict) else None
                if not name or not ch.get("member", True):
                    continue
                rows = httpx.get(
                    f"{base}/channels/{name}/store", headers=hdrs, timeout=5.0
                ).json()
                for row in rows if isinstance(rows, list) else []:
                    key = str(row.get("key", ""))
                    if key.startswith("claim:") or key.startswith("phase:"):
                        found.append((float(row.get("updated_at") or 0.0),
                                      str(name), key))
        except Exception:
            return []
        self._scan_ok = True
        # Newest first; a CLAIM outranks a phase row touched in the same
        # instant, because the claim is the finer-grained unit and the only
        # thing that carries a per-slice receipt.
        found.sort(key=lambda r: (r[0], r[2].startswith("claim:"), r[1], r[2]),
                   reverse=True)
        return found

    def _read_work_row(self, channel: str,
                       key: str) -> tuple[int, dict] | None:
        """(version, value) for one store row, or None if it cannot be read."""
        api_key = _config.get_cached_key(self.hub, self.agent_id)
        if not api_key:
            return None
        import urllib.parse
        import httpx
        try:
            entry = httpx.get(
                f"{self.hub.rstrip('/')}/channels/{channel}/store/"
                f"{urllib.parse.quote(key, safe=':')}",
                headers={"Authorization": f"Bearer {api_key}"}, timeout=5.0,
            ).json()
        except Exception:
            return None
        value = entry.get("value")
        if not isinstance(value, dict):
            return None
        return int(entry.get("version", 0)), value

    def _continuable(self, key: str, value: dict) -> bool:
        """Is this row work THIS seat may continue right now?

        TWO kinds of row qualify:

        1. A LIVE CLAIM this seat owns — the finer-grained unit.
        2. An OPEN `phase:` row this seat STEWARDS. Field evidence
           (2026-07-31): a delegate held one claim, `blocked` on an external
           tool fault, plus `phase:manuscript` open with itself as steward and
           `next: writing` declared. Claims-only gating read that seat as
           having nothing to continue, so it took ZERO work turns across a
           24-turn fleet run. A steward with an open phase has real pending
           work by definition: the phase does not close until it acts.

        A row whose status word says done/parked/blocked (or done:true) is not
        continuable — chaining on a declared blocker only spins. That never
        makes the SEAT dead: the next row in the list is considered, and a
        blocked row does not count against opening a new one.
        """
        if key.startswith("claim:"):
            if value.get("owner") != self.agent_id or value.get("done"):
                return False
            if not self._is_terminal(value.get("status"), value.get("state")):
                return True
            # A park with a DECLARED dependency that has since moved is the
            # one terminal row worth reconsidering (2026-08-06): this seat
            # said in structured state "resume when that row changes", and
            # it changed. Without this, `parked` is a one-way door — the
            # incident that cost seven seats a night was a delegate parked
            # on a dependency satisfied 3m43s later.
            return self._waiting_on_satisfied(value.get("waiting_on"))
        return (value.get("steward") == self.agent_id
                and not self._is_terminal(value.get("status"),
                                          value.get("current"))
                and any(str(value.get(field) or "").strip()
                        for field in ("next", "next_step", "current")))

    def _waiting_on_satisfied(self, dep: Any) -> bool:
        """Has the row this claim declared it waits on moved past the
        version stamped when the wait was declared?

        Read straight from the hub, across channels: the dependency that
        caused the incident lived in a different room from the parked row.
        Any failure to read answers False — an unreachable hub must never
        manufacture work."""
        if not isinstance(dep, dict) or not dep.get("key"):
            return False
        api_key = _config.get_cached_key(self.hub, self.agent_id)
        if not api_key:
            return False
        import urllib.parse
        import httpx
        try:
            r = httpx.get(
                f"{self.hub}/channels/"
                f"{urllib.parse.quote(str(dep.get('channel')), safe='')}"
                f"/store/{urllib.parse.quote(str(dep['key']), safe='')}",
                headers={"Authorization": f"Bearer {api_key}"}, timeout=5.0)
            if r.status_code != 200:
                return False
            return int(r.json().get("version") or 0) > int(
                dep.get("at_version") or 0)
        except Exception:
            return False

    def _owned_live_claims(self) -> list[tuple[str, str, int, dict]]:
        """All non-terminal claims owned by this seat, from existing APIs.
        Claims ONLY — reception-debt verification asks "is there a claim row
        linked to this pending ask", and a phase row never answers that."""
        live: list[tuple[str, str, int, dict]] = []
        for _, channel, key in self._work_rows():
            if not key.startswith("claim:"):
                continue
            got = self._read_work_row(channel, key)
            if got and self._continuable(key, got[1]):
                live.append((channel, key, got[0], got[1]))
        return live

    _DONE_STATUS_WORDS = frozenset(
        {"done", "shipped", "complete", "completed", "closed", "cancelled",
         "canceled", "abandoned"})

    def _linked_claim_sources(self) -> set[str]:
        """`source_message_id` of every claim this seat owns that is not
        DONE. Blocked and parked claims count: a claim standing
        `blocked: waiting on the operator` is a materialized plan with a
        recorded reason, not an ignored debt — the reception verdict uses
        this set to excuse standing rows whose work is claimed."""
        out: set[str] = set()
        for _, channel, key in self._work_rows():
            if not key.startswith("claim:"):
                continue
            got = self._read_work_row(channel, key)
            if got is None:
                continue
            value = got[1]
            if value.get("owner") != self.agent_id or value.get("done"):
                continue
            status = str(value.get("status") or value.get("state") or "")
            first = status.strip().lower().split()[0].rstrip(".,;:!—-") \
                if status.strip() else ""
            if first in self._DONE_STATUS_WORDS:
                continue
            src = str(value.get("source_message_id") or "")
            if src:
                out.add(src)
        return out

    def _continuation_snapshot(self) -> tuple[str, str, int] | None:
        """(channel, key, version) of the work this seat should continue NOW,
        or None. Read with the cached key over EXISTING endpoints (precedent:
        listen's /owed poll). Any failure returns None — initiative fails
        toward silence, never toward burn.

        Rows are considered NEWEST FIRST and a row that has already spent its
        strikes is SKIPPED, not returned: one stale row must never be able to
        starve the seat's real work (see _work_rows). Stopping at the first
        answer also keeps the idle-boundary cost at one or two GETs, which
        matters now that the window itself is derived from this scan.

        The phase row is an IGNITION, not a sustainer. Real slice receipts
        land on a CLAIM row, so a stewarded phase collects strikes (it is not
        touched per slice) and is skipped after WORK_STRIKES chunks — exactly
        enough to let the woken steward open a proper claim row for the arc
        and chain on THAT indefinitely.
        """
        for _, channel, key in self._work_rows():
            got = self._read_work_row(channel, key)
            if got is None or not self._continuable(key, got[1]):
                continue
            version = got[0]
            if self._strike_count(f"{channel}/{key}@{version}") >= WORK_STRIKES:
                continue
            return channel, key, version
        return None

    def _strike_count(self, ck: str, now: float | None = None) -> int:
        """Strikes against a row version, with expiry: after WORK_STRIKE_TTL
        without a fresh strike the slate is clean and the row re-enters
        selection. Retirement bounds burn inside the hour; it must never be
        a process-lifetime verdict on work whose only possible reviver is
        the seat the verdict silenced."""
        now = time.time() if now is None else now
        ts = self._work_strike_at.get(ck)
        if ts is not None and now - ts > WORK_STRIKE_TTL:
            self._work_strikes.pop(ck, None)
            self._work_strike_at.pop(ck, None)
            return 0
        return self._work_strikes.get(ck, 0)

    def _receipt_elsewhere(self, since: float, skip_channel: str,
                           skip_key: str) -> bool:
        """Did this seat leave a receipt on any OTHER work row since `since`?

        The strike rule used to read only the SELECTED row, so a chunk that
        did exactly what WORK_PROMPT commands — advance a claim and write
        done/blocked on it — still struck the phase row that selected it
        (the advanced claim is terminal, so the snapshot falls back to the
        unchanged phase). Three by-the-book chunks retired the novel
        steward's only live row while five illustrations landed in the same
        chunks (2026-08-04). A receipt is a row this seat owns or stewards
        whose updated_at moved past the chunk start — including NEW rows and
        rows moved to terminal states; `_work_rows` is newest-first, so the
        scan stops at the first row older than the chunk."""
        checked = 0
        for updated, ch, key in self._work_rows():
            if updated < since - 2.0:
                break
            if (ch, key) == (skip_channel, skip_key):
                continue
            got = self._read_work_row(ch, key)
            if got is None:
                continue
            value = got[1]
            if (value.get("owner") == self.agent_id
                    or value.get("steward") == self.agent_id):
                return True
            checked += 1
            if checked >= 8:
                break
        return False

    def _prune_work_times(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        self._work_times = [t for t in self._work_times
                            if now - t < TURN_BUDGET_WINDOW]
        return now

    def run_work_turn(self) -> bool:
        """Spawn ONE bounded work chunk (WORK_PROMPT, --work-timeout cap).

        Uses a work-only session, and a failing chunk never touches reception:
        its only bound is the per-version strike ledger in _chain_step.
        """
        sid = self.work_session_id
        prompt = WORK_PROMPT if sid else WORK_BOOT_PROMPT
        # A DELEGATE SUPERVISES; IT DOES NOT TAKE THE SEATS' WORK.
        #
        # Operator, 2026-08-07: "the delegate is helping others to do their
        # work but must not do their work on their behalf."
        #
        # The contradiction was in this line, not in the charter. The charter
        # says "a slice another seat owns is DISPATCHED, not done yourself";
        # WORK_PROMPT tells every driven seat to "do ONE bounded slice toward
        # completion". The same seat, in the same hour, was told both by two
        # subsystems. Branching here deletes the contradiction rather than
        # adding a rule about it.
        if self._is_delegate_seat():
            prompt = SUPERVISE_PROMPT + prompt
        self._last_turn_stage = None
        self._work_times.append(time.time())
        # An UNPROVEN harness never gets the full --work-timeout: right after
        # a failed turn the next chunk is capped at the reception bound,
        # because the cost of guessing wrong is this seat being deaf for the
        # whole window (live 2026-07-31: hour-long chunks against a dead
        # endpoint). One healthy turn restores the full budget.
        self._turn_timeout = (min(self.work_timeout, self.reception_timeout)
                              if self._fail_streak else self.work_timeout)
        try:
            with self._long_turn_notice("work"):
                new_sid, ok = self._spawn(prompt, sid)
        finally:
            self._turn_timeout = self.reception_timeout
        if not ok:
            # A chunk that NEVER REACHED THE HUB is the same fact as a
            # reception turn that never reached it — the same binary against
            # the same endpoint — so it feeds the same ONE backoff. Without
            # this the cap above read a streak nothing was feeding: measured
            # on this tree, three consecutive 429-failing chunks each still
            # got the full 3600s, i.e. three hours deaf per row, and neither
            # `_hold` nor `_chain_block` ever saw the outage.
            # SEMANTIC verdicts are excluded on purpose (_TRANSPORT_STAGES):
            # a chunk that did real workspace work without touching an Agora
            # tool is scored `mcp-use`, and holding reception for that would
            # penalise a seat for working. Its only bound stays the strike
            # ledger in _chain_step.
            if self._last_turn_stage in _TRANSPORT_STAGES:
                self._note_failure(self._last_turn_stage or "harness",
                                   self._last_turn_detail)
            # A failed resume: drop the session once and boot fresh next time.
            if self.work_session_id:
                self.work_session_id = None
                self._write_session(self._work_session_path, None)
                self._work_turns_on_session = 0
            return True
        self.work_session_id = new_sid
        self._write_session(self._work_session_path, new_sid)
        self._work_turns_on_session += 1
        if self._work_turns_on_session >= self.session_rotate:
            self._adapter.rotate_session("work")
            self.work_session_id = None
            self._write_session(self._work_session_path, None)
            self._work_turns_on_session = 0
        return True

    def _activate_work_claim(self, channel: str, key: str) -> None:
        """Bind work context to one row (claim or stewarded phase); a
        different row boots a fresh work session."""
        ref = f"{channel}/{key}"
        if ref == self._work_claim_ref:
            return
        self._work_claim_ref = ref
        self._write_session(self._work_claim_path, ref)
        self.work_session_id = None
        self._write_session(self._work_session_path, None)
        self._work_turns_on_session = 0

    def _chain_block(self, snap: tuple[str, str, int] | None
                     ) -> tuple[str, float] | None:
        """Why no work chunk may start now: (reason, seconds until it can).

        ONE predicate, used by both consumers — `_chain_step` (should I spawn?)
        and `_listen_window` (how long may I sleep?). They disagreed before:
        the window said "a chunk is ready, poll every 20s" while the step said
        "parked", so a seat with an exhausted chain re-scanned the whole hub
        three times a minute forever.
        """
        retry = self._backoff_retry_after()
        if retry > 0:
            # Never spend a work chunk on a harness that is currently failing:
            # the chunk blocks reception for its whole timeout and would fail
            # the same way — it is the same binary and the same endpoint.
            return (self._fail_reason or "harness-failing"), retry
        if snap is None:
            return "no-continuable-work", 0.0
        channel, key, version = snap
        if self._strike_count(f"{channel}/{key}@{version}") >= WORK_STRIKES:
            # Selection normally skips a spent row; this is the second lock,
            # so a caller that supplies its own snapshot still cannot spin
            # chunks against a row that has proved it will not move.
            return "no-receipt", 0.0
        now = self._prune_work_times()
        if len(self._work_times) >= self.work_budget:
            return "work-budget", self._retry_after(
                self._work_times, self.work_budget, now)
        return None

    def _chain_step(self, snap: tuple[str, str, int] | None) -> bool:
        """One initiative step at an idle boundary: spawn a work chunk when
        the seat holds continuable work — a live claim, or an open phase row
        it stewards (see _continuation_snapshot). Continuation is a LOOP
        property — chunks chain at DRIVE_CHAIN_WAIT listen windows and any
        obligation preempts at the arm between them — never a model posture.

        Strikes are keyed on the row's CAS VERSION: a chunk that ends with no
        receipt from this seat ANYWHERE — not just on the selected row — is a
        strike, and at WORK_STRIKES the row is retired from selection.
        Recoverable two ways: any row touch mints a fresh version, and
        WORK_STRIKE_TTL expires the strikes themselves (see _strike_count).
        A chunk that advanced a DIFFERENT row this seat owns — including to a
        terminal word like done/blocked, which is the receipt WORK_PROMPT
        commands — is working, not spinning, and takes no strike.
        Retiring a ROW is not parking the SEAT: the next candidate is picked
        on the following pass.
        """
        # Silent when blocked: the loop's own state line already named the
        # reason and the release second on this very pass.
        if self._chain_block(snap) is not None:
            return False
        channel, key, version = snap
        self._activate_work_claim(channel, key)
        ck = f"{channel}/{key}@{version}"
        self._state("chunk", reason="continuable-work", row=ck,
                    strikes=self._strike_count(ck))
        chunk_started = time.time()
        ran = self.run_work_turn()
        after = self._continuation_snapshot()
        if (after is not None and after[0] == channel and after[1] == key
                and after[2] == version):
            if self._receipt_elsewhere(chunk_started, channel, key):
                _emit(f"AGORA_DRIVE initiative=receipt-off-row "
                      f"agent={self.agent_id} key={ck} (the chunk advanced "
                      "another row this seat owns; no strike)")
                return ran
            strikes = self._work_strikes[ck] = self._strike_count(ck) + 1
            self._work_strike_at[ck] = time.time()
            if strikes >= WORK_STRIKES:
                _emit(f"AGORA_DRIVE initiative=retired agent={self.agent_id} "
                      f"key={ck} reason=no-receipt ({WORK_STRIKES} chunks "
                      "left the row unchanged; a version bump OR "
                      f"{int(WORK_STRIKE_TTL / 60)}m of cooldown brings it "
                      "back. The seat keeps working: the next continuable "
                      "row is picked on the next pass)")
        return ran

    def _mirror_mission(self) -> None:
        """Refresh the seat's MISSION into the workspace rule file, and warn
        loudly when there isn't one.

        The mission already rides `whoami`, and the boot prompts order that
        call. But a tool RESULT is something a weak model can skim; the rule
        file is composed into the system prompt and reaches the model before
        its first tool call. Both, then — belt and braces, for the one
        sentence that decides whether a seat knows what it is for.

        Written from the LIVE hub value at every driver start, inside markers,
        so the copy can never drift from the operator's current text (a rule
        file frozen at `agora setup` time would say whatever the seat was for
        last month).

        Measured 2026-08-06: the single seat on this hub with no mission was
        the delegate, and it called a build finished at message 4 of 62."""
        api_key = _config.get_cached_key(self.hub, self.agent_id)
        if not api_key:
            return
        import httpx
        try:
            me = httpx.get(f"{self.hub}/whoami", timeout=5.0,
                           headers={"Authorization": f"Bearer {api_key}"}).json()
        except Exception:
            return                              # never block a run on this
        mission = str(me.get("mission") or "").strip()
        if not mission:
            _emit(f"AGORA_DRIVE warn=no-mission agent={self.agent_id} — this "
                  "seat has no standing mission, so every session invents its "
                  "own role from the room. Set one: "
                  f"`agora mission set {self.agent_id} '<what this seat is "
                  "FOR>'`.")
        try:
            from .setup_harness import write_mission_block
            path = write_mission_block(self.cwd, self.harness, mission)
        except Exception:
            return
        if path is not None:
            _emit(f"AGORA_DRIVE event=mission status=ok agent={self.agent_id} "
                  f"file={path.name} chars={len(mission)}")

    def run(self, *, once: bool = False, max_turns: int | None = None) -> int:
        """The loop: wait for an obligation, drive a turn, repeat; at idle
        boundaries additionally chain claim-gated work chunks (a seat holding a
        live claim keeps working — that is not an opt-in, it is the job).
        `once` drives a single turn immediately (boot); `max_turns`
        bounds the run (harness/testing). Idle waits cost ~0 tokens
        (blocked in listen)."""
        self._preflight_spawner()
        # Order matters (review F1): claim the driver seat FIRST — its
        # refusal names the real conflict (a live driver) — then check for
        # an interactive listener, ignoring the previous driver's own one.
        prev_pid = self._acquire_drive_pid()
        self._check_foreign_listener(prev_driver_pid=prev_pid)
        self._clear_stale_owed_signature()
        sdk = (self._adapter.mcp_probe.sdk_version
               if self._adapter.mcp_probe else "unknown")
        details = ["AGORA_DRIVE event=ready status=ok",
                   f"agent={self.agent_id}", f"harness={self.harness}",
                   f"hub={self.hub}", "mcp=required",
                   f"mcp_sdk={sdk or 'unknown'}",
                   f"mcp_command={self._mcp_binding.command}"]
        if "permissions" in self._adapter.SUPPORTS:
            details.append(f"permissions={self.permissions}")
        # Always state the model, even when it is the harness's own default:
        # "which brain is answering my hub" is the first thing an operator
        # debugging a quiet seat needs, and omitting the line hid exactly the
        # case where the default was too small to work at all.
        details.append(f"model={self._adapter.effective_model()}")
        if self.provider:
            details.append(f"provider={self.provider}")
        # Read the ADAPTER's value, not the Driver's flag: a per-harness default
        # (e.g. codex -> medium) is resolved in _make_adapter, and a ready line
        # that omitted it understated what the seat is actually running.
        if self._adapter.reasoning_effort:
            details.append(f"reasoning_effort={self._adapter.reasoning_effort}")
        details.append(f"work_budget={self.work_budget}/h")
        _emit(" ".join(details))
        self._adapter.warn_effective_model()
        # Advisory knobs: forwarded, never guaranteed. Said once here so the
        # ready line cannot silently over-claim a setting the provider drops.
        if self._adapter.IDENTITY_SCOPE == "process":
            _emit(f"AGORA_DRIVE warn=identity-scope agent={self.agent_id} "
                  f"harness={self.harness} scope=process — this harness cannot "
                  "tell a turn which seat it is, so whatever identity its "
                  "server is configured with is the one that posts. Safe only "
                  "while that server serves THIS seat alone; a second seat "
                  "would post under this one's name. Verify with "
                  f"`agora harness-check {self.harness} --live`.")
        if "permissions" in self._adapter.ADVISORY:
            _emit(f"AGORA_DRIVE warn=advisory-knob agent={self.agent_id} "
                  f"harness={self.harness} knob=permissions "
                  f"value={self.permissions} — a DECLARATION, not containment: "
                  "this harness enforces nothing at tool time. Contain the "
                  "seat externally (container/VM) if its workspace matters.")
        if self._adapter.reasoning_effort and "reasoning" in self._adapter.ADVISORY:
            _emit(f"AGORA_DRIVE warn=advisory-knob agent={self.agent_id} "
                  f"harness={self.harness} knob=reasoning "
                  f"value={self.reasoning_effort} — forwarded to the harness, "
                  "but an OpenAI-compatible provider may silently ignore "
                  "effort scaling. Check the harness's own stderr (or run "
                  "with --turn-log) before trusting it.")
        self._mirror_mission()
        driven = 0
        try:
            if once:
                ran = self.run_turn()
                return 0 if ran and self._last_turn_ok else 1
            backoff = 1.0
            while max_turns is None or driven < max_turns:
                self._touch_drive_pid()
                hold = (self._hold(has_debt=self._pending_wake_has_debt)
                        if self._pending_wake else None)
                # A held human/peer debt outranks idle listening. Run it as
                # soon as capacity returns; unowned broadcasts additionally
                # pass the small anti-storm fuse.
                if self._pending_wake and hold is None:
                    has_debt = self._pending_wake_has_debt
                    self._pending_wake = False
                    self._pending_wake_has_debt = False
                    self._state("turn", reason="held-wake")
                    if self.run_turn(broadcast=not has_debt):
                        driven += 1
                    continue
                # ONE scan per pass, shared by the window and the chunk gate:
                # they used to disagree, and each was paying its own hub walk.
                snap = self._continuation_snapshot()
                if self._scan_ok:
                    self._has_work = snap is not None
                # source=auto: notify-file tail when the hub is local (0
                # sockets), websocket otherwise — hard-coding "file" made
                # remote seats deaf. signal_passthrough: SIGTERM/SIGINT must
                # kill THIS loop, not be swallowed by the listener's own
                # handlers. Missed-wake recovery is INSIDE run_listen:
                # arming starts with a debt poll (signature-gated), so an
                # obligation that landed mid-turn wakes at the next arm —
                # which, while a chain is live, is at most DRIVE_CHAIN_WAIT
                # away: obligations always preempt the next chunk.
                window = self._listen_window(snap)
                if hold is not None:
                    window = min(window, max(hold[2], 0.01))
                    self._state(hold[0], reason=hold[1], next_s=hold[2],
                                wake="held")
                else:
                    block = self._chain_block(snap)
                    reason = block[0] if block else "chunk-ready"
                    if snap is None and not self._scan_ok:
                        # DO NOT claim `no-continuable-work` off a walk that
                        # never completed. A seat mid hub-blip holding a live
                        # claim looks byte-identical to a genuinely idle one,
                        # and that is the difference between "your delegate is
                        # done" and "your delegate cannot see its own work".
                        # The window already retries soon (_listen_window);
                        # this is the log telling the same truth.
                        reason = "work-scan-unreadable"
                    self._state("armed", reason=reason, next_s=window,
                                row=(f"{snap[0]}/{snap[1]}@{snap[2]}"
                                     if snap else ""))
                rc = run_listen(agent_id=self.agent_id, url=self.hub,
                                once=True, important_only=True,
                                max_wait=window, source="auto",
                                signal_passthrough=True, driver_call=True)
                if rc == _DRIVER_UNOWNED_WAKE:
                    # A WAKE MUST CARRY WORK. This batch named nobody and the
                    # hub says the seat owes nothing, so a turn here has
                    # nothing to settle — and a seat with nothing to settle
                    # posts ceremony to justify having woken (measured: 50%
                    # of such turns vs 8.3% when addressed asks were live).
                    # The mail is already delivered and waits in the inbox;
                    # the next ask, escalation or work chunk reads it. Never
                    # silent: the decision is on stdout with its reason. It
                    # then falls through as an idle window, so real claim
                    # work is not delayed by mail that obliges nothing.
                    _emit(f"AGORA_DRIVE wake-noop agent={self.agent_id} "
                          "reason=unowned-broadcast owed=0 — mail delivered "
                          "and waiting; no turn spawned")
                    rc = 0
                if rc in (2, _DRIVER_BROADCAST_WAKE):
                    # The listener classifies the triggering batch itself.
                    # This cannot be confused by an old unchanged owed row:
                    # addressed/forced events + backlog debt return 2; a pure
                    # room-wide open/blocked batch returns the internal code 4.
                    has_debt = rc == 2
                    wake_hold = self._hold(has_debt=has_debt)
                    if wake_hold is None:
                        # This turn drains the WHOLE inbox, held debt
                        # included — a still-set flag would spawn a
                        # spurious turn at the next idle (review F2).
                        self._pending_wake = False
                        self._pending_wake_has_debt = False
                        self._state("turn",
                                    reason="obligation" if has_debt
                                    else "broadcast")
                        if self.run_turn(broadcast=not has_debt):
                            driven += 1
                        backoff = 1.0
                    else:
                        # Held, never dropped: the listener already recorded
                        # the owed signature, so without this flag the debt
                        # would wait for hub escalation (consumed-wake
                        # stall, review 2026-07-28); the flag converts it
                        # into a turn the moment the window releases.
                        self._pending_wake = True
                        self._pending_wake_has_debt |= has_debt
                        # ONE honest line per pass: when this pass already
                        # armed with the very same hold, repeating it byte for
                        # byte doubles a parked seat's log without adding a
                        # fact. A hold that CHANGED (a new addressed wake is
                        # judged against the hard ceiling, not the broadcast
                        # fuse) is a different fact and is always said.
                        if hold is None or hold[:2] != wake_hold[:2]:
                            self._state(wake_hold[0], reason=wake_hold[1],
                                        next_s=wake_hold[2], wake="held")
                elif rc == 0:                 # idle timeout OR hub-unreachable
                    if (self._pending_wake
                            and self._hold(
                                has_debt=self._pending_wake_has_debt) is None):
                        has_debt = self._pending_wake_has_debt
                        self._pending_wake = False
                        self._pending_wake_has_debt = False
                        self._state("turn", reason="held-wake")
                        if self.run_turn(broadcast=not has_debt):
                            driven += 1
                        continue
                    # A HELD wake blocks new work chunks (storm review,
                    # 2026-07-28): starting a chunk here could pin the seat
                    # for up to --work-timeout while a human's debt sits at
                    # its exact release point — reception outranks work. This
                    # is only ever a WAIT now, never a stall: the only things
                    # that hold a wake are backoff and a spent budget, both
                    # of which name their release second on the state line.
                    if not self._pending_wake:
                        if self._chain_step(snap):
                            driven += 1
                else:                         # unexpected: bounded backoff
                    self._state("backoff", reason=f"listener-rc={rc}",
                                next_s=backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
            return 0
        finally:
            self._clear_drive_pid()


def run_drive(*, agent_id: str | None = None, url: str | None = None,
              harness: str | None = None,
              model: str | None = DEFAULT_MODEL,
              provider: str | None = None,
              reasoning_effort: str | None = None,
              harness_args: dict[str, str] | None = None,
              max_wait: float = DEFAULT_MAX_WAIT,
              permissions: str | None = None, sandbox: str | None = None,
              turn_budget: int = DEFAULT_TURN_BUDGET,
              broadcast_turn_budget: int = DEFAULT_BROADCAST_TURN_BUDGET,
              session_rotate: int = DEFAULT_SESSION_ROTATE,
              work_timeout: float = TURN_TIMEOUT,
              reception_timeout: float = RECEPTION_TURN_TIMEOUT,
              work_budget: int = DEFAULT_WORK_BUDGET, force: bool = False,
              turn_log: str | None = None,
              once: bool = False, max_turns: int | None = None,
              cwd: Path | None = None) -> int:
    from .setup_harness import resolve_drive_harness

    workspace = Path(cwd) if cwd else Path.cwd()
    # Knob and drivability checks come FIRST, before any workspace or identity
    # resolution: "this harness cannot be driven" and "this harness does not
    # have that knob" are facts about the request itself, and reporting a
    # missing-wiring error instead would send the operator to fix the wrong
    # thing (and, for a knob, let them arm a seat that fails every wake).
    _validate_drive_request(harness, model=model, provider=provider,
                            reasoning_effort=reasoning_effort,
                            permissions=_effective_permissions(
                                harness, permissions, sandbox))
    try:
        resolved_harness = resolve_drive_harness(workspace, harness)
    except ValueError as exc:
        raise SystemExit(f"agora drive: {exc}") from exc
    identity = resolve_workspace_identity(workspace, harness=resolved_harness) or {}
    # A workspace seat is canonical for a driver. Ambient variables are a
    # fallback only outside a wired workspace; stale shell state must never
    # silently redirect a configured seat to another identity or hub.
    for variable, explicit, configured in (
        ("AGORA_AGENT_ID", agent_id, identity.get("AGORA_AGENT_ID")),
        ("AGORA_URL", url, identity.get("AGORA_URL")),
    ):
        ambient = os.environ.get(variable)
        if (explicit is None and configured and ambient
                and str(ambient).rstrip("/") != str(configured).rstrip("/")):
            _emit("AGORA_DRIVE event=config status=ignored "
                  "reason=workspace-seat-wins "
                  f"variable={variable} ambient={json.dumps(ambient)} "
                  f"configured={json.dumps(configured)}")
    aid = (agent_id or identity.get("AGORA_AGENT_ID")
           or os.environ.get("AGORA_AGENT_ID"))
    if not aid:
        raise SystemExit(
            "agora drive: cannot determine the agent id. Pass --as <id>, or "
            "cd to the folder you wired with `agora setup` (agora does not "
            "search parent folders)."
        )
    hub = (url or identity.get("AGORA_URL") or os.environ.get("AGORA_URL")
           or _config.load_config().get("url") or "http://127.0.0.1:8765")
    hub = str(hub).rstrip("/")
    driver = Driver(aid, hub, harness=resolved_harness, model=model,
                    provider=provider,
                    reasoning_effort=reasoning_effort,
                    harness_args=harness_args, max_wait=max_wait,
                    permissions=permissions, sandbox=sandbox,
                    turn_budget=turn_budget,
                    broadcast_turn_budget=broadcast_turn_budget,
                    session_rotate=session_rotate,
                    work_timeout=work_timeout, work_budget=work_budget,
                    reception_timeout=reception_timeout,
                    force=force, turn_log=turn_log, cwd=workspace)
    try:
        return driver.run(once=once, max_turns=max_turns)
    except KeyboardInterrupt:
        _emit(f"AGORA_DRIVE event=stopped status=ok agent={aid} "
              "reason=operator-interrupt")
        return 130
