"""THE ACCEPTANCE GATE: a whole delegated arc, driven, with nobody watching.

Everything else in the drive suite pins one mechanism. This pins the JOB: an
operator hands one request to a delegate, and the fleet must carry it to a
delivered artifact — decompose, dispatch, answer, chase, verify, report —
without a single further human post.

Real parts: the hub (in-process FastAPI + its own database), the drivers, the
loop, the verdict path, the /owed ledger, the store, per-ask addressing.
Scripted part: the harness. Each "turn" is a deterministic Python function
that does what a well-behaved seat does, so the test measures the ENGINE and
never the model. No tokens, no network, ~2 seconds.

The shape is the live 2026-08-03 arc that stalled (at-test#445): operator ->
delegate -> four addressed asks across five seats -> answers -> three work
chunks -> delivery. That arc needed two operator nudges to finish. This one
gets zero, and the assertions at the bottom say exactly that.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import agora.drive as drive_mod
from agora.drive import Driver

ADMIN = "arc-admin-key"
WORKERS = ("at1", "at2", "at3", "at4", "at5")
CHANNEL = "arc"


# -- the hub, and the client the drivers really talk to -------------------------

@pytest.fixture()
def hub(tmp_path, monkeypatch):
    """An isolated hub plus an httpx.get shim so the driver's own hub reads
    (/owed, the store walk, the by-seq ask lookup) hit it for real."""
    from agora.hub.app import create_app

    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    client = TestClient(create_app(db_path=str(tmp_path / "arc.db"),
                                   admin_key=ADMIN, rate_per_minute=100000.0))
    keys: dict[str, str] = {}
    for seat in ("operator", "delegate", *WORKERS):
        got = client.post("/agents", json={"id": seat, "name": seat.title()},
                          headers={"Authorization": f"Bearer {ADMIN}"})
        assert got.status_code == 200, got.text
        keys[seat] = got.json()["api_key"]

    def headers(seat: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {keys[seat]}"}

    client.post("/channels", json={"name": CHANNEL}, headers=headers("operator"))
    for seat in ("delegate", *WORKERS):
        token = client.post(f"/channels/{CHANNEL}/invites",
                            json={"agent_id": seat},
                            headers=headers("operator")).json()["invite_token"]
        client.post(f"/channels/{CHANNEL}/join", json={"invite_token": token},
                    headers=headers(seat))

    # The drivers use `httpx.get` with a bearer; route those at the test hub.
    by_key = {v: k for k, v in keys.items()}
    import httpx

    def get(url, **kw):
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        token = str(kw.get("headers", {}).get("Authorization", "")).split()[-1]
        assert token in by_key, f"unknown bearer for {path}"
        return client.get(path, headers={"Authorization": f"Bearer {token}"})

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(drive_mod._config, "get_cached_key",
                        lambda hub_url, agent_id: keys[agent_id])

    client.keys = keys                                    # type: ignore[attr-defined]
    client.headers_for = headers                          # type: ignore[attr-defined]
    return client


# -- the scripted harness ------------------------------------------------------

class Seat:
    """One seat's fake harness: a reception pass and a work chunk.

    Deliberately literal about the protocol — it answers the asks addressed to
    it and nothing else, which is precisely the behaviour the driver used to
    mark FAILED when four asks were fanned to four seats.
    """

    def __init__(self, hub: TestClient, seat: str) -> None:
        self.hub, self.seat = hub, seat
        self.h = hub.headers_for(seat)
        self.turns: list[str] = []

    # -- hub helpers --
    def owed(self) -> dict:
        return self.hub.get("/owed", headers=self.h).json()

    def post(self, **payload) -> dict:
        got = self.hub.post(f"/channels/{CHANNEL}/messages", json=payload,
                            headers=self.h)
        assert got.status_code == 200, got.text
        return got.json()

    def message(self, seq: int) -> dict:
        return self.hub.get(f"/channels/{CHANNEL}/messages/by-seq/{seq}",
                            headers=self.h).json()

    def store_set(self, key: str, value: dict) -> None:
        got = self.hub.put(f"/channels/{CHANNEL}/store/{key}",
                           json={"value": value}, headers=self.h)
        assert got.status_code == 200, got.text

    def store_get(self, key: str) -> dict | None:
        got = self.hub.get(f"/channels/{CHANNEL}/store/{key}", headers=self.h)
        return got.json().get("value") if got.status_code == 200 else None

    # -- the spawn the Driver calls --
    def __call__(self, prompt: str, session_id: str | None):
        kind = "work" if prompt.startswith("AGORA WORK CHUNK") else "reception"
        self.turns.append(kind)
        (self.work_chunk if kind == "work" else self.reception)()
        return f"session-{self.seat}", True

    def reception(self) -> None:
        raise NotImplementedError

    def work_chunk(self) -> None:
        return


class Worker(Seat):
    """A dispatched seat: answers the asks that NAME IT, then stops."""

    def reception(self) -> None:
        for row in self.owed().get("to_answer", []):
            mine = [a for a in row.get("asks_naming_you", [])]
            if not mine:
                continue
            self.post(body=f"{self.seat} answers {','.join(mine)}",
                      title=f"answer from {self.seat}", status="reply",
                      reply_to=row["id"], to=[row["sender"]],
                      data={"answers": mine})


class Delegate(Seat):
    """The delegate: ack, decompose, DISPATCH, monitor, verify, deliver.

    It owns the request end to end and never asks a human for anything.
    """

    CLAIM = "claim:msg-1"
    ASKS = {"a1": ["at1", "at2"], "a2": ["at3"], "a3": ["at4"], "a4": ["at5"]}

    def __init__(self, hub: TestClient, seat: str) -> None:
        super().__init__(hub, seat)
        self.fanout_seq: int | None = None
        self.chunks = 0
        self.delivered = False

    def reception(self) -> None:
        for row in self.owed().get("to_answer", []):
            if self.fanout_seq is None:
                self.dispatch(row)
            else:
                # Anything else that names the delegate mid-arc is answered
                # from the claim row, never left to rot.
                self.post(body="status: on it, see the claim row",
                          title="status", status="reply", reply_to=row["id"],
                          to=[row["sender"]])

    def dispatch(self, row: dict) -> None:
        """One message, four asks, each addressed to the seat that owns it."""
        self.store_set(self.CLAIM, {
            "owner": self.seat, "status": "in_progress",
            "source_message_id": row["id"], "next_step": "collect answers",
        })
        fan = self.post(
            body="Decomposed. Each ask names its owner.",
            title="addressed asks", status="open",
            to=list(WORKERS),
            data={"asks": [{"id": aid, "text": f"do {aid}", "to": to}
                           for aid, to in self.ASKS.items()]},
        )
        self.fanout_seq = fan["seq"]
        # ACK to the operator: "yours, I am on it" — which also settles the
        # request row so the hub stops escalating a debt that IS being worked.
        self.post(body="Acknowledged and dispatched to five seats.",
                  title="ack + dispatch", status="reply", reply_to=row["id"],
                  to=[row["sender"]])

    # -- the work engine: a three-slice ladder, no nudges --
    #: Each slice ends at a checkpoint and writes its receipt on the row; the
    #: driver re-wakes for the next one. Assembly cannot start until every
    #: dispatched ask is answered, so the delegate must MONITOR across rounds.
    LADDER = ("outline", "assemble", "verify")

    def work_chunk(self) -> None:
        self.chunks += 1
        claim = self.store_get(self.CLAIM) or {}
        stage = claim.get("stage", "")
        answered = self.answers_in()
        if stage == "":
            claim.update(stage="outline", next_step="assemble once answers land")
        elif stage == "outline":
            if len(answered) < len(self.ASKS):
                # MONITORING is work: chase exactly what is missing, from the
                # row, without posting noise to the room.
                missing = sorted(set(self.ASKS) - answered)
                claim.update(next_step=f"waiting on {','.join(missing)}")
            else:
                claim.update(stage="assemble", next_step="verify the artifact")
        elif stage == "assemble":
            claim.update(stage="verify", next_step="report to the operator")
        elif not self.delivered:
            self.delivered = True
            self.post(body="Artifact assembled from all five answers.",
                      title="delivery", status="resolved",
                      reply_to=self.message(self.fanout_seq)["id"])
            claim.update(status="done", next_step="delivered")
        self.store_set(self.CLAIM, claim)

    def answers_in(self) -> set[str]:
        """Which asks have been answered — read from the hub, not memory."""
        row = self.message(self.fanout_seq)
        return set(self.ASKS) - {str(a) for a in (row.get("pending_asks") or [])}


# -- the fleet pump ------------------------------------------------------------

#: The longest UNINTERRUPTED quiet stretch this room offers a seat. Measured,
#: not invented: during the 2026-08-03 arc the delegate's gaps between wakes
#: were 908s, 111s, 440s and 771s — a busy room simply never hands a seat the
#: full 1200s idle ceiling, so a driver that asks for the ceiling before it
#: may run a chunk never reaches an idle boundary at all.
ROOM_QUIET = 900.0


class Fleet:
    """Round-robin over real Driver loops with a faithful stub listener.

    The listener's contract is the only thing stubbed, and the stub is CAUSAL:
    it returns `2` when the hub says this seat owes something, `0` (an idle
    boundary — the only place a work chunk can start) when the driver asked to
    wait no longer than the room stays quiet, and otherwise `2` again for the
    addressed nudge that arrives first. That is the live shape: reader's long
    waits were cut short by `hub-alerts` messages addressed to it.

    So the listen WINDOW decides whether this arc finishes, exactly as it does
    in production.
    """

    def __init__(self, hub: TestClient, monkeypatch) -> None:
        self.hub = hub
        self.seats: dict[str, Seat] = {}
        self.drivers: dict[str, Driver] = {}
        self.windows: dict[str, list[float]] = {}
        for seat, cls in [("delegate", Delegate)] + [(w, Worker) for w in WORKERS]:
            harness = cls(hub, seat)
            driver = Driver(seat, "http://arc-hub", spawn=harness,
                            max_wait=1200.0, cwd=hub.workspace)
            driver.verify_reception_debt = True    # the REAL verdict path
            self.seats[seat], self.drivers[seat] = harness, driver
            self.windows[seat] = []
        monkeypatch.setattr(drive_mod, "run_listen", self._listen)

    def _listen(self, **kw):
        seat, window = kw["agent_id"], kw["max_wait"]
        self.windows[seat].append(window)
        self.passes -= 1
        if self.passes <= 0:
            raise SystemExit(0)                    # end this seat's slice
        owed = self.hub.get("/owed", headers=self.hub.headers_for(seat)).json()
        if owed["counts"]["to_answer"]:
            return 2                               # a real obligation
        return 0 if window <= ROOM_QUIET else 2    # boundary, or a nudge

    def pump(self, rounds: int = 12, passes: int = 3) -> None:
        """`rounds` sweeps of the fleet; each seat gets `passes` loop passes."""
        for _ in range(rounds):
            for seat, driver in self.drivers.items():
                self.passes = passes
                with pytest.raises(SystemExit):
                    driver.run(max_turns=None)


def _sent_by(hub: TestClient, seat: str) -> set[str]:
    rows = hub.get(f"/channels/{CHANNEL}/messages",
                   headers=hub.headers_for(seat)).json()
    return {m["id"] for m in rows if m["sender"] == seat}


def test_a_delegated_arc_completes_with_zero_external_nudges(hub, tmp_path,
                                                             monkeypatch,
                                                             capsys):
    hub.workspace = tmp_path                             # type: ignore[attr-defined]
    fleet = Fleet(hub, monkeypatch)

    # ONE human act: the request. Nothing else is posted by a human, ever.
    operator = Seat(hub, "operator")
    request = operator.post(
        body="Rebuild the artifact and report when it is delivered.",
        title="operator request", status="open", to=["delegate"],
    )
    human_posts = _sent_by(hub, "operator")
    assert request["id"] in human_posts
    fleet.pump()

    out = capsys.readouterr().out
    delegate = fleet.seats["delegate"]

    # 1. THE JOB GOT DONE — decomposed, dispatched, answered, delivered.
    assert delegate.fanout_seq is not None, "the delegate never dispatched"
    assert delegate.answers_in() == set(Delegate.ASKS), "asks left unanswered"
    assert delegate.delivered, "no artifact was reported to the operator"
    assert (delegate.store_get(Delegate.CLAIM) or {})["status"] == "done"

    # 2. IT TOOK MULTIPLE WORK CHUNKS, unattended.
    assert delegate.chunks >= 3, f"only {delegate.chunks} work chunks"

    # 3. ZERO EXTERNAL NUDGES: nothing human was posted after the request.
    #    (The live arc this is modelled on needed two: "the artifact has not
    #    been rebuilt" at +36min and "still yours" at +54min.)
    assert _sent_by(hub, "operator") == human_posts

    # 4. NO SEAT WAS EVER MUTED — nothing held, parked, backed off or dropped,
    #    and not one turn was scored a failure.
    assert "state=backoff" not in out
    assert "state=parked" not in out
    assert "wake=held" not in out
    assert "status=error" not in out
    assert "debt-remains" not in out

    # 5. THE FAN-OUT DID NOT PENALISE THE SEATS THAT ANSWERED. Every worker
    #    answered only the asks naming it, leaving the others pending — the
    #    exact shape that failed five of seven seats live on 2026-08-03.
    for worker in WORKERS:
        assert fleet.seats[worker].turns, f"{worker} never ran a turn"
        assert fleet.drivers[worker]._fail_streak == 0
        assert fleet.drivers[worker]._pending_wake is False

    # 6. THE DELEGATE POLLED AT THE CHAIN CADENCE while it held work, instead
    #    of waiting out the 1200s idle ceiling for its first chunk. A chunk
    #    can only start at an idle boundary, so at the ceiling this arc would
    #    have needed 20 quiet minutes per slice — which is exactly why the
    #    live one produced 1 chunk in 62 minutes and needed a human twice.
    windows = fleet.windows["delegate"]
    assert windows[0] == 1200.0                  # no work yet: the ceiling
    assert windows.count(drive_mod.DRIVE_CHAIN_WAIT) >= delegate.chunks
    # ...and the workers, holding nothing, never poll at the chain cadence.
    for worker in WORKERS:
        assert set(fleet.windows[worker]) == {1200.0}


def test_the_arc_transcript_names_every_state(hub, tmp_path, monkeypatch,
                                              capsys):
    """The operator must be able to follow the arc from stdout alone."""
    hub.workspace = tmp_path                             # type: ignore[attr-defined]
    fleet = Fleet(hub, monkeypatch)
    Seat(hub, "operator").post(body="go", title="request", status="open",
                               to=["delegate"])
    fleet.pump(rounds=6)
    lines = [line for line in capsys.readouterr().out.splitlines()
             if line.startswith("AGORA_DRIVE state=")]
    states = {line.split("state=", 1)[1].split()[0] for line in lines}
    assert {"armed", "turn", "chunk"} <= states
    # Every armed/held line carries the next transition time.
    for line in lines:
        if line.startswith("AGORA_DRIVE state=armed"):
            assert "next=" in line and "reason=" in line
