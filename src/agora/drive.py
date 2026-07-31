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

import contextlib
import hashlib
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
    "assigned work, START DOING THE WORK NOW in this workspace; do not send "
    "an acknowledgement, intention, or promise. Finish and answer with "
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
    "rules; skim your channels. Then run one reception pass (check_inbox, "
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
    "obligation is waiting; you hold the live "
    "claim in your home channel — continue THAT work. FIRST re-read the "
    "claim row and any newer messages touching the task: a newer message "
    "may have canceled, refined, or superseded it (the record outranks "
    "your memory) — if so, adjust or park on the record instead of "
    "continuing blind. Otherwise do ONE bounded slice toward completion, "
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
    "whoami and heed the hub rules; skim your channels. Then follow the work "
    "contract: re-read your one live claim and newer messages that may "
    "supersede it, do one bounded slice, and update the claim row. The row is "
    "the only per-slice receipt; never post reception-pass, no-delta, guard-"
    "rerun, parked, or routine progress messages. If blocked, mark the row "
    "and send one addressed structured ask in a DM or focused group only "
    "when another seat can act; never broadcast or repeat an unchanged blocker. "
    "Then END. Do not wait, listen, or start watchers."
)

DEFAULT_MODEL: str | None = None
DEFAULT_MAX_WAIT = 1200.0           # idle ceiling; a wake returns instantly
DEFAULT_TURN_BUDGET = 250           # light abuse ceiling; ordinary debt rarely parks
DEFAULT_BROADCAST_TURN_BUDGET = 100 # roomy fuse for noisy unowned wakes
TURN_BUDGET_WINDOW = 3600.0
DEFAULT_SESSION_ROTATE = 25         # turns on one session before a fresh one
POISON_STRIKES = 3                  # a wake that crashes N turns is quarantined
QUARANTINE_TTL = 3600.0             # and stays quarantined only THIS long. The
#                                     wake key is the owed SIGNATURE, and an
#                                     obligation a seat cannot yet settle has a
#                                     signature that BY CONSTRUCTION never
#                                     changes — so a permanent quarantine made
#                                     three transient harness failures deafen
#                                     the seat forever to the exact debt it most
#                                     needed to answer. Expiry bounds the damage
#                                     to one window; a genuinely poisonous wake
#                                     simply re-earns its strikes.
INFRA_BACKOFF_BASE = 60.0           # first wait after a PROVIDER-level failure
INFRA_BACKOFF_MAX = 900.0           # ceiling for the exponential backoff: a
#                                     rate-limited fleet must stop hammering,
#                                     but must still recover on its own
MUTE_NOTICE_INTERVAL = 300.0        # how often a seat that is NOT processing
#                                     obligations must say so on stdout: a
#                                     driver may be quiet, never silently mute
LONG_TURN_NOTICE = 600.0            # cadence of the "still running" line for a
#                                     turn that blocks the single-threaded loop
FAILURE_LEDGER_MAX_BYTES = 1_000_000
TURN_TIMEOUT = 3600.0               # one WORK chunk; full jobs span many chunks
RECEPTION_TURN_TIMEOUT = 600.0      # one RECEPTION turn. Deliberately much
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
_LISTENER_FRESH_S = 600.0           # a listen pidfile younger than this marks
#                                     a live interactive surface (tab loops
#                                     rewrite it every <=245s)
_DRIVER_STALE_S = 7200.0            # a drive pidfile older than this never
#                                     blocks anyone (reboot pid-reuse guard)


@dataclass(frozen=True)
class TurnEvidence:
    ok: bool
    stage: str | None = None
    reason: str | None = None
    detail: str | None = None
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReceptionDebt:
    """Debt identities that a reception turn must engage or consume."""

    to_answer: frozenset[str]
    to_consume: frozenset[str]
    structured: tuple[tuple[str, int, str, frozenset[str]], ...] = ()

    @property
    def empty(self) -> bool:
        return not self.to_answer and not self.to_consume


