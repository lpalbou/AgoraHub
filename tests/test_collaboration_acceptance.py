"""THE COLLABORATION ACCEPTANCE GATE: does an operator request actually get
DELIVERED by the fleet, unattended?

Everything else in this suite pins a MECHANISM (one endpoint, one predicate,
one refusal). Nothing pinned the PRODUCT: "the human asks for something and the
fleet, with no further human turn, finishes it." That question has only ever
been answered by a hand-run live fleet — hours of tokens, a human reading a
transcript — which is exactly why whole-arc regressions keep reaching
production: every part passed, the collaboration did not happen.

This module is the missing gate: a real hub (in-process, ephemeral, no LLM, no
tokens, seconds) driven through a multi-round arc by SCRIPTED seats. The seats
are deliberately DUMB — they never guess, never poll a peer, never invent work.
Every step they take is one the hub HANDED them, through the production
surfaces (`/owed`, `waiting_on`, phase rows, the real `Driver` continuation
gate). So the arc completes only if the hub's guarantees compose. When one
stops holding, the arc stalls, the harness posts the nudge a human would have
posted, and the test fails naming the property and printing the arc.

---------------------------------------------------------------------------
THE EMPIRICAL RECORD THIS GATE IS BUILT FROM
---------------------------------------------------------------------------
Every property below is grounded in the LIVE fleet record (~/.agora/agora.db
read-only, /tmp/agora-drive-*.log), not in what the docs say should happen.
The measurements, with the queries that produced them:

E1  UNATTENDED MULTI-ROUND ARCS ARE THE NORM — not a single quick exchange.
      sqlite3 'file:~/.agora/agora.db?mode=ro' "SELECT count(*), count(DISTINCT
        sender), round((max(created_at)-min(created_at))/60), sum(sender='laurent')
        FROM messages WHERE channel='msg-445-registry-fix'"
      -> 32 msgs, 8 seats, 204 min, operator msgs = 0
      (last operator message anywhere: 2026-08-01 21:33; that arc ran 08-03
      00:25 -> 03:50.)  The arc here therefore runs MANY rounds, not one.

E2  THE DELEGATE'S TIME IS WORK, NOT TALK.
      grep -o 'kind=[a-z]* dur=[0-9]*s' /tmp/agora-drive-reader.log | awk ...
      -> work turns=18 total=3376s avg=188s | wake turns=8 total=300s avg=38s
      92% of the delegate's driven seconds were self-directed WORK CHUNKS.
      A delegate that only answers messages is a failed delegate.

E3  DECOMPOSE, DISPATCH, AND RE-DISPATCH IS THE DELEGATE'S ACT.
      msg-445-registry-fix: #22 open to=[at1..at5] (asks) -> #23..#27 five
      answers, #26 = at5 DECLINING -> #29 open to=['editor'] with per-ask
      to=['editor'] -> #30 editor answers -> #31/#32 reader resolves.
      at-test#446 fanned 7 seats; at-test#447 "Claim opened ...".

E4  45% OF NAMED SEATS NEVER REPLY — the silent seat is the MEDIAN case.
      Over 6 days: 145 ask-bearing messages, 187 named seats, 84 never replied.
      at-test#439 named [editor, reviewer]; reviewer never replied, yet its
      cursor is 459 (> 439) => the hub knew "acked past, no reply"; the arc
      still closed at #442/#443 on the answers the delegate had.
      => the adversarial variant below is not a corner case, it is Tuesday.

E5  THE DELEGATE ESCALATES TO THE HUMAN WHEN THE BLOCKER IS OUTSIDE ITS POWER.
      dm:laurent--reader, 17 delegate DMs on 08-01, e.g. "Operator action
      needed: relaunch offline at-test seats", "Stale-claim batch still blocked
      on the same offline owners ... still blocked on operator restart".

E6  CLAIMS ACCUMULATE RECEIPTS ACROSS CHUNKS, LINKED TO THE REQUEST.
      store: at-test/claim:msg-445 v26 owner=reader source_message_id=01KYZ...;
      claim:msg-375 v148; claim:msg-396 v12; phase:manuscript v10 steward=reader
      status=complete paths=["manuscript.md"].

E7  INITIATIVE IS BOUNDED, LIVE.
      /tmp/agora-drive-reader.log: "initiative=parked ... reason=no-receipt
      (3 chunks left the row unchanged)".

E8  A RECEPTION TURN THAT LEAVES ITS DEBT IS SCORED FAILED, MUTED AND HELD —
    AND THE RETRY IS WHAT DELIVERS.
      /tmp/agora-drive-editor.log: "reason=debt-remains ...
      pending_without_linked_claim=01KZ2HH7AN..." then "mute ... wake=held",
      then a 128s turn in which editor posted msg-445#30 answering ask "1".
      => the verdict must never fire on a seat that DID answer the asks naming
      it (that is the at-test#446 mass-mute), and a mute must never be terminal.

E9  BATCHED SETTLEMENT IS IN LIVE USE: msg-396-preface 2 messages carrying
    `consumes`, work-split-for-396 4.

---------------------------------------------------------------------------
WHAT "SMART COLLABORATION" MEANS HERE, AS PROPERTIES
---------------------------------------------------------------------------
  ROUTING   — work reaches exactly the seats it names (E3): an operator
              request obliges the reporting delegate whatever its status and
              whether or not it names anyone; a fan-out obliges the named and
              NOBODY else; an unaddressed peer broadcast obliges no one.
  PROGRESS  — a seat with continuable work keeps getting turns unattended
              (E2/E6/E7), an idle seat burns none, and a seat that answered
              the asks naming it is never scored as having failed (E8).
  MONITOR   — the delegate can SEE who has not delivered (`waiting_on`,
              E4) and act on it without a human.
  CLOSURE   — debts end: one `consumes` settles N (E9), an answered ask leaves
              /owed, and a bystander cannot close a human's request.
  PHASE     — the current phase is a fact on every reception pass (E6), and a
              write to a registered path rings the advisory.
  VOTES     — the window binds and the HUB publishes the tally with no chair.
  CHARTER   — a stale charter view is stated once and clears on read.
  THE ARC   — ack -> claim -> decompose/dispatch -> monitor & chase -> repeated
              own work chunks -> VERIFY the artifact against the request ->
              report and close: many rounds, ZERO human nudges, no ceremony,
              nobody muted for behaving correctly.
  ADVERSARY — a seat goes dark mid-arc and never answers. The delegate must
              NOTICE and either route around it or escalate to the operator;
              the arc must terminate, and the test proves WHICH happened.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agora import config as _config
from agora.drive import Driver, TurnEvidence
from agora.hub.app import create_app
from agora.hub.obligations import discharge_state
from agora.listen import qualifies
from agora.vote import (VOTE_DATA_KEY, VOTE_RESULT_KEY, build_vote_post,
                        published_result)

ADMIN_KEY = "acceptance-admin"
HUB_URL = "http://testserver"          # TestClient's own base_url
ROOM = "build"
ARTIFACT = "build/summary.md"

#: The operator's request, decomposed into the three things that must be TRUE
#: of the artifact before anyone may report completion, and the ask each one
#: waits on. The delegate proves every requirement against the HUB's copy of
#: the file and the HUB's record of who answered — never against its own
#: memory of having asked. `a2r` is the re-dispatch of `a2` after its owner
#: goes dark (live shape: msg-445#22 -> #29).
REQUIREMENT_ASKS = {"## export": ("a1",),
                    "## verified": ("a2", "a2r"),
                    "## changelog": ("a3",)}
REQUIREMENTS = tuple(REQUIREMENT_ASKS)


# ---------------------------------------------------------------------------
# The arc recorder: what a human reads when this gate goes red
# ---------------------------------------------------------------------------

class Arc:
    """Ordered record of everything the fleet did, plus the counters that make
    "it worked" measurable: NUDGES (a human had to push), CEREMONY (messages
    carrying no new information), and MUTES (the driver scored a correct turn
    as a failure)."""

    def __init__(self) -> None:
        self.events: list[tuple[float, str, str, str]] = []
        self.t0 = time.time()
        self.round = 0
        self.nudges: list[str] = []
        self.mutes: list[str] = []
        self.work_chunks: dict[str, int] = {}
        self.reception_turns: dict[str, int] = {}
        self.capabilities: set[str] = set()

    def note(self, actor: str, verb: str, detail: str = "") -> None:
        self.events.append((self.round, actor, verb, detail))

    def used(self, capability: str) -> None:
        self.capabilities.add(capability)

    def nudge(self, why: str) -> None:
        self.nudges.append(why)
        self.note("HUMAN", "NUDGE", why)

    def render(self) -> str:
        head = (f"--- the arc: {len(self.events)} events, "
                f"{len(self.nudges)} human nudges, {len(self.mutes)} mutes, "
                f"work chunks {self.work_chunks}, "
                f"reception turns {self.reception_turns} ---")
        lines = [head]
        for rnd, actor, verb, detail in self.events:
            lines.append(f"  r{rnd:<3} {actor:<10} {verb:<24} {detail}")
        lines.append(f"--- capabilities exercised: "
                     f"{sorted(self.capabilities) or 'NONE'} ---")
        return "\n".join(lines)


def broke(prop: str, why: str, arc: Arc | None = None) -> None:
    """Fail naming the collaboration PROPERTY, not the expression."""
    tail = f"\n\n{arc.render()}" if arc is not None else ""
    pytest.fail(f"COLLABORATION PROPERTY BROKEN — {prop}\n  {why}{tail}")


def require(cond: Any, prop: str, why: str, arc: Arc | None = None) -> None:
    if not cond:
        broke(prop, why, arc)


# ---------------------------------------------------------------------------
# The fleet: a real hub, real seats, scripted behaviour
# ---------------------------------------------------------------------------

class Fleet:
    """An ephemeral hub plus the seats of a realistic delivery team."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.notify_dir = tmp_path / "notify"
        self.app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                              rate_per_minute=100000.0,
                              notify_dir=str(self.notify_dir),
                              dark_watch_seconds=0.0, vote_watch_seconds=0.0)
        self.client = TestClient(self.app)
        self.service = self.app.state.service
        self.arc = Arc()
        self.headers: dict[str, dict[str, str]] = {}

    # -- wiring --------------------------------------------------------------

    def register(self, agent_id: str, operator: bool = False) -> str:
        r = self.client.post("/agents",
                             json={"id": agent_id, "mission": f"seat {agent_id}", "operator": operator},
                             headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert r.status_code == 200, r.text
        key = r.json()["api_key"]
        self.headers[agent_id] = {"Authorization": f"Bearer {key}"}
        _config.cache_key(HUB_URL, agent_id, key)     # what a driver reads
        return key

    def make_room(self, owner: str, name: str, members: list[str]) -> None:
        r = self.client.post("/channels", json={"name": name, "private": False},
                             headers=self.headers[owner])
        assert r.status_code == 200, r.text
        for seat in [owner, *members]:
            j = self.client.post(f"/channels/{name}/join", json={},
                                 headers=self.headers[seat])
            assert j.status_code == 200, j.text
        self.arc.note(owner, "opened room", f"#{name} + {members}")

    def set_sla(self, owner: str, channel: str, minutes: float) -> None:
        """Compress the horizon: the live arcs run for hours (E1), so the
        hub's escalation of a rotting obligation is part of what a delegate
        sees. A tiny SLA reproduces that pressure in test time."""
        self.client.put(f"/channels/{channel}/store/channel%3Ameta",
                        json={"value": {"response_sla_minutes": minutes}},
                        headers=self.headers[owner])

    def delegate(self, agent_id: str, powers: tuple[str, ...] = ("reporting",)) -> None:
        r = self.client.put("/admin/delegation",
                            json={"agent_id": agent_id, "powers": list(powers)},
                            headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert r.status_code == 200, r.text
        self.arc.note(agent_id, "delegated", ",".join(powers))

    # -- the surfaces a seat actually calls -----------------------------------

    def post(self, actor: str, channel: str, note: str = "",
             **payload: Any) -> dict[str, Any]:
        r = self.client.post(f"/channels/{channel}/messages", json=payload,
                             headers=self.headers[actor])
        if r.status_code != 200:
            broke("hub refused a legitimate post",
                  f"{actor} -> #{channel}: {r.status_code} {r.text}", self.arc)
        msg = r.json()
        data = payload.get("data") or {}
        tags = [k for k in ("asks", "answers", "consumes") if payload.get(k)]
        tags += [k for k in ("vote", "vote_result") if data.get(k)]
        self.arc.note(actor, f"post {payload.get('status', 'fyi')}",
                      f"#{channel}#{msg['seq']} {note}"
                      + (f" [{'+'.join(tags)}]" if tags else ""))
        return msg

    def dm(self, actor: str, peer: str, note: str = "", **payload: Any) -> dict[str, Any]:
        r = self.client.post(f"/dms/{peer}/messages", json=payload,
                             headers=self.headers[actor])
        if r.status_code != 200:
            broke("hub refused a legitimate DM",
                  f"{actor} -> {peer}: {r.status_code} {r.text}", self.arc)
        self.arc.note(actor, "dm", f"-> {peer}: {note or payload.get('body', '')[:48]}")
        return r.json()

    def owed(self, actor: str) -> dict[str, Any]:
        r = self.client.get("/owed", headers=self.headers[actor])
        assert r.status_code == 200, r.text
        return r.json()

    def store_set(self, actor: str, channel: str, key: str, value: Any,
                  expect_version: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"value": value}
        if expect_version is not None:
            body["expect_version"] = expect_version
        r = self.client.put(f"/channels/{channel}/store/{_quote(key)}",
                            json=body, headers=self.headers[actor])
        if r.status_code != 200:
            broke("hub refused a legitimate store write",
                  f"{actor} {key}: {r.status_code} {r.text}", self.arc)
        self.arc.note(actor, "store_set", f"{key} v{r.json()['version']} "
                                          f"{json.dumps(value)[:64]}")
        return r.json()

    def store_get(self, actor: str, channel: str, key: str) -> dict[str, Any] | None:
        r = self.client.get(f"/channels/{channel}/store/{_quote(key)}",
                            headers=self.headers[actor])
        return r.json() if r.status_code == 200 else None

    def fs_write(self, actor: str, channel: str, path: str, content: str,
                 expect_version: int | None = None,
                 description: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"content": content, "description": description}
        if expect_version is not None:
            body["expect_version"] = expect_version
        r = self.client.put(f"/channels/{channel}/fs/{path}", json=body,
                            headers=self.headers[actor])
        if r.status_code != 200:
            broke("hub refused a legitimate fs write",
                  f"{actor} {path}: {r.status_code} {r.text}", self.arc)
        self.arc.note(actor, "fs_write", f"{path} v{r.json()['version']}")
        return r.json()

    def fs_read(self, actor: str, channel: str, path: str) -> dict[str, Any] | None:
        r = self.client.get(f"/channels/{channel}/fs/{path}",
                            headers=self.headers[actor])
        return r.json() if r.status_code == 200 else None

    def fs_list(self, actor: str, channel: str) -> list[dict[str, Any]]:
        r = self.client.get(f"/channels/{channel}/fs",
                            headers=self.headers[actor])
        return r.json() if r.status_code == 200 else []

    def read_message(self, actor: str, channel: str, message_id: str) -> None:
        self.client.get(f"/channels/{channel}/messages/{message_id}",
                        headers=self.headers[actor])

    def history(self, actor: str, channel: str) -> list[dict[str, Any]]:
        r = self.client.get(f"/channels/{channel}/messages",
                            params={"limit": 500}, headers=self.headers[actor])
        assert r.status_code == 200, r.text
        return r.json()

    def notify(self, agent_id: str) -> list[dict[str, Any]]:
        p = self.notify_dir / f"{agent_id}-inbox.log"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line]

    def woke(self, agent_id: str) -> list[dict[str, Any]]:
        """The events that would ACTUALLY wake this seat's listener."""
        return [e for e in self.notify(agent_id)
                if qualifies(e, agent_id, important_only=True)]

    # -- hub truth, for assertions -------------------------------------------

    def discharge(self, message_id: str):
        parent = self.service.db.get_message(message_id)
        return discharge_state(parent, self.service.db.replies_to(message_id),
                               self.service.operator_ids(),
                               self.service.reporting_delegate_ids())


def _quote(key: str) -> str:
    from urllib.parse import quote
    return quote(key, safe="")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An isolated AGORA_HOME: driver session/pid state and the key cache never
    touch the operator's live ~/.agora."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AGORA_HOME", str(h))
    return h


@pytest.fixture()
def fleet(tmp_path, home, monkeypatch) -> Fleet:
    f = Fleet(tmp_path)

    # The real Driver's continuation gate reads the hub over module-level
    # httpx; point that at this in-process hub so the gate under test is the
    # PRODUCTION one and not a mock of it.
    def _get(url, **kw):
        kw.pop("timeout", None)
        return f.client.get(url, headers=kw.get("headers"))

    monkeypatch.setattr(httpx, "get", _get)
    return f


@pytest.fixture()
def team(fleet: Fleet) -> Fleet:
    """operator + reporting delegate + two working seats + one bystander —
    the shape of the live at-test fleet, minus the seats that only watch."""
    fleet.register("laurent", operator=True)
    for seat in ("reader", "writer", "checker", "bystander"):
        fleet.register(seat)
    fleet.make_room("reader", ROOM, ["laurent", "writer", "checker", "bystander"])
    fleet.set_sla("reader", ROOM, 0.002)      # compressed horizon (E1)
    fleet.delegate("reader")
    return fleet


def _owed_ids(fleet: Fleet, seat: str) -> set[str]:
    return {row["id"] for row in fleet.owed(seat)["to_answer"]}


def _driver(fleet: Fleet, agent_id: str, spawn) -> Driver:
    return Driver(agent_id, HUB_URL, spawn=spawn, cwd=fleet.tmp,
                  work_budget=200, max_wait=0.01)


def chain_step(driver: Driver) -> bool:
    """Ask the PRODUCTION initiative gate for one work chunk, at whatever
    arity it currently has.

    This gate tests the BEHAVIOUR of continuation, not drive.py's private
    signature: the loop was refactored on 2026-08-03 to compute the snapshot
    once and pass it in. Pinning the arity here would turn every legitimate
    refactor of the engine into a red acceptance run, which is exactly the
    wrong incentive — the engine's own unit tests own its shape."""
    import inspect
    if inspect.signature(driver._chain_step).parameters:
        return bool(driver._chain_step(driver._continuation_snapshot()))
    return bool(driver._chain_step())


# ===========================================================================
# ROUTING — work reaches exactly the seats it names            (evidence E3)
# ===========================================================================

@pytest.mark.parametrize("status", ["open", "reply", "fyi"])
def test_routing_an_operator_request_always_obliges_the_delegate(team: Fleet, status):
    """ROUTING/operator-request-lands-on-the-delegate.

    A request that obliges nobody is the failure that produced NOTHING on
    2026-08-01 while every seat was alive and heartbeating. Whatever status
    the human used, and whether or not they named anyone, the seat
    responsible end-to-end owes it.

    Note what the live record shows when this rule is NOT relied on: the last
    operator post was at-test#438 (08-01 21:33), and the 08-03 arc started at
    at-test#445 — a RELAY by another seat ("Operator revision request —
    reader, yours END TO END as reporting delegate", sender `agora`,
    to=["reader"]). A relay is a seat doing by hand what this predicate does
    mechanically, and it only works while some seat remembers to write it."""
    fleet = team
    root = fleet.post("reader", ROOM, status="open", title="export",
                      body="production export prepared", note="thread root")
    kw: dict[str, Any] = {"status": status, "to": []}
    if status == "reply":
        kw["reply_to"] = root["id"]
    ask = fleet.post("laurent", ROOM, note="THE REQUEST", title="ship it",
                     body="finish the export summary and verify it", **kw)

    require(ask["id"] in _owed_ids(fleet, "reader"),
            "ROUTING/operator-request-lands-on-the-delegate",
            f"the operator posted status={status} to=[] and the reporting "
            f"delegate does not owe it — the request obliges NOBODY, which is "
            f"the at-test#396 hole", fleet.arc)
    for bystander in ("writer", "checker", "bystander"):
        require(ask["id"] not in _owed_ids(fleet, bystander),
                "ROUTING/an-operator-request-does-not-oblige-the-whole-room",
                f"{bystander} was pinned by an unaddressed operator message — "
                f"that is the wake-storm shape, not routing", fleet.arc)


def test_routing_a_fanout_obliges_exactly_the_named_seats(team: Fleet):
    """ROUTING/per-ask-addressing-binds-exactly-the-named.

    Decomposition is the delegate's core act (live: at-test#446 fanned 7 seats;
    msg-445#29 named exactly ['editor']). It is worth something only if the hub
    turns each named ask into that seat's own debt and leaves everyone else
    alone — names in prose flag nobody (70 misses in 48h)."""
    fleet = team
    fan = fleet.post("reader", ROOM, status="open", title="decomposition",
                     note="fan-out", body="two parts, one each",
                     asks=[{"id": "a1", "text": "draft the summary",
                            "to": ["writer"]},
                           {"id": "a2", "text": "verify the numbers",
                            "to": ["checker"]}])

    rows = {seat: {r["id"]: r for r in fleet.owed(seat)["to_answer"]}
            for seat in ("writer", "checker", "bystander", "laurent")}
    require(rows["writer"].get(fan["id"], {}).get("asks_naming_you") == ["a1"],
            "ROUTING/per-ask-addressing-binds-exactly-the-named",
            f"writer's own ask is not machine-readable on /owed: "
            f"{rows['writer'].get(fan['id'])}", fleet.arc)
    require(rows["checker"].get(fan["id"], {}).get("asks_naming_you") == ["a2"],
            "ROUTING/per-ask-addressing-binds-exactly-the-named",
            f"checker's own ask is not machine-readable on /owed: "
            f"{rows['checker'].get(fan['id'])}", fleet.arc)
    for quiet in ("bystander", "laurent"):
        require(fan["id"] not in rows[quiet],
                "ROUTING/bystanders-are-not-pinned",
                f"{quiet} is named by no ask yet owes the fan-out — every "
                f"un-named seat pays attention tax for someone else's work",
                fleet.arc)

    # ...and the wake follows the pin: the named seats wake, the bystander does not.
    for named in ("writer", "checker"):
        require(any(e["id"] == fan["id"] for e in fleet.woke(named)),
                "ROUTING/a-named-seat-is-woken",
                f"{named} owns an ask on {fan['id']} and its listener would "
                f"not wake — an ask that reaches nobody is dead air", fleet.arc)
    require(not any(e["id"] == fan["id"] for e in fleet.woke("bystander")),
            "ROUTING/bystanders-are-not-woken",
            "the bystander's listener wakes on an addressed fan-out that names "
            "someone else (the 62%-of-commons-wakes regression)", fleet.arc)


def test_routing_an_unaddressed_peer_broadcast_obliges_nobody(team: Fleet):
    """ROUTING/an-unaddressed-peer-broadcast-obliges-nobody.

    The complement of the operator rule. A PEER'S room-wide open is mail: it is
    delivered and it may wake, but it pins no one — otherwise every open post
    in a busy room becomes five seats' debt and /owed stops meaning anything."""
    fleet = team
    shout = fleet.post("writer", ROOM, status="open", title="thoughts?",
                       body="anyone have opinions on the format?", to=[],
                       note="unaddressed peer broadcast")
    for seat in ("reader", "checker", "bystander", "laurent"):
        require(shout["id"] not in _owed_ids(fleet, seat),
                "ROUTING/an-unaddressed-peer-broadcast-obliges-nobody",
                f"{seat} owes a peer's addresseeless open — obligation "
                f"inflation makes /owed unreadable", fleet.arc)
    require(any(e["id"] == shout["id"] for e in fleet.notify("checker")),
            "ROUTING/an-unaddressed-open-is-still-visible",
            "an addresseeless open vanished from the room entirely", fleet.arc)
    require(not any(e["id"] == shout["id"] for e in fleet.woke("checker")),
            "ROUTING/an-unaddressed-open-does-not-buy-a-turn",
            "an addresseeless peer open still wakes checker — room chatter "
            "became wakeful work again", fleet.arc)


# ===========================================================================
# PROGRESS — a seat with work keeps getting turns             (evidence E2/E6/E7)
# ===========================================================================

def test_progress_a_live_claim_keeps_earning_work_turns(team: Fleet):
    """PROGRESS/a-live-claim-keeps-earning-work-turns.

    The difference between a fleet and a chat room. Live: the delegate spent
    18 work turns / 3376s against 8 reception turns / 300s (E2), and
    claim:msg-445 reached v26 (E6) — one receipt per slice. Measured failure
    the gate exists for: a delegate holding an open claim took ZERO chunks
    across a 49-minute arc and moved only when a human posted."""
    fleet = team
    fleet.store_set("reader", ROOM, "claim:export",
                    {"owner": "reader", "status": "in_progress",
                     "note": "the export summary"})

    chunks: list[str] = []

    def spawn(prompt, session_id):
        # A well-behaved chunk leaves a RECEIPT on the row (a version bump).
        chunks.append(prompt.splitlines()[0])
        cur = fleet.store_get("reader", ROOM, "claim:export")
        fleet.store_set("reader", ROOM, "claim:export",
                        {**cur["value"], "note": f"slice {len(chunks)}"},
                        expect_version=cur["version"])
        return f"work-{len(chunks)}", True

    d = _driver(fleet, "reader", spawn)
    for _ in range(4):
        require(chain_step(d),
                "PROGRESS/a-live-claim-keeps-earning-work-turns",
                f"the chain stopped after {len(chunks)} chunks with the claim "
                f"still live and no nudge in sight", fleet.arc)
    require(len(chunks) == 4, "PROGRESS/a-live-claim-keeps-earning-work-turns",
            f"expected 4 work chunks, got {len(chunks)}", fleet.arc)

    # ...and a seat with NOTHING continuable burns no turns at all.
    idle = _driver(fleet, "bystander", spawn)
    before = len(chunks)
    require(chain_step(idle) is False,
            "PROGRESS/an-idle-seat-burns-no-turns",
            "a seat holding no claim and stewarding no phase spawned a work "
            "chunk — that is tokens for nothing", fleet.arc)
    require(len(chunks) == before, "PROGRESS/an-idle-seat-burns-no-turns",
            "the idle seat's chunk actually ran", fleet.arc)

    # ...and a DONE claim stops the chain: finished work is not fuel.
    cur = fleet.store_get("reader", ROOM, "claim:export")
    fleet.store_set("reader", ROOM, "claim:export",
                    {**cur["value"], "done": True, "status": "done"},
                    expect_version=cur["version"])
    require(chain_step(d) is False,
            "PROGRESS/a-finished-claim-stops-the-chain",
            "the chain kept spawning chunks against a claim marked done",
            fleet.arc)


def test_progress_a_stewarded_open_phase_is_also_continuable(team: Fleet):
    """PROGRESS/a-stewarded-open-phase-is-continuable-work.

    Field evidence 2026-07-31: a delegate whose only claim was `blocked` but
    who stewarded an open phase read as "nothing to do" and moved only on human
    nudges. Live phase rows are real (E6: phase:manuscript v10, steward=reader):
    a steward with an open phase has pending work by definition — the phase does
    not close until they act."""
    fleet = team
    fleet.store_set("reader", ROOM, "phase:export",
                    {"current": "export", "status": "open", "next": "verify",
                     "steward": "reader", "paths": [ARTIFACT]})
    fleet.store_set("reader", ROOM, "claim:blocked-thing",
                    {"owner": "reader", "blocked_on": "external", "needs": "the vendor build to land", "status": "blocked",
                     "note": "waiting on an external tool"})

    ran: list[int] = []

    def spawn(prompt, session_id):
        ran.append(1)
        return "s", True

    d = _driver(fleet, "reader", spawn)
    require(d._continuation_snapshot() is not None,
            "PROGRESS/a-stewarded-open-phase-is-continuable-work",
            "a steward of an OPEN phase, whose only claim is blocked, reads as "
            "having nothing to continue — the 24-turn zero-chunk stall",
            fleet.arc)
    require(chain_step(d) and ran,
            "PROGRESS/a-stewarded-open-phase-is-continuable-work",
            "the stewarded phase did not ignite a work chunk", fleet.arc)

    # A seat that is NOT the steward reads the same row and parks.
    other = _driver(fleet, "checker", spawn)
    require(other._continuation_snapshot() is None,
            "PROGRESS/a-phase-ignites-only-its-steward",
            "a non-steward burned a chunk on someone else's phase", fleet.arc)


def test_progress_a_receiptless_chain_parks_instead_of_spinning(team: Fleet):
    """PROGRESS/a-chain-that-leaves-no-receipt-parks.

    The other half of continuation: initiative must be BOUNDED. Live (E7):
    "initiative=parked ... reason=no-receipt (3 chunks left the row
    unchanged)". Chunks that never touch the row are not progress."""
    fleet = team
    fleet.store_set("reader", ROOM, "claim:silent",
                    {"owner": "reader", "status": "in_progress"})
    ran: list[int] = []

    def spawn(prompt, session_id):
        ran.append(1)                      # never touches the row
        return "s", True

    d = _driver(fleet, "reader", spawn)
    for _ in range(6):
        chain_step(d)
    require(len(ran) == 3, "PROGRESS/a-chain-that-leaves-no-receipt-parks",
            f"a receipt-less chain ran {len(ran)} chunks; the strike bound is "
            f"3 — an agent that posts nothing must not burn the budget",
            fleet.arc)


def test_progress_answering_the_asks_that_name_you_is_never_a_failed_turn(
        team: Fleet):
    """PROGRESS/answering-your-own-asks-is-a-good-turn.

    The at-test#446 mass-mute: `pending_asks` on a row is MESSAGE-global, so a
    seat that answered its own ask still saw pending ids and the driver scored
    its turn as unsettled debt — 5 of 7 seats at once, muted for doing exactly
    the addressed work they were handed. E8 shows the verdict firing correctly
    on a seat that answered NOTHING; nothing may punish one that did."""
    fleet = team
    fan = fleet.post("reader", ROOM, status="open", title="decomposition",
                     body="two parts", note="fan-out",
                     asks=[{"id": "a1", "text": "draft", "to": ["writer"]},
                           {"id": "a2", "text": "verify", "to": ["checker"]}])

    d = _driver(fleet, "writer", lambda p, s: ("s", True))
    d._reception_debt_before = d._reception_debt()
    d._reception_debt_verification_required = True
    require(d._reception_debt_before is not None
            and not d._reception_debt_before.empty,
            "PROGRESS/the-driver-can-read-its-own-debt",
            "the driver could not read /owed for a seat that plainly owes an "
            "ask", fleet.arc)

    fleet.post("writer", ROOM, status="reply", reply_to=fan["id"],
               answers=["a1"], body=f"draft is at {ARTIFACT}",
               note="answers ONLY its own ask")

    verdict = d._verify_reception_debt(TurnEvidence(ok=True), "wake")
    require(verdict.ok, "PROGRESS/answering-your-own-asks-is-a-good-turn",
            f"a seat that answered exactly the ask naming it was scored "
            f"FAILED ({verdict.stage}: {verdict.detail}) — this is the "
            f"mass-mute, and a muted seat stops collaborating", fleet.arc)

    # ...while a seat that ignores its debt is still caught (E8's true case).
    lazy = _driver(fleet, "checker", lambda p, s: ("s", True))
    lazy._reception_debt_before = lazy._reception_debt()
    lazy._reception_debt_verification_required = True
    lazy_verdict = lazy._verify_reception_debt(TurnEvidence(ok=True), "wake")
    require(not lazy_verdict.ok,
            "PROGRESS/an-ignored-debt-is-still-a-failed-turn",
            "a seat that answered nothing passed verification — the gate that "
            "makes reception structural is off", fleet.arc)


# ===========================================================================
# MONITOR — the delegate can SEE who has not delivered         (evidence E4)
# ===========================================================================

def test_monitor_the_hub_tells_the_asker_who_has_not_delivered(team: Fleet):
    """MONITOR/the-asker-is-told-who-has-not-delivered-and-why.

    45% of named seats never reply (E4). A delegate responsible end-to-end must
    therefore be able to tell, without a human, "who owes me and did they even
    see it" — and the two cases need different acts: a seat that acked past the
    ask and stayed silent is a CHASE candidate; a seat the hub never served is
    a reachability problem to escalate (E5). at-test#439 is the live instance:
    reviewer never replied, and its cursor (459) was already past the ask."""
    fleet = team
    fan = fleet.post("reader", ROOM, status="open", title="decomposition",
                     body="two parts", note="fan-out",
                     asks=[{"id": "a1", "text": "draft", "to": ["writer"]},
                           {"id": "a2", "text": "verify", "to": ["checker"]}])

    waiting = {(r["seat"], r["ask"]): r["state"]
               for r in fleet.owed("reader")["waiting_on"]}
    require(waiting == {("writer", "a1"): "not-yet-acked",
                        ("checker", "a2"): "not-yet-acked"},
            "MONITOR/the-asker-is-told-who-has-not-delivered-and-why",
            f"the delegate cannot see who its outstanding asks are on: "
            f"{waiting} — monitoring would have to be guesswork or polling",
            fleet.arc)

    # writer answers; checker only ACKS (reads past it) and says nothing.
    fleet.post("writer", ROOM, status="reply", reply_to=fan["id"],
               answers=["a1"], body="drafted", note="answers a1")
    fleet.client.post("/inbox/ack", json={"cursors": {ROOM: fan["seq"]}},
                      headers=fleet.headers["checker"])

    waiting = {(r["seat"], r["ask"]): r["state"]
               for r in fleet.owed("reader")["waiting_on"]}
    require(waiting == {("checker", "a2"): "acked-past-no-reply"},
            "MONITOR/a-delivered-answer-stops-being-a-wait",
            f"after one answer and one silent ack the delegate sees {waiting} "
            f"— it can neither stop chasing the seat that delivered nor tell "
            f"that the silent one actually saw the ask", fleet.arc)

    # And the hub applies its own pressure on the unanswered half.
    time.sleep(0.15)                       # past the compressed SLA
    row = next(r for r in fleet.owed("checker")["to_answer"]
               if r["id"] == fan["id"])
    require(row["escalated"] is True,
            "MONITOR/an-unanswered-ask-escalates-on-its-own",
            "an ask past the channel's response window is not escalated — the "
            "delegate's chase would be the only pressure in the system",
            fleet.arc)


# ===========================================================================
# CLOSURE — debts end, and only the right seat may end them   (evidence E9)
# ===========================================================================

def test_closure_one_consumes_message_settles_every_debt(team: Fleet):
    """CLOSURE/consumes-settles-N-debts-with-one-message.

    Field measurement: one seat posted TEN identical "adopted and consumed"
    messages in one second; 26% of 253 messages carried zero information.
    Batching is in live use today (E9) and is what makes a wide fan-out
    affordable."""
    fleet = team
    fan = fleet.post("reader", ROOM, status="open", title="canvass",
                     body="three parts", note="fan-out",
                     asks=[{"id": "a1", "text": "one", "to": ["writer"]},
                           {"id": "a2", "text": "two", "to": ["checker"]},
                           {"id": "a3", "text": "three", "to": ["bystander"]}])
    for seat, ask in (("writer", "a1"), ("checker", "a2"), ("bystander", "a3")):
        fleet.post(seat, ROOM, status="reply", reply_to=fan["id"],
                   answers=[ask], body=f"{seat} answers {ask}")
        require(fan["id"] not in _owed_ids(fleet, seat),
                "CLOSURE/an-answered-ask-leaves-your-owed",
                f"{seat} answered {ask} and still owes the fan-out — a debt "
                f"that survives its own discharge never stops re-waking",
                fleet.arc)

    consume_rows = fleet.owed("reader")["to_consume"]
    require(len(consume_rows) == 3, "CLOSURE/answers-become-consumption-debt",
            f"the asker should owe 3 consumptions, has {len(consume_rows)}",
            fleet.arc)
    fleet.post("reader", ROOM, status="fyi", body="all three folded in; "
               "drafting against claim:export now", note="ONE batch receipt",
               consumes=[r["answer_id"] for r in consume_rows])
    left = fleet.owed("reader")["to_consume"]
    require(left == [], "CLOSURE/consumes-settles-N-debts-with-one-message",
            f"one batched receipt left {len(left)} debts standing — the fleet "
            f"is back to O(n) ceremony messages", fleet.arc)


def test_closure_a_bystander_cannot_close_a_humans_request(team: Fleet):
    """CLOSURE/only-the-operator-or-the-delegate-closes-a-human-request.

    The 75-second discharge: the operator posted five requirements, one seat
    answered part of it, and the thread read as settled — four requirements
    silently abandoned. A human's broadcast is the delegate's to finish."""
    fleet = team
    ask = fleet.post("laurent", ROOM, status="open", title="five things",
                     body="1) export 2) verify 3) publish 4) note 5) report",
                     to=[], note="THE REQUEST")

    fleet.post("writer", ROOM, status="reply", reply_to=ask["id"],
               body="did the export", note="partial reply from a bystander")
    require(not fleet.discharge(ask["id"]).closed,
            "CLOSURE/only-the-operator-or-the-delegate-closes-a-human-request",
            "a bystander's partial reply closed the human's request", fleet.arc)
    require(ask["id"] in _owed_ids(fleet, "reader"),
            "CLOSURE/a-partial-reply-does-not-release-the-delegate",
            "the delegate stopped owing the operator's request because someone "
            "else spoke in the thread", fleet.arc)

    fleet.post("writer", ROOM, status="resolved", reply_to=ask["id"],
               body="calling this done", note="bystander tries to CLOSE")
    require(not fleet.discharge(ask["id"]).closed,
            "CLOSURE/only-the-operator-or-the-delegate-closes-a-human-request",
            "a bystander's bare `resolved` closed the human's request — "
            "closure by strangers needs an audited pointer", fleet.arc)

    # A completion report that POINTS AT NOTHING is REFUSED outright
    # (2026-08-12; before that it was silently ineffective — 2026-08-04's
    # "5.1MB, 3 embedded images … /path/to/novel" shape, where the channel
    # filesystem held no such file, and live 2026-08-11, where a delegate
    # posted three uncited "delivery complete" resolveds in a row).
    r = fleet.client.post(f"/channels/{ROOM}/messages",
                          json={"status": "resolved", "reply_to": ask["id"],
                                "body": "all five delivered"},
                          headers=fleet.headers["reader"])
    require(r.status_code == 400 and "data.evidence" in r.text,
            "CLOSURE/an-uncited-completion-report-is-refused",
            "a bare `resolved` from the delegate was accepted — a delivery "
            "nobody can check is indistinguishable from a lie", fleet.arc)
    require(not fleet.discharge(ask["id"]).closed,
            "CLOSURE/an-uncited-completion-report-closes-nothing",
            "the refused report still closed the human's request", fleet.arc)

    fleet.fs_write("reader", ROOM, ARTIFACT, "requirement A\nrequirement B\n",
                   description="the deliverable the operator asked for")
    head = fleet.fs_read("reader", ROOM, ARTIFACT)
    # The room has peers, so the report must also cite the agreed plan and a
    # peer-authored review (2026-08-12): an uncontested delivery is refused.
    fleet.store_set("reader", ROOM, "plan:build",
                    {"slices": {"reader": "deliver", "writer": "review"}})
    fleet.fs_write("writer", ROOM, "review-build.md",
                   "verdict: checked against the request\n",
                   description="peer review of the delivery")
    review_head = fleet.fs_read("writer", ROOM, "review-build.md")
    fleet.post("reader", ROOM, status="resolved", reply_to=ask["id"],
               body=f"all five delivered; report in {ARTIFACT}",
               data={"evidence": [
                   {"kind": "store", "ref": "plan:build"},
                   {"kind": "fs", "ref": f"{ARTIFACT}@{head['version']}"},
                   {"kind": "fs",
                    "ref": f"review-build.md@{review_head['version']}"}]},
               note="the DELEGATE closes: artifact + plan + peer review cited")
    require(fleet.discharge(ask["id"]).closed,
            "CLOSURE/the-delegate-can-close-what-it-owns",
            "the reporting delegate asserted end-to-end completion and the hub "
            "did not close the request — nothing can ever be finished",
            fleet.arc)
    require(ask["id"] not in _owed_ids(fleet, "reader"),
            "CLOSURE/a-closed-request-leaves-owed",
            "a closed request still sits in the delegate's /owed", fleet.arc)


# ===========================================================================
# PHASE — the current phase is a fact every reception pass reads
# ===========================================================================

def test_phase_order_is_served_to_every_seat_and_rings_on_registered_paths(
        team: Fleet):
    """PHASE/the-current-phase-is-on-every-reception-pass.

    Operator, verbatim: "one seat working on v4 while another was working on
    v3. No seat should work on a v4 until v3 is declared complete." The hub
    cannot know what a message "works on", so it never blocks — it makes the
    phase impossible to miss and rings on writes to the paths it registers.
    Live shape (E6): phase:manuscript, steward=reader, paths=["manuscript.md"]."""
    fleet = team
    fleet.store_set("reader", ROOM, "phase:export",
                    {"current": "export", "status": "open", "next": "verify",
                     "steward": "reader", "paths": [ARTIFACT]})

    for seat in ("writer", "checker", "bystander", "laurent"):
        phases = fleet.owed(seat)["phases"]
        require([p["current"] for p in phases] == ["export"],
                "PHASE/the-current-phase-is-on-every-reception-pass",
                f"{seat}'s reception pass does not carry the open phase "
                f"({phases}) — a phase order nobody reads is the phase order "
                f"that failed", fleet.arc)
        require(phases[0]["next"] == "verify" and phases[0]["steward"] == "reader",
                "PHASE/the-phase-row-says-what-comes-next-and-who-declares-it",
                f"{seat} cannot tell what the next phase is or who may open "
                f"it: {phases[0]}", fleet.arc)

    fleet.fs_write("checker", ROOM, ARTIFACT, "# summary\nnumbers\n",
                   description="the export summary")
    advisories = [e for e in fleet.notify("checker")
                  if "advisory" in (e.get("preview") or "")]
    require(advisories, "PHASE/a-registered-path-rings-the-advisory",
            "a write to a path the OPEN phase registers rang nothing at the "
            "writer — phase disorder becomes invisible again", fleet.arc)
    steward_ring = [e for e in fleet.notify("reader")
                    if "advisory" in (e.get("preview") or "")]
    require(steward_ring, "PHASE/the-steward-hears-writes-on-its-track",
            "the steward was not told that another seat wrote its registered "
            "path — the failure is invisible to each seat alone", fleet.arc)
    require("nothing was blocked" in advisories[0]["preview"],
            "PHASE/the-advisory-is-advisory",
            f"the advisory does not say the write succeeded: "
            f"{advisories[0]['preview'][:120]}", fleet.arc)
    require(not any(qualifies(e, "checker", important_only=True)
                    for e in advisories),
            "PHASE/an-advisory-never-spawns-a-turn",
            "the phase advisory would wake the writer — teaching feedback that "
            "costs a driven turn is a tax on every write", fleet.arc)

    # And a seat that is not the steward cannot declare the transition.
    r = fleet.client.put(f"/channels/{ROOM}/store/{_quote('phase:export')}",
                         json={"value": {"current": "verify", "status": "open",
                                         "steward": "checker"}},
                         headers=fleet.headers["checker"])
    require(r.status_code == 403, "PHASE/only-the-steward-declares-the-transition",
            f"a non-steward rewrote the phase order ({r.status_code}) — any "
            f"seat could freeze the room", fleet.arc)


# ===========================================================================
# VOTES — the window binds and the HUB publishes
# ===========================================================================

def _open_vote(fleet: Fleet, chair: str, topic: str, options: list[str],
               closes_in: float) -> dict[str, Any]:
    post = build_vote_post(chair, topic, options)
    assert post is not None
    data = dict(post["data"])
    data[VOTE_DATA_KEY] = {**data[VOTE_DATA_KEY],
                           "closes_at": time.time() + closes_in}
    return fleet.post(chair, ROOM, status="open", title=post["title"],
                      body=post["body"], data=data,
                      note=f"blind vote (closes in {closes_in:.0f}s)")


def test_votes_the_window_binds_and_the_hub_publishes_the_tally(team: Fleet):
    """VOTES/the-hub-publishes-what-the-chair-promised.

    A blind vote is a coordination primitive only if the result lands. Ballots
    live in the chair's DMs, so a chair whose process died at the deadline used
    to strand the room forever. The hub is the guarantee: the window binds
    while blindness protects something, and the tally is published to the
    ORIGIN channel either way."""
    fleet = team
    vote = _open_vote(fleet, "reader", "storage engine", ["sqlite", "postgres"],
                      closes_in=600.0)
    tag = vote["data"][VOTE_DATA_KEY]["tag"]

    # Two of four eligible members ballot, in two of the rendered spellings.
    fleet.dm("writer", "reader", body=f"vote {tag}: 2")
    fleet.dm("checker", "reader", body=f"vote {tag}: sqlite")
    require(fleet.service.vote_sweep() == [],
            "VOTES/the-announced-window-binds",
            "the hub published before the deadline with voters outstanding — "
            "the announced window is a promise to the voters", fleet.arc)

    # Everyone eligible votes: blindness now protects nothing, so it closes.
    fleet.dm("bystander", "reader", body=f"vote {tag}: postgres > sqlite")
    fleet.dm("laurent", "reader", body=f"vote {tag}: 1")
    require(fleet.service.vote_sweep(),
            "VOTES/the-hub-publishes-what-the-chair-promised",
            "every eligible member voted and the hub published nothing — the "
            "room waits forever on a chair that may not exist", fleet.arc)

    root = fleet.service.db.get_message(vote["id"])
    result = published_result(root, fleet.service.db.replies_to(vote["id"]))
    require(result is not None and result.sender == "hub",
            "VOTES/the-hub-publishes-to-the-origin-channel",
            "the tally is not in the channel the vote was called in", fleet.arc)
    ballots = result.data[VOTE_RESULT_KEY]["ballots"]
    require(ballots == {"writer": [1], "checker": [0], "bystander": [1, 0],
                        "laurent": [0]},
            "VOTES/every-rendered-ballot-spelling-counts",
            f"ballots written in the spellings the hub itself printed were "
            f"miscounted: {ballots}", fleet.arc)


def test_votes_a_deadline_publishes_with_no_chair_process_alive(team: Fleet):
    """VOTES/a-passed-deadline-publishes-without-the-chair."""
    fleet = team
    vote = _open_vote(fleet, "reader", "release date", ["friday", "monday"],
                      closes_in=-1.0)
    tag = vote["data"][VOTE_DATA_KEY]["tag"]
    fleet.dm("writer", "reader", body=f"vote {tag}: friday")
    require(fleet.service.vote_sweep(),
            "VOTES/a-passed-deadline-publishes-without-the-chair",
            "the deadline passed and nothing was published", fleet.arc)
    result = published_result(fleet.service.db.get_message(vote["id"]),
                              fleet.service.db.replies_to(vote["id"]))
    require(result is not None
            and "deadline reached" in result.data[VOTE_RESULT_KEY]["closed"],
            "VOTES/a-passed-deadline-publishes-without-the-chair",
            f"the published result does not name the deadline: {result}",
            fleet.arc)


# ===========================================================================
# CHARTER — told once, cleared on read
# ===========================================================================

def test_charter_a_stale_view_is_stated_once_and_clears_on_read(team: Fleet):
    """CHARTER/a-stale-charter-is-stated-once-and-clears-on-read.

    The standing role model is the shared vocabulary; a long-running seat that
    never learns it changed is collaborating against an old contract. It must
    also never WAKE anyone: a charter is context, not a debt."""
    fleet = team
    scopes = [r["scope"] for r in fleet.owed("writer")["charters"]]
    require(scopes == ["hub", ROOM],
            "CHARTER/a-seat-is-told-what-it-has-not-read",
            f"a fresh seat is behind on both charters and was told {scopes}",
            fleet.arc)
    require(fleet.owed("writer")["to_answer"] == [],
            "CHARTER/a-charter-is-context-not-a-debt",
            "a charter pointer created an answerable obligation — that is a "
            "wake for something nobody asked", fleet.arc)

    fleet.client.get("/charter", headers=fleet.headers["writer"])
    fleet.client.get(f"/channels/{ROOM}/charter", headers=fleet.headers["writer"])
    require(fleet.owed("writer")["charters"] == [],
            "CHARTER/the-line-clears-on-read",
            "the charter line survived the read — a permanent line is noise "
            "the seat learns to skip", fleet.arc)

    fleet.client.put("/admin/charter",
                     headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                     json={"text": "# next\nmember, owner, delegate, operator.\n"})
    rows = {r["scope"]: r for r in fleet.owed("writer")["charters"]}
    hub_row = rows.get("hub", {})
    require(hub_row and hub_row["your_receipt"] < hub_row["version"],
            "CHARTER/a-republished-charter-comes-back-with-the-stale-receipt",
            f"a seat that read the previous version was never told the charter "
            f"changed under it: {rows}", fleet.arc)


# ===========================================================================
# THE ARC — the whole job, unattended, over many rounds
# ===========================================================================

class ScriptedSeat:
    """A seat that does exactly what the skill teaches and nothing else.

    It never invents work, never nudges a peer on a hunch, never polls. Its
    entire input is what the hub hands it: `/owed` (debts, phases, waiting_on)
    and its own continuable rows. If the arc completes, the HUB carried it."""

    def __init__(self, fleet: Fleet, agent_id: str, role: str, *,
                 answer_after: int = 0, silent: bool = False) -> None:
        self.fleet = fleet
        self.id = agent_id
        self.role = role
        self.answer_after = answer_after   # rounds this seat sits on its ask
        self.silent = silent               # ...or never answers at all (E4)
        self.first_seen: dict[str, int] = {}
        self.dispatched_at: dict[str, int] = {}
        self.request_id: str | None = None
        self.fan_id: str | None = None
        self.claim_key: str | None = None
        self.chased: set[str] = set()
        self.escalated: set[str] = set()
        self.reassigned: set[str] = set()
        self.pins: dict[str, list[str]] = {}
        self.withdrawn = False
        self.reported = False
        self.saw_phase = False
        self.verified_at_report: list[str] = []
        self.driver = _driver(fleet, agent_id, self._spawn)

    # -- the work lane (E2: this is where a delegate spends its time) --------

    def _spawn(self, prompt: str, session_id: str | None):
        arc = self.fleet.arc
        arc.work_chunks[self.id] = arc.work_chunks.get(self.id, 0) + 1
        arc.note(self.id, "WORK CHUNK", f"#{arc.work_chunks[self.id]}")
        self.work_chunk()
        return f"{self.id}-work", True

    def work_chunk(self) -> None:
        """ONE bounded slice, then a receipt on the row (E6). The delegate's
        chunk is: monitor what is outstanding, advance the artifact, verify it
        against the request, and only then report."""
        if self.role != "delegate":
            return
        self._monitor()
        if self._advance_artifact():
            return
        self._verify_and_report()

    # -- monitoring: chase, then escalate                     (E4/E5) --------

    #: Rounds an ask may stay outstanding before the delegate chases it, and
    #: before it stops waiting and re-routes. A round is one driver loop pass;
    #: live those passes are minutes apart across arcs of hours (E1), so these
    #: are the compressed stand-in for "long enough that a human would ask".
    CHASE_AFTER, GIVE_UP_AFTER = 2, 4

    def _monitor(self) -> None:
        """Read the hub's own answer to "who has not delivered" and act on it.
        No polling of peers, no guessing: `waiting_on` is the surface (E4)."""
        f = self.fleet
        for row in f.owed(self.id)["waiting_on"]:
            seat, ask, state = row["seat"], row["ask"], row["state"]
            key = f"{seat}/{ask}"
            waited = f.arc.round - self.dispatched_at.setdefault(key, f.arc.round)
            if waited < self.CHASE_AFTER or key in self.escalated:
                continue
            if key not in self.chased:
                self.chased.add(key)
                chase = f.post(self.id, ROOM, status="open", to=[seat],
                               title=f"chasing {ask}",
                               body=f"{seat}: ask {ask} is still open "
                                    f"({state}); it blocks {self.claim_key}. "
                                    f"Answer it or say you cannot.",
                               asks=[{"id": "1",
                                      "text": f"answer {ask} or say you cannot",
                                      "to": [seat]}],
                               note=f"CHASE {seat} (hub said {state})")
                # A chase is itself an obligation this seat created. Track it:
                # giving up on the seat must retire every thread it pinned,
                # or the abandoned seat escalates forever on withdrawn work.
                self.pins.setdefault(seat, []).append(chase["id"])
                # Record the WAIT on the claim row. A chunk that leaves no
                # receipt is a strike, and three of them retire the row — so a
                # delegate that spends its chunks chasing would lose the very
                # chain that lets it keep chasing. "Waiting, by design" is a
                # receipt (live: claim:msg-445 reached v26 this way).
                self._touch_claim(status="in_progress",
                                  note=f"waiting on {seat}/{ask} ({state})")
                f.arc.used("monitor-chase")
                continue
            if waited >= self.GIVE_UP_AFTER:
                # Chased and still nothing. Do BOTH live responses: tell the
                # human (E5) and route the work around the dark seat (E3).
                self.escalated.add(key)
                f.dm(self.id, "laurent",
                     body=f"Operator action needed: {seat} has not answered "
                          f"ask {ask} after a chase ({state}). Re-routing it "
                          f"so {self.claim_key} can still finish.",
                     note="ESCALATE to the operator")
                f.arc.used("monitor-escalate")
                self._reassign(ask, seat)
                self._touch_claim(status="in_progress",
                                  note=f"re-routed {ask} away from {seat}")

    def _reassign(self, ask: str, silent_seat: str) -> None:
        f = self.fleet
        stand_in = next(s for s in ("bystander", "writer", "checker")
                        if s not in (silent_seat, self.id))
        new_ask = f"{ask}r"
        msg = f.post(self.id, ROOM, status="open", title=f"re-assigning {ask}",
                     body=f"{silent_seat} is dark on {ask}; {stand_in}, please "
                          f"take it for {self.claim_key}.",
                     asks=[{"id": new_ask, "text": f"take over {ask}",
                            "to": [stand_in]}],
                     note=f"RE-DISPATCH {ask} -> {stand_in}")
        self.dispatched_at[f"{stand_in}/{new_ask}"] = f.arc.round
        self.reassigned.add(ask)
        f.arc.used("monitor-route-around")
        # Withdraw everything this seat pinned on the dark colleague — the
        # decomposition ask AND every chase — instead of leaving obligations
        # rotting on a seat that will never answer. The asker's own `resolved`
        # closes its own thread (ADR-0003), so this needs no authority beyond
        # having asked.
        if not self.withdrawn:
            self.withdrawn = True
            for pinned in [self.fan_id, *self.pins.get(silent_seat, [])]:
                f.post(self.id, ROOM, status="resolved", reply_to=pinned,
                       body=f"{ask} withdrawn from {silent_seat} and re-routed "
                            f"(see #{msg['seq']}); {self.claim_key} continues.",
                       note="WITHDRAW an obligation pinned on the dark seat")
            f.arc.used("monitor-withdraw")

    # -- the artifact: one slice per chunk, verified before reporting --------

    def _advance_artifact(self) -> bool:
        """Integrate ONE delivered answer into the artifact. True when a slice
        was written (this chunk is spent), False when nothing is integrable —
        either the artifact is whole, or the answer it needs has not landed.

        A requirement whose ask is still outstanding is NOT the delegate's to
        invent: it waits, and monitoring is what turns that wait into an act."""
        f = self.fleet
        head = f.fs_read(self.id, ROOM, ARTIFACT)
        content = head["content"] if head else ""
        version = head["version"] if head else 0
        answered = self._answered_asks()
        for requirement in REQUIREMENTS:
            if requirement in content:
                continue
            if not (set(REQUIREMENT_ASKS[requirement]) & answered):
                continue                      # still owed by a peer
            body = ((content or "# export summary\n")
                    + f"\n{requirement}\nintegrated by {self.id} in round "
                      f"{f.arc.round}\n")
            f.fs_write(self.id, ROOM, ARTIFACT, body, expect_version=version,
                       description="the deliverable the operator asked for")
            self._touch_claim(status="in_progress",
                              note=f"integrated {requirement} "
                                   f"(round {f.arc.round})")
            return True
        return False

    def _answered_asks(self) -> set[str]:
        """Which asks have actually been ANSWERED, per the channel record —
        never per the delegate's memory of having asked."""
        out: set[str] = set()
        for m in self.fleet.history(self.id, ROOM):
            out.update(str(a) for a in ((m.get("data") or {}).get("answers") or []))
        return out

    def _verify_and_report(self) -> None:
        f = self.fleet
        head = f.fs_read(self.id, ROOM, ARTIFACT)
        content = head["content"] if head else ""
        present = [r for r in REQUIREMENTS if r in content]
        if len(present) != len(REQUIREMENTS) or self.reported:
            return
        self.verified_at_report = present
        f.arc.used("verify-artifact-before-reporting")
        # Adversarial gate (2026-08-12): a peer that did NOT write the
        # artifact files its verdict on the record, and the report cites it —
        # the hub refuses an uncontested delivery in a room with peers.
        reviewer = next(s for s in f.headers
                        if s not in (self.id, "laurent"))
        f.store_set(reviewer, ROOM, "review:build",
                    {"verdict": "checked against the operator's request",
                     "reviewer": reviewer})
        f.arc.used("peer-review-before-report")
        # The report CITES the version it verified. Until 2026-08-04 this
        # property was unfalsifiable: `verified_at_report` is set by this
        # very method, so the assertion only proved the test double took its
        # own code path — the hub was never asked to check anything, and a
        # live delegate that skipped the step could not fail the gate. The
        # citation is what the HUB resolves (a dangling ref is refused and
        # discharges nothing), so the property is now enforced, not modelled.
        f.post(self.id, ROOM, status="resolved", reply_to=self.request_id,
               title="delivered",
               body=f"done: {ARTIFACT} v{head['version']} carries "
                    f"{', '.join(present)}; verified against the request; "
                    f"peer-reviewed by {reviewer}. {self.claim_key} closed.",
               data={"evidence": [
                   {"kind": "store", "ref": "plan:build"},
                   {"kind": "fs", "ref": f"{ARTIFACT}@{head['version']}"},
                   {"kind": "store", "ref": "review:build"},
                   {"kind": "store", "ref": self.claim_key}]},
               note="THE REPORT (artifact + plan + peer review cited)")
        self._touch_claim(status="done", done=True,
                          note=f"delivered {ARTIFACT}")
        self.reported = True
        f.arc.used("delegate-report-and-close")

    def _touch_claim(self, **fields: Any) -> None:
        cur = self.fleet.store_get(self.id, ROOM, self.claim_key)
        self.fleet.store_set(self.id, ROOM, self.claim_key,
                             {**cur["value"], **fields},
                             expect_version=cur["version"])

    # -- the reception lane --------------------------------------------------

    def ready(self, owed: dict[str, Any]) -> bool:
        """Scripted latency: live arcs span hours and seats answer late or
        never (E1/E4). Latency gates WHEN a seat takes its turn — never WHICH
        debts it settles once it does: a reception pass settles what you owe
        and then ends, or the next pass is scored as a failed turn (E8)."""
        if self.silent:
            return False
        first = min(self.first_seen.setdefault(r["id"], self.fleet.arc.round)
                    for r in owed["to_answer"])
        return self.fleet.arc.round - first >= self.answer_after

    def _standing(self, row: dict[str, Any]) -> bool:
        """A to_answer row that is SUPPOSED to survive turns: the operator
        commission this seat has already claimed and acked (2026-08-04 —
        the ledger keeps it until the completion report, by design). It is
        served by the WORK lane, never by another reply; answering it again
        every round is the re-ack ceremony the live stall exhibited."""
        return (self.role == "delegate" and self.request_id is not None
                and row.get("id") == self.request_id)

    def reception_turn(self, owed: dict[str, Any]) -> bool:
        """Settle everything owed, then end. Returns True if the seat acted."""
        f = self.fleet
        rows = [r for r in owed["to_answer"] if not self._standing(r)]
        owed = {**owed, "to_answer": rows}
        if not (rows and self.ready(owed)) and not owed["to_consume"]:
            if rows:
                self._ack_only(rows)   # awake, triaged, not ready: ack (E4)
            return False
        f.arc.reception_turns[self.id] = f.arc.reception_turns.get(self.id, 0) + 1
        f.arc.note(self.id, "reception turn",
                   f"answer={len(rows)} consume={len(owed['to_consume'])} "
                   f"phases={[p['current'] for p in owed['phases']]}")
        if owed["phases"]:
            self.saw_phase = True
        before = self.driver._reception_debt()
        for row in rows:
            self._answer(row, owed)
        if owed["to_consume"]:
            self._consume(owed["to_consume"])
        self._grade(before, rows)
        return True

    def _ack_only(self, rows: list[dict[str, Any]]) -> None:
        """"Ack means seen, not done." A live slow seat wakes, triages and
        moves its cursor without answering — which is precisely the state the
        hub reports to the asker as `acked-past-no-reply` (E4: reviewer's
        cursor 459 was past the ask it never answered)."""
        top = max(r["seq"] for r in rows)
        self.fleet.client.post("/inbox/ack", json={"cursors": {ROOM: top}},
                               headers=self.fleet.headers[self.id])

    def _grade(self, before, rows) -> None:
        """Run the PRODUCTION verdict engine over the turn just taken. A seat
        that settled what it could must never be scored failed (E8)."""
        if before is None or before.empty or not rows:
            return
        d = self.driver
        d._reception_debt_before = before
        d._reception_debt_verification_required = True
        verdict = d._verify_reception_debt(TurnEvidence(ok=True), "wake")
        d._reception_debt_before = None
        d._reception_debt_verification_required = False
        if not verdict.ok:
            self.fleet.arc.mutes.append(f"{self.id}: {verdict.detail}")
            self.fleet.arc.note(self.id, "MUTED", verdict.detail)

    def _answer(self, row: dict[str, Any], owed: dict[str, Any]) -> None:
        f = self.fleet
        f.read_message(self.id, row["channel"], row["id"])
        mine = row.get("asks_naming_you") or []
        if mine:
            phase = owed["phases"][0]["current"] if owed["phases"] else "none"
            f.post(self.id, ROOM, status="reply", reply_to=row["id"],
                   answers=mine,
                   body=f"{self.id}: {', '.join(mine)} done, inside phase "
                        f"{phase}. verify: the numbers check out.",
                   note=f"answers {mine}")
            f.arc.used("per-ask-answer")
            return
        if self.role == "delegate" and row["sender"] == "laurent":
            self._ack_and_decompose(row)
            return
        # A bare addressed open (a chase, most often): answer it with WHERE
        # the work is, not with an acknowledgement — a receipt that carries no
        # pointer is the ceremony this gate prices.
        done = sorted(self._answered_asks() & {"a1", "a2", "a2r", "a3"})
        f.post(self.id, ROOM, status="reply", reply_to=row["id"],
               body=f"{self.id}: {', '.join(done) or 'nothing'} answered; the "
                    f"content is in {ARTIFACT}.",
               note="reply to a bare addressed open")

    def _ack_and_decompose(self, row: dict[str, Any]) -> None:
        """The delegate's opening act, in the live shape (E3/E6): take the
        request END TO END with a claim linked to it, say so in-thread, declare
        the phase, then cut the work into ADDRESSED asks."""
        f = self.fleet
        self.request_id = row["id"]
        self.claim_key = f"claim:{row['channel']}-{row['seq']}"
        f.store_set(self.id, ROOM, "phase:export",
                    {"current": "export", "status": "open", "next": "verify",
                     "steward": self.id, "paths": [ARTIFACT]})
        f.arc.used("phase-declared")
        f.store_set(self.id, ROOM, self.claim_key,
                    {"owner": self.id, "status": "in_progress",
                     "source_message_id": row["id"],
                     "note": "the operator's export request, end to end"})
        f.arc.used("claim-linked-to-the-request")
        f.post(self.id, ROOM, status="reply", reply_to=row["id"],
               body=f"Taking this end to end: {self.claim_key} is open and "
                    f"phase:export is declared (export -> verify). Artifact: "
                    f"{ARTIFACT}. Decomposition follows.",
               note="ACK (owns it, cites the claim)")
        f.arc.used("delegate-ack")
        # The plan is a mandatory step (2026-08-12): the completion report
        # must cite the plan row the work was built under.
        f.store_set(self.id, ROOM, "plan:build",
                    {"slices": {"writer": "export scope",
                                "checker": "verify numbers",
                                "bystander": "changelog"},
                     "settled": "decomposed by the delegate, one part each"})
        fan = f.post(self.id, ROOM, status="open", reply_to=row["id"],
                     title="export: decomposition",
                     body=f"Three parts, one each, against {self.claim_key}.",
                     asks=[{"id": "a1", "text": "confirm the export scope",
                            "to": ["writer"]},
                           {"id": "a2", "text": "verify the numbers",
                            "to": ["checker"]},
                           {"id": "a3", "text": "supply the changelog",
                            "to": ["bystander"]}],
                     note="DECOMPOSITION (addressed asks)")
        self.fan_id = fan["id"]
        for seat, ask in (("writer", "a1"), ("checker", "a2"),
                          ("bystander", "a3")):
            self.dispatched_at[f"{seat}/{ask}"] = f.arc.round
        f.arc.used("addressed-fanout")

    def _consume(self, rows: list[dict[str, Any]]) -> None:
        f = self.fleet
        refs = [r["answer_id"] for r in rows]
        r = f.client.post(
            f"/channels/{ROOM}/messages",
            json={"status": "fyi", "consumes": refs,
                  "body": f"folded {len(refs)} answer(s) into "
                          f"{self.claim_key}; writing {ARTIFACT}."},
            headers=f.headers[self.id])
        if r.status_code == 200:
            f.arc.note(self.id, "post fyi",
                       f"#{ROOM}#{r.json()['seq']} ONE batch receipt for "
                       f"{len(refs)} [consumes]")
            if len(refs) > 1:
                f.arc.used("consumes-batch")
            return
        # The pre-0140 fallback: one ceremonial receipt per debt. Never fails
        # the arc by itself — the ceremony counter is what prices it.
        for ref in refs:
            f.post(self.id, ROOM, status="fyi", body="adopted and consumed",
                   consumes=[ref], note="per-thread ceremony receipt")


def _run_arc(fleet: Fleet, seats: list[ScriptedSeat], *,
             max_rounds: int = 40) -> int:
    """The fleet loop, modelled on `agora drive`: settle what you owe, else
    advance what you hold, else idle. Live arcs run for hours across many
    rounds (E1), so the horizon is deliberately long — completion, not
    latency, is the metric.

    A round in which NOBODY moved and the request is not delivered is a
    stall, and a stall is what a human nudge is for: the harness posts one,
    exactly as the operator would have, and the arc records it. Note what
    this catches that "is anyone still owed something" would not: a delegate
    whose work chain has retired its own claim (three chunks that left no
    receipt) sits still while a debt it can no longer act on stays open —
    the failure looks like patience and is actually death."""
    for rnd in range(1, max_rounds + 1):
        fleet.arc.round = rnd
        acted = False
        for seat in seats:
            owed = fleet.owed(seat.id)
            if seat.reception_turn(owed):
                acted = True
                continue
            if chain_step(seat.driver):        # the REAL continuation gate
                acted = True
        if _arc_complete(fleet, seats):
            return rnd
        if not acted:
            fleet.arc.nudge(f"round {rnd}: nobody in the fleet had a turn and "
                            f"the request is undelivered")
            fleet.post("laurent", ROOM, status="open", to=["reader"],
                       body="status?", note="THE NUDGE THIS GATE EXISTS TO KILL")
    return max_rounds


def _arc_complete(fleet: Fleet, seats: list[ScriptedSeat]) -> bool:
    delegate = seats[0]
    if delegate.request_id is None:
        return False
    if not fleet.discharge(delegate.request_id).closed:
        return False
    head = fleet.fs_read(delegate.id, ROOM, ARTIFACT)
    return bool(head) and all(r in head["content"] for r in REQUIREMENTS)


def _ceremony(fleet: Fleet) -> list[dict[str, Any]]:
    """Messages carrying no new information: they discharge nothing, declare
    nothing, and point at no artifact or row that exists. This is the metric
    the field measured at 26% of 253 messages.

    The HUMAN's own brief is never ceremony — it is the information the whole
    arc exists to act on, and it cannot cite artifacts that do not exist yet.
    Ceremony is what the FLEET adds on top."""
    refs = {x["path"] for x in fleet.fs_list("reader", ROOM)}
    refs |= {row["key"] for row in
             fleet.client.get(f"/channels/{ROOM}/store",
                              headers=fleet.headers["reader"]).json()}
    operators = fleet.service.operator_ids()
    empty = []
    for m in fleet.history("reader", ROOM):
        if m["kind"] != "message" or m["sender"] in operators:
            continue
        data = m.get("data") or {}
        if any(data.get(k) for k in ("asks", "answers", "consumes", "vote",
                                     "vote_result")):
            continue
        if any(ref in (m.get("body") or "") for ref in refs):
            continue
        empty.append(m)
    return empty


def _request(fleet: Fleet) -> dict[str, Any]:
    msg = fleet.post("laurent", ROOM, status="open", to=[],
                     title="export summary",
                     body="please produce the verified export summary "
                          "(export section, verification, changelog) and "
                          "report back when it is done",
                     note="THE OPERATOR'S ONE MESSAGE")
    fleet.arc.used("operator-request")
    return msg


def _assert_delivered(fleet: Fleet, seats: list[ScriptedSeat], rounds: int,
                      request: dict[str, Any]) -> None:
    delegate = seats[0]
    head = fleet.fs_read("reader", ROOM, ARTIFACT)
    require(_arc_complete(fleet, seats),
            "THE ARC/an-operator-request-is-delivered",
            f"the arc never completed in {rounds} rounds: request closed="
            f"{fleet.discharge(request['id']).closed}, artifact="
            f"{(head or {}).get('content', '<missing>')!r}", fleet.arc)
    require(not fleet.arc.nudges, "THE ARC/zero-human-nudges",
            f"the fleet stalled and needed {len(fleet.arc.nudges)} human "
            f"nudge(s): {fleet.arc.nudges} — an arc that only moves when a "
            f"human pushes is not collaboration", fleet.arc)
    require(not fleet.arc.mutes, "THE ARC/nobody-is-muted-for-behaving-correctly",
            f"the driver scored a correct reception turn as failed: "
            f"{fleet.arc.mutes} — that is the at-test#446 mass-mute, and a "
            f"muted seat stops collaborating", fleet.arc)

    # E2: a delegate that only talks is a failed delegate.
    work = fleet.arc.work_chunks.get("reader", 0)
    talk = fleet.arc.reception_turns.get("reader", 0)
    require(work >= 3, "THE ARC/the-delegate-does-its-own-work",
            f"the delegate took only {work} work chunks — live it took 18 "
            f"work turns / 3376s against 8 reception turns / 300s; an "
            f"orchestrator that never works is a router", fleet.arc)
    require(work >= talk, "THE ARC/the-delegate-does-more-than-it-says",
            f"the delegate spent {talk} reception turns and only {work} work "
            f"chunks; live the ratio ran 18:8 the other way, and a delegate "
            f"that mostly answers messages is one the arc waits on",
            fleet.arc)

    # The delegate verified the artifact against the request BEFORE reporting.
    require(delegate.verified_at_report == list(REQUIREMENTS),
            "THE ARC/the-report-is-verified-not-asserted",
            f"the delegate reported completion having proven "
            f"{delegate.verified_at_report} of {list(REQUIREMENTS)} — a "
            f"converged plan is not a delivered artifact", fleet.arc)
    claim = fleet.store_get("reader", ROOM, delegate.claim_key)
    require(claim and claim["value"].get("source_message_id") == request["id"],
            "THE ARC/the-claim-names-the-request-it-serves",
            f"the delegate's claim row does not link back to the operator's "
            f"message: {claim}", fleet.arc)
    if claim["version"] >= 3:
        fleet.arc.used("claim-receipts")
    if (fleet.store_get("reader", ROOM, "phase:export") or {}).get("value"):
        fleet.arc.used("phase-observed")
    if head and head["version"] >= 3:
        fleet.arc.used("fs-artifact")
    if work >= 3:
        fleet.arc.used("work-continuation")


def test_the_whole_arc_completes_unattended(team: Fleet):
    """THE ARC/an-operator-request-is-delivered-with-zero-human-nudges.

    One human message in. The delegate acks and takes it end to end on a claim
    linked to the request, declares the phase, decomposes into ADDRESSED asks,
    monitors what is outstanding and chases the slow seat with no human
    involved, does its own repeated work chunks between rounds, verifies the
    artifact against the request, then reports and closes. Out: a file on the
    channel filesystem and a closed thread.

    This is the test that would have caught every whole-arc regression we have
    paid for in live fleet hours."""
    fleet = team
    seats = [ScriptedSeat(fleet, "reader", "delegate"),
             ScriptedSeat(fleet, "writer", "member", answer_after=0),
             # checker sits on its ask for three rounds: live arcs are hours
             # long (E1) and the delegate must MONITOR, not assume.
             ScriptedSeat(fleet, "checker", "member", answer_after=3),
             ScriptedSeat(fleet, "bystander", "member")]

    request = _request(fleet)
    rounds = _run_arc(fleet, seats)
    _assert_delivered(fleet, seats, rounds, request)

    require("monitor-chase" in fleet.arc.capabilities,
            "THE ARC/the-delegate-monitors-what-it-dispatched",
            "no seat was ever chased even though one sat on its ask for "
            "rounds — the delegate is dispatching and forgetting", fleet.arc)
    require(rounds >= 4, "THE ARC/the-arc-is-a-real-multi-round-job",
            f"the whole arc collapsed into {rounds} rounds — the scripted "
            f"latencies are not being exercised, so this proves nothing about "
            f"the hours-long arcs the fleet actually runs", fleet.arc)

    ceremony = _ceremony(fleet)
    messages = [m for m in fleet.history("reader", ROOM)
                if m["kind"] == "message"]
    require(len(ceremony) == 0, "THE ARC/no-ceremony",
            f"{len(ceremony)} of {len(messages)} messages carried no "
            f"information: {[m['body'][:48] for m in ceremony]}", fleet.arc)

    expected = {"operator-request", "delegate-ack", "claim-linked-to-the-request",
                "addressed-fanout", "per-ask-answer", "consumes-batch",
                "phase-declared", "monitor-chase", "claim-receipts",
                "fs-artifact", "work-continuation",
                "verify-artifact-before-reporting", "delegate-report-and-close"}
    missing = expected - fleet.arc.capabilities
    require(not missing, "THE ARC/every-promised-capability-is-used",
            f"the arc completed WITHOUT using: {sorted(missing)} — the hub "
            f"promises these and the collaboration did not need (or could not "
            f"reach) them", fleet.arc)

    require(fleet.owed("bystander")["to_answer"] == [],
            "THE ARC/the-bystander-is-never-pinned",
            "an uninvolved seat finished the arc owing something", fleet.arc)
    for seat in ("reader", "writer", "checker"):
        require(fleet.owed(seat)["to_answer"] == [],
                "THE ARC/every-debt-is-settled-at-the-end",
                f"{seat} still owes {fleet.owed(seat)['to_answer']}", fleet.arc)
    require(seats[1].saw_phase and seats[2].saw_phase,
            "THE ARC/the-phase-order-reached-the-working-seats",
            "a working seat took its turn without the current phase in hand",
            fleet.arc)
    print(fleet.arc.render())          # the green transcript (pytest -s)


def test_the_arc_survives_a_seat_that_goes_dark(team: Fleet):
    """THE ARC/a-silent-seat-does-not-hang-the-arc.

    45% of named seats never reply (E4) — this is the median arc, not the
    corner. The delegate must NOTICE (the hub tells it: `waiting_on` says
    acked-past-no-reply) and then do one of the two things the live delegate
    does: route the work around the dark seat (E3, msg-445#22 -> #29) or
    escalate to the human (E5, 17 DMs on 08-01). Either is a pass; hanging
    forever is not, and neither is silently dropping the requirement."""
    fleet = team
    seats = [ScriptedSeat(fleet, "reader", "delegate"),
             ScriptedSeat(fleet, "writer", "member", answer_after=0),
             ScriptedSeat(fleet, "checker", "member", silent=True),   # DARK
             ScriptedSeat(fleet, "bystander", "member")]

    request = _request(fleet)
    rounds = _run_arc(fleet, seats)
    delegate = seats[0]

    require(rounds < 40, "THE ARC/a-silent-seat-does-not-hang-the-arc",
            "the arc never terminated: one dark seat froze the whole delivery "
            "— which is 45% of live arcs", fleet.arc)
    _assert_delivered(fleet, seats, rounds, request)

    resolution = sorted({"routed-around" for _ in delegate.reassigned}
                        | {"escalated-to-operator" for _ in delegate.escalated})
    require(resolution, "THE ARC/a-silent-seat-is-noticed",
            "the arc finished without the delegate ever noticing the dark "
            "seat — it got lucky, it did not monitor", fleet.arc)
    require("monitor-chase" in fleet.arc.capabilities,
            "THE ARC/a-silent-seat-is-chased-before-it-is-abandoned",
            "the delegate abandoned or escalated a seat it never chased",
            fleet.arc)
    fleet.arc.note("reader", "RESOLUTION", "+".join(resolution))

    # The requirement the dark seat owned was DELIVERED, not dropped.
    head = fleet.fs_read("reader", ROOM, ARTIFACT)
    require("## verified" in head["content"],
            "THE ARC/a-dark-seat-does-not-silently-drop-a-requirement",
            f"the artifact shipped without the part the dark seat owned: "
            f"{head['content']!r}", fleet.arc)
    # ...and the human heard about it exactly once per silent ask, by DM.
    dms = [m for m in fleet.history("reader", "dm:laurent--reader")
           if m["sender"] == "reader"]
    require(len(dms) == len(delegate.escalated),
            "THE ARC/the-human-is-told-once-not-spammed",
            f"the delegate sent {len(dms)} escalation DMs for "
            f"{len(delegate.escalated)} silent ask(s)", fleet.arc)
    # ...and the dark seat is not left carrying a debt for work that moved.
    require(fleet.owed("checker")["to_answer"] == [],
            "THE ARC/a-re-routed-ask-is-withdrawn-not-left-rotting",
            f"the dark seat still owes {fleet.owed('checker')['to_answer']} "
            f"for work that was re-assigned — it will escalate forever",
            fleet.arc)
    print(fleet.arc.render())          # the green transcript (pytest -s)