# Issued bearer values have a long random suffix. Requiring 32+ characters
# avoids corrupting ordinary identifiers such as ``agora_protocol.py`` in a
# transcript while still covering every generated key format.
_AGORA_KEY_RE = re.compile(r"\bagora_[A-Za-z0-9_-]{32,}\b")


def _redact(text: str) -> str:
    """Keep diagnostics actionable without ever recording bearer values."""
    return _AGORA_KEY_RE.sub("agora_[REDACTED]", text)


def _one_line(text: str, *, limit: int = 500) -> str:
    return _redact(" ".join(text.split()))[:limit]


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

    def build_command(self, prompt: str, session_id: str | None) -> list[str]:
        cmd = ["claude", "-p", "--output-format", "json"]
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
    _PERMISSION = {
        "read": {"bash": "deny", "edit": "deny", "write": "deny",
                 "webfetch": "deny", "websearch": "deny", "agora*": "allow"},
        "write": {"bash": "allow", "edit": "allow", "write": "allow",
                  "webfetch": "deny", "websearch": "deny", "agora*": "allow"},
        "all": {"*": "allow", "agora*": "allow"},
    }

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

    def environment(self) -> dict[str, str]:
        # opencode has no argv equivalent of codex's `-c`; its per-run layer
        # is this env var. Riding it here also puts the binding in the surface
        # `agora harness-check` inspects (C4/C5/C8).
        return {"OPENCODE_CONFIG_CONTENT": json.dumps(self._run_config())}

    def build_command(self, prompt: str, session_id: str | None) -> list[str]:
        cmd = ["opencode", "run",
               # NOT optional: without it the turn runs in whatever $PWD the
               # parent shell had, silently losing the workspace, the project
               # config and AGENTS.md.
               "--dir", str(Path(self.cwd).resolve()),
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
            provider = _provider_failure(harness_error, stderr)
            if provider:
                # The provider refused or fell over. Nothing about this seat's
                # wake caused it and no retry count can fix it — it heals on
                # its own clock, so it is backed off, never struck.
                return TurnEvidence(
                    ok=False, stage="infrastructure", reason="provider-failure",
                    detail=_one_line(harness_error or stderr or provider),
                    tools=tuple(successful))
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
               "--timeout", str(int(RECEPTION_TURN_TIMEOUT))]
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
    id (for resume/state), the per-hour turn budget, the poison ledger keyed
    by the wake's channel head, and the session-rotation counter."""

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
        # `spawn` is injectable so the loop is unit-testable without a real
        # harness process: spawn(prompt, session_id) -> (new_session_id|None, ok).
        self._spawn = spawn or self._spawn_turn
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
            self._attempts_path = home / f"drive-{agent_id}.attempts"
        else:
            self._reception_session_path = (
                home / f"drive-{agent_id}.{harness}.reception-v2.session")
            self._work_session_path = (
                home / f"drive-{agent_id}.{harness}.work-v2.session")
            self._work_claim_path = home / f"drive-{agent_id}.{harness}.work-v2.claim"
            self._attempts_path = home / f"drive-{agent_id}.{harness}.attempts"
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
        # wake key -> when its quarantine LAPSES. Never permanent: see
        # QUARANTINE_TTL.
        self._quarantine_until: dict[str, float] = {}
        self._infra_failures = 0                  # consecutive provider failures
        self._infra_retry_at = 0.0                # ...and when to try again
        self._last_mute_notice = 0.0              # cadence of the "still mute" line
        self._turn_timeout = RECEPTION_TURN_TIMEOUT  # work turns raise it
        self._work_times: list[float] = []        # work-chunk spawns, rolling hour
        self._work_strikes: dict[str, int] = {}   # claim-version -> receipt-less chunks
        self._chain_live = False                  # a work chain is running
        self._pending_wake = False                # a budget-parked wake is HELD
        self._pending_wake_has_debt = False       # addressed/owed beats broadcast fuse
        self._reception_debt_before: ReceptionDebt | None = None
        self._reception_debt_verification_required = False
        self._last_turn_ok: bool | None = None
        # Stage of the most recent failed turn ("harness" = the process itself
        # failed; "mcp-use"/"reception" = the turn ran but its outcome was
        # judged incomplete). Only "harness" may cost a poison strike.
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

    def _attempts(self) -> dict[str, int]:
        try:
            return json.loads(self._attempts_path.read_text())
        except (OSError, ValueError):
            return {}

    def _bump_attempt(self, key: str) -> int:
        data = self._attempts()
        data[key] = data.get(key, 0) + 1
        try:
            self._attempts_path.write_text(json.dumps(data))
        except OSError:
            pass
        return data[key]

    def _clear_attempt(self, key: str) -> None:
        data = self._attempts()
        if data.pop(key, None) is not None:
            try:
                self._attempts_path.write_text(json.dumps(data))
            except OSError:
                pass

    # -- quarantine (bounded) ------------------------------------------------

    @property
    def _quarantined(self) -> set[str]:
        """Wake keys quarantined RIGHT NOW (expired ones are not)."""
        now = time.time()
        return {k for k, until in self._quarantine_until.items() if now < until}

    def _quarantine_expired(self, key: str) -> bool:
        """True once `key`'s quarantine has lapsed; clears it and its strikes.

        The strikes go with it: a wake that returns after the window deserves
        a full fresh try, not instant re-quarantine off the on-disk ledger.
        """
        until = self._quarantine_until.get(key)
        if until is None or time.time() < until:
            return False
        del self._quarantine_until[key]
        self._clear_attempt(key)
        _emit(f"AGORA_DRIVE quarantine-lapsed agent={self.agent_id} key={key} "
              f"after={QUARANTINE_TTL:.0f}s — retrying this obligation")
        return True

    # -- provider failures (retry, never strike) -----------------------------

    def _note_infra_failure(self, detail: str) -> float:
        """Record a provider-level failure and return the backoff in seconds."""
        self._infra_failures += 1
        wait = min(INFRA_BACKOFF_BASE * (2 ** (self._infra_failures - 1)),
                   INFRA_BACKOFF_MAX)
        self._infra_retry_at = time.time() + wait
        _emit(f"AGORA_DRIVE parked agent={self.agent_id} "
              f"reason=provider-failing consecutive={self._infra_failures} "
              f"retry_in={wait:.0f}s wake=held — the harness's PROVIDER is "
              f"failing, not this seat; obligations wait and still escalate "
              f"hub-side. detail={_one_line(detail, limit=200) or 'none'}")
        return wait

    def _clear_infra_failure(self) -> None:
        if self._infra_failures:
            _emit(f"AGORA_DRIVE recovered agent={self.agent_id} "
                  f"after={self._infra_failures} provider-failure(s)")
        self._infra_failures = 0
        self._infra_retry_at = 0.0

    def _infra_retry_after(self) -> float:
        return max(0.0, self._infra_retry_at - time.time())

    # -- never silently mute -------------------------------------------------

    def _mute_notice(self) -> None:
        """Say, at intervals, that this seat is NOT processing obligations.

        A driver whose wakes are all held, parked or quarantined still
        heartbeats — presence advances, the listener re-arms — and so reads as
        an idle HEALTHY seat. That is precisely how eight seats stayed mute for
        four hours on 2026-07-31. Quiet is fine; mute must announce itself.
        """
        held = self._pending_wake
        infra = self._infra_retry_after()
        quarantined = self._quarantined
        if not (held or infra > 0 or quarantined):
            self._last_mute_notice = 0.0
            return
        now = time.time()
        if now - self._last_mute_notice < MUTE_NOTICE_INTERVAL:
            return
        self._last_mute_notice = now
        parts = [f"AGORA_DRIVE mute agent={self.agent_id}"]
        if held:
            parts.append("wake=held")
        if infra > 0:
            parts.append(f"reason=provider-failing retry_in={infra:.0f}s "
                         f"consecutive={self._infra_failures}")
        if quarantined:
            parts.append(f"quarantined_keys={len(quarantined)}")
        parts.append("— this seat is NOT processing obligations; they still "
                     "escalate hub-side")
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

    def _budget_ok(self) -> bool:
        self._prune_turn_times()
        return len(self._turn_times) < self.turn_budget

    def _broadcast_budget_ok(self) -> bool:
        self._prune_turn_times()
        return len(self._broadcast_turn_times) < self.broadcast_turn_budget

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

    def _budget_retry_after(self, *, has_debt: bool) -> float:
        """Seconds until a held wake is admissible (zero if it is now).

        Addressed/owed work uses only the high hard ceiling. Unowned
        room-wide wakes also pass the smaller broadcast fuse, so a noisy
        channel cannot consume the addressed-turn allowance.
        """
        now = self._prune_turn_times()
        hard = self._retry_after(self._turn_times, self.turn_budget, now)
        # A failing provider is a wait like any other: fold it in so the loop's
        # listen window shrinks to the retry instant instead of the idle
        # ceiling, and the seat resumes the moment the provider heals.
        hard = max(hard, self._infra_retry_after())
        if has_debt:
            return hard
        broadcast = self._retry_after(
            self._broadcast_turn_times, self.broadcast_turn_budget, now
        )
        return max(hard, broadcast)

    def _wake_admissible(self, *, has_debt: bool) -> bool:
        return (self._infra_retry_after() <= 0.0 and self._budget_ok()
                and (has_debt or self._broadcast_budget_ok()))

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
            to_consume=frozenset(
                str(row.get("answer_id"))
                for row in raw.get("to_consume", [])
                if isinstance(row, dict) and row.get("answer_id")
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
        """Read the hub-decorated pending ask ids for one source message."""
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
            pending = row.get("pending_asks")
            return frozenset(str(v) for v in (pending or []))
        except Exception:
            return None

    def _verify_reception_debt(self, evidence: TurnEvidence,
                               kind: str) -> TurnEvidence:
        """Require every debt present before this turn to be settled.

        Hub semantics already encode the right abstraction: an original
        ``to_answer`` row disappears after any valid reply/claim/refusal, and
        ``to_consume`` disappears after the answer is read or used.  Comparing
        IDs avoids parsing model prose and ignores new debt arriving mid-turn.
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
        unconsumed = sorted(before.to_consume & after.to_consume)
        unresolved_structured: list[str] = []
        linked_sources: set[str] | None = None
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
                    linked_sources = {
                        str(value.get("source_message_id") or "")
                        for _, _, _, value in self._owned_live_claims()
                    }
                if message_id not in linked_sources:
                    unresolved_structured.append(message_id)
        if not unanswered and not unconsumed and not unresolved_structured:
            return evidence
        parts = []
        if unanswered:
            parts.append("to_answer=" + ",".join(unanswered[:10]))
        if unconsumed:
            parts.append("to_consume=" + ",".join(unconsumed[:10]))
        if unresolved_structured:
            parts.append(
                "pending_without_linked_claim=" + ",".join(unresolved_structured[:10])
            )
        return TurnEvidence(
            ok=False, stage="reception", reason="debt-remains",
            detail="original debt remains after turn: " + " ".join(parts),
            tools=evidence.tools,
        )

    def _spawn_cursor_agent(self, prompt: str, session_id: str | None):
        """Compatibility shim for the legacy Cursor-only tests/callers."""
        return self._spawn_turn(prompt, session_id)

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
        evidence = self._adapter.assess_turn(
            stdout_text, stderr_text, proc.returncode, kind
        )
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

    def _wake_key(self) -> str:
        """Identify the current wake for the poison ledger: a wake that keeps
        crashing the same turn is a poison message; after POISON_STRIKES we
        quarantine it (the unacked obligation still escalates hub-side, so
        it cannot rot invisibly).

        Primary key: the owed SIGNATURE the listener just recorded
        (listen-<id>.owedsig) — the debt's identity. The old size-only key
        collided after notify-file rotation and degraded to a constant '0'
        for ws-mode seats, quarantining every future wake after three bad
        turns (review 2026-07-28). Fallbacks keep the old behavior."""
        sig = _config.home() / f"listen-{self.agent_id}.owedsig"
        try:
            content = sig.read_text().strip()
            if content:
                return hashlib.sha256(content.encode()).hexdigest()[:12]
        except OSError:
            pass
        nf = _config.home() / f"{self.agent_id}-inbox.log"
        try:
            return f"{nf.stat().st_size}"
        except OSError:
            return "0"

    def run_turn(self, *, broadcast: bool = False) -> bool:
        """Drive ONE reception turn. Returns True if a turn ran."""
        self._last_turn_ok = None
        self._last_turn_stage = None
        if not self._wake_admissible(has_debt=not broadcast):
            # No sleep here (the old 300s nap was a deaf window): the LOOP
            # holds the wake (_pending_wake) and keeps listening; direct
            # callers just get the False.
            infra = self._infra_retry_after()
            if infra > 0:
                _emit(f"AGORA_DRIVE parked agent={self.agent_id} "
                      f"reason=provider-failing retry_in={infra:.0f}s")
                return False
            reason = "broadcast-budget" if self._budget_ok() else "turn-budget"
            limit = (self.turn_budget if reason == "turn-budget"
                     else self.broadcast_turn_budget)
            _emit(f"AGORA_DRIVE parked agent={self.agent_id} "
                  f"reason={reason} ({limit}/h)")
            return False
        key = self._wake_key()
        self._quarantine_expired(key)
        if key in self._quarantine_until:
            # NEVER SILENT: a dropped wake used to return False with no line
            # at all, so a deafened seat looked identical to an idle one.
            left = max(0.0, self._quarantine_until[key] - time.time())
            _emit(f"AGORA_DRIVE wake-dropped agent={self.agent_id} key={key} "
                  f"reason=quarantined retry_in={left:.0f}s — this seat is "
                  "NOT processing this obligation; it still escalates hub-side")
            return False
        sid = self.reception_session_id
        prompt = WAKE_PROMPT if sid else BOOT_PROMPT
        verify_debt = self._spawn == self._spawn_turn
        debt_before = self._reception_debt() if verify_debt else None
        self._reception_debt_verification_required = verify_debt
        self._reception_debt_before = debt_before
        now = time.time()
        self._turn_times.append(now)
        if broadcast:
            self._broadcast_turn_times.append(now)
        try:
            new_sid, ok = self._spawn(prompt, sid)
        finally:
            self._reception_debt_before = None
            self._reception_debt_verification_required = False
        self._last_turn_ok = ok
        if not ok:
            # ONLY a HARNESS-level failure is a strike. The poison ledger
            # exists for "this wake crashes the harness" — a real, repeatable
            # defect. A SEMANTIC verdict ("debt remains") is a normal outcome:
            # debt an agent cannot settle alone (waiting on a peer, needing a
            # human ruling, an ask it declined) produces a STABLE owedsig, so
            # the same key returned every wake and hit 3 strikes with
            # certainty — the seat then went permanently deaf to exactly the
            # obligation it most needed help with. The ledger is on disk, so
            # a restart re-quarantined on the first failure.
            # A CONFIGURATION error can never succeed, so retrying it is pure
            # waste: the model does not support this reasoning effort, the
            # sandbox contract is impossible, etc. Semantic failures are retried
            # (correctly — debt often needs another pass), so without this branch
            # a bad `--reasoning-effort` would respawn a turn forever. Abort
            # loudly, quoting the harness's own message, which names the fix.
            if self._last_turn_stage == "harness-config":
                raise SystemExit(
                    f"agora drive: {self.harness} refused this seat's "
                    f"configuration, and no retry can fix it:\n  "
                    f"{_one_line(self._last_turn_detail) or 'no detail'}\n"
                    "  Fix the flag (commonly --model / --reasoning-effort) and "
                    "restart the driver.")
            # A PROVIDER failure (429, 5xx, a turn that timed out having called
            # nothing) is about the endpoint, never about this wake: striking it
            # is how a rate limit at 04:59 deafened eight seats for hours. Back
            # off instead — loudly — and keep the wake.
            if self._last_turn_stage == "infrastructure":
                self._note_infra_failure(self._last_turn_detail)
                self._pending_wake = True
                self._pending_wake_has_debt = not broadcast
                return True
            self._clear_infra_failure()
            # No recorded stage = a raw spawn failure (the process died before
            # any evidence could be assessed), which IS a harness failure.
            harness_failure = self._last_turn_stage in (None, "harness")
            if harness_failure:
                n = self._bump_attempt(key)
                if n >= POISON_STRIKES:
                    self._quarantine_until[key] = time.time() + QUARANTINE_TTL
                    _emit(f"AGORA_DRIVE quarantine agent={self.agent_id} "
                          f"key={key} strikes={n} ttl={QUARANTINE_TTL:.0f}s — a "
                          f"wake crashed {n} turns; the obligation still "
                          f"escalates hub-side and this key retries after the "
                          f"ttl")
            if not harness_failure or key not in self._quarantine_until:
                # Hold the wake — including for broadcasts (an unowned
                # room-wide ask used to be dropped outright on its first
                # imperfect turn, which is how #commons went quiet).
                self._pending_wake = True
                self._pending_wake_has_debt = not broadcast
            # Only a real resume failure invalidates the session. Dropping it
            # on a semantic verdict threw away the resumable thread and paid a
            # full cold-start BOOT_PROMPT on every subsequent wake.
            if harness_failure and self.reception_session_id:
                self.reception_session_id = None
                self._write_session(self._reception_session_path, None)
                self._reception_turns_on_session = 0
            return True
        self._clear_infra_failure()
        self._clear_attempt(key)
        self._quarantine_until.pop(key, None)
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

    # -- claim-gated work chunks (work continuation, 2026-07-28) -------------

    def _owned_live_claims(self) -> list[tuple[str, str, int, dict]]:
        """All non-terminal claims owned by this seat, from existing APIs."""
        found: list[tuple[str, str, int, dict]] = []
        api_key = _config.get_cached_key(self.hub, self.agent_id)
        if not api_key:
            return found
        import urllib.parse
        import httpx
        hdrs = {"Authorization": f"Bearer {api_key}"}
        base = self.hub.rstrip("/")
        terminal = {"done", "shipped", "delivered", "complete", "completed",
                    "closed", "landed", "merged", "released", "resolved",
                    "parked", "paused", "blocked", "on-hold", "onhold",
                    "hold", "deferred"}
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
                    if not key.startswith("claim:"):
                        continue
                    entry = httpx.get(
                        f"{base}/channels/{name}/store/"
                        f"{urllib.parse.quote(key, safe=':')}",
                        headers=hdrs, timeout=5.0,
                    ).json()
                    value = entry.get("value")
                    if not isinstance(value, dict):
                        continue
                    if value.get("owner") != self.agent_id or value.get("done"):
                        continue
                    status = str(value.get("status") or value.get("state") or "").strip().lower()
                    first = (status.split()[0].rstrip(".,;:!—-")
                             if status.split() else "")
                    if first in terminal:
                        continue
                    found.append((name, key, int(entry.get("version", 0)), value))
        except Exception:
            return []
        return found

    def _claim_snapshot(self) -> tuple[str, str, int] | None:
        """(channel, key, version) of the seat's live claim, or None. Read
        with the cached key over EXISTING endpoints (precedent: listen's
        /owed poll). Any failure returns None — initiative fails toward
        silence, never toward burn. A row whose status word says done/
        parked/blocked (or done:true) is not continuable work."""
        claims = self._owned_live_claims()
        if not claims:
            return None
        channel, key, version, _ = claims[0]
        return channel, key, version

    def _work_budget_ok(self) -> bool:
        now = time.time()
        self._work_times = [t for t in self._work_times if now - t < 3600.0]
        return len(self._work_times) < self.work_budget

    def run_work_turn(self) -> bool:
        """Spawn ONE bounded work chunk (WORK_PROMPT, --work-timeout cap).
        Uses a work-only session and does NOT share the wake poison ledger —
        a failing chunk must never quarantine the
        inbox head and deafen reception (composition bug, review
        2026-07-28); chunk failures are bounded by the per-version strike
        ledger in _chain_step instead."""
        sid = self.work_session_id
        prompt = WORK_PROMPT if sid else WORK_BOOT_PROMPT
        self._work_times.append(time.time())
        # An UNPROVEN provider never gets the full --work-timeout: after an
        # infrastructure failure the next chunk is capped at the reception
        # bound, because the cost of guessing wrong is this seat being deaf for
        # the whole window (live 2026-07-31: hour-long chunks against a dead
        # endpoint). One healthy turn restores the full budget.
        self._turn_timeout = (min(self.work_timeout, RECEPTION_TURN_TIMEOUT)
                              if self._infra_failures else self.work_timeout)
        try:
            with self._long_turn_notice("work"):
                new_sid, ok = self._spawn(prompt, sid)
        finally:
            self._turn_timeout = RECEPTION_TURN_TIMEOUT
        if not ok:
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
        """Bind work context to one claim; a different claim boots fresh."""
        ref = f"{channel}/{key}"
        if ref == self._work_claim_ref:
            return
        self._work_claim_ref = ref
        self._write_session(self._work_claim_path, ref)
        self.work_session_id = None
        self._write_session(self._work_session_path, None)
        self._work_turns_on_session = 0

    def _chain_step(self) -> bool:
        """One initiative step at an idle boundary: spawn a work chunk when
        the seat holds a live, progressing claim. Continuation is a LOOP
        property — chunks chain at DRIVE_CHAIN_WAIT listen windows and any
        obligation preempts at the arm between them — never a model
        posture. Strikes are keyed on the claim row's CAS VERSION: a chunk
        that ends without touching the row is a strike; WORK_STRIKES parks
        the chain (recoverable — any row touch mints a fresh version);
        parking is never the wake quarantine."""
        if self._infra_retry_after() > 0:
            # Never spend a work chunk on a provider that is currently failing:
            # the chunk blocks reception for its whole timeout and cannot
            # succeed anyway.
            self._chain_live = False
            return False
        snap = self._claim_snapshot()
        if snap is None:
            self._chain_live = False
            return False
        channel, key, version = snap
        self._activate_work_claim(channel, key)
        ck = f"{channel}/{key}@{version}"
        if self._work_strikes.get(ck, 0) >= WORK_STRIKES:
            if self._chain_live:
                _emit(f"AGORA_DRIVE initiative=parked agent={self.agent_id} "
                      f"key={ck} reason=no-receipt ({WORK_STRIKES} chunks "
                      "left the claim row unchanged; a NEW receipt on the "
                      "row — a version bump — resumes)")
            self._chain_live = False
            return False
        if not self._work_budget_ok():
            if self._chain_live:
                _emit(f"AGORA_DRIVE initiative=parked agent={self.agent_id} "
                      f"reason=work-budget ({self.work_budget}/h)")
            self._chain_live = False
            return False
        ran = self.run_work_turn()
        after = self._claim_snapshot()
        if (after is not None and after[0] == channel and after[1] == key
                and after[2] == version):
            self._work_strikes[ck] = self._work_strikes.get(ck, 0) + 1
        self._chain_live = ran and after is not None
        return ran

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
        driven = 0
        try:
            if once:
                ran = self.run_turn()
                return 0 if ran and self._last_turn_ok else 1
            backoff = 1.0
            while max_turns is None or driven < max_turns:
                self._touch_drive_pid()
                self._mute_notice()
                # A held human/peer debt outranks idle listening. Run it as
                # soon as capacity returns; unowned broadcasts additionally
                # pass the small anti-storm fuse.
                if (self._pending_wake
                        and self._wake_admissible(
                            has_debt=self._pending_wake_has_debt)):
                    has_debt = self._pending_wake_has_debt
                    self._pending_wake = False
                    self._pending_wake_has_debt = False
                    if self.run_turn(broadcast=not has_debt):
                        driven += 1
                    continue
                # source=auto: notify-file tail when the hub is local (0
                # sockets), websocket otherwise — hard-coding "file" made
                # remote seats deaf. signal_passthrough: SIGTERM/SIGINT must
                # kill THIS loop, not be swallowed by the listener's own
                # handlers. Missed-wake recovery is INSIDE run_listen:
                # arming starts with a debt poll (signature-gated), so an
                # obligation that landed mid-turn wakes at the next arm —
                # which, while a chain is live, is at most DRIVE_CHAIN_WAIT
                # away: obligations always preempt the next chunk.
                window = (DRIVE_CHAIN_WAIT if self._chain_live
                          else self.max_wait)
                if self._pending_wake:
                    retry = self._budget_retry_after(
                        has_debt=self._pending_wake_has_debt)
                    window = min(window, max(retry, 0.01))
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
                    if self._wake_admissible(has_debt=has_debt):
                        # This turn drains the WHOLE inbox, held debt
                        # included — a still-set flag would spawn a
                        # spurious turn at the next idle (review F2).
                        self._pending_wake = False
                        self._pending_wake_has_debt = False
                        if self.run_turn(broadcast=not has_debt):
                            driven += 1
                        backoff = 1.0
                    else:
                        # Budget-parked: HOLD the wake instead of sleeping
                        # deaf for 300s. The listener already recorded the
                        # owed signature, so without this flag the debt
                        # would wait for hub escalation (consumed-wake
                        # stall, review 2026-07-28); the flag converts it
                        # into a turn the moment the budget window slides.
                        self._pending_wake = True
                        self._pending_wake_has_debt |= has_debt
                        reason = ("turn-budget" if has_debt
                                  else "broadcast-budget")
                        _emit(f"AGORA_DRIVE parked agent={self.agent_id} "
                              f"reason={reason} "
                              "wake=held")
                elif rc == 0:                 # idle timeout OR hub-unreachable
                    if (self._pending_wake
                            and self._wake_admissible(
                                has_debt=self._pending_wake_has_debt)):
                        has_debt = self._pending_wake_has_debt
                        self._pending_wake = False
                        self._pending_wake_has_debt = False
                        if self.run_turn(broadcast=not has_debt):
                            driven += 1
                        continue
                    # A HELD wake blocks new work chunks (storm review,
                    # 2026-07-28): starting a chunk here could pin the seat
                    # for up to --work-timeout while a human's debt sits at
                    # its exact release point — reception outranks work.
                    if not self._pending_wake:
                        if self._chain_step():
                            driven += 1
                else:                         # unexpected: bounded backoff
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
                    force=force, turn_log=turn_log, cwd=workspace)
    try:
        return driver.run(once=once, max_turns=max_turns)
    except KeyboardInterrupt:
        _emit(f"AGORA_DRIVE event=stopped status=ok agent={aid} "
              "reason=operator-interrupt")
        return 130
