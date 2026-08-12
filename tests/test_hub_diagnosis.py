"""Hub-side latent-defect audit (2026-08-03) + the `agora doctor` surface.

Each test in the first half replays a defect of ONE class: a heuristic that
assumes a simple case, applied to a legitimate complex one, degrading
silently. All four were reproduced on an isolated hub against the shipped
v0.14.0 tree before being fixed here; the live-hub evidence for each is cited
in the test that closes it.

The second half covers `agora doctor` — the single diagnostic that replaces
reconstructing a stalled arc from sqlite + notify logs + driver logs.
"""

import time

from fastapi.testclient import TestClient

from agora.hub.app import create_app
from agora.hub.presence import _ACTIVE_WINDOW, _RECEPTION_STALE

ADMIN_KEY = "test-admin"


def make_client(**kw) -> TestClient:
    return TestClient(create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                                 rate_per_minute=600.0, dark_watch_seconds=0,
                                 **kw))


def register(client: TestClient, agent_id: str, operator: bool = False) -> dict:
    r = client.post("/agents", json={"id": agent_id, "mission": f"seat {agent_id}", "operator": operator},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_channel(client: TestClient, owner: dict, name: str, *members) -> None:
    client.post("/channels", json={"name": name}, headers=owner)
    for member in members:
        tok = client.post(f"/channels/{name}/invites", json={},
                          headers=owner).json()["invite_token"]
        client.post(f"/channels/{name}/join", json={"invite_token": tok},
                    headers=member)


def post(client: TestClient, headers: dict, channel: str = "room", **kw) -> dict:
    return client.post(f"/channels/{channel}/messages", json=kw,
                       headers=headers).json()


def _fleet_observed_then_vanished(service, seats=("a", "b", "c")):
    """2026-08-04 denominator: FLEET DARK measures OBSERVED seats vanishing;
    a cold roster is not a fleet. Arm the seats through the real recording
    path, then age their presence past every liveness window."""
    for s in seats:
        service.presence.mark_reception(s)
    service._fleet_eligible_agents()          # records the live observation
    for s in seats:
        service.presence._last_reception[s] -= (_RECEPTION_STALE + 1300.0)
        if s in service.presence._last_seen:
            service.presence._last_seen[s] -= (_RECEPTION_STALE + 1300.0)


def alert_bodies(client: TestClient, op: dict) -> list[str]:
    rows = client.get("/channels/hub-alerts/messages", headers=op).json()
    return [m["body"] for m in rows] if isinstance(rows, list) else []


def sla_room(client, owner, name, *members, minutes=0.001):
    make_channel(client, owner, name, *members)
    client.put(f"/channels/{name}/store/channel:meta",
               json={"value": {"response_sla_minutes": minutes}}, headers=owner)


# --------------------------------------------------------------------------
# 1. A listening seat is not a dark seat.

def test_armed_reception_is_not_offline_presence():
    """The two liveness clocks are written by ONE request (the /owed arm poll
    touches both), yet `_ACTIVE_WINDOW` (600s) was shorter than
    `_RECEPTION_STALE` (900s) — so for every seat whose work chunk ran longer
    than ten minutes, `presence.get()` said 'offline' while `reception()`
    said 'armed' about the same instant."""
    from agora.hub.presence import PresenceTracker

    t = PresenceTracker()
    t.touch("reader")
    t.mark_reception("reader")
    age = (_ACTIVE_WINDOW + _RECEPTION_STALE) / 2      # squarely in the band
    t._last_seen["reader"] -= age
    t._last_reception["reader"] -= age
    assert t.reception("reader")[0] == "armed"
    assert t.get("reader").state == "active"           # was "offline"


def test_reception_stale_window_outlasts_the_listener_idle_ceiling():
    """THE INVARIANT, pinned. `_RECEPTION_STALE` is the hub's "this listener
    is DEAF" line; `listen.ADAPT_CAP_DEFAULT` is the longest a HEALTHY idle
    listener waits between arms. If the first is not comfortably larger than
    the second, the hub calls its own idle seats dead — and `_fleet_seat_live`
    plus the DARK/DEAF watchdogs are built on that read, so the contradiction
    manufactures alerts instead of reporting them.

    Live evidence (2026-08-03): 900 < 1200 shipped for months. Minutes after
    all eight drivers logged `state=armed next=1200s`, `agora doctor` read
    "3/50 seats live" with six armed seats shown "offline / stale 17m", and
    `fleet.collapsed` was true at live_fraction 0.08 — a permanently dark
    fleet on a fleet that was entirely awake."""
    from agora import listen as listen_mod
    from agora.hub import presence as presence_mod

    assert presence_mod._RECEPTION_STALE > listen_mod.ADAPT_CAP_DEFAULT * 2, (
        f"_RECEPTION_STALE={presence_mod._RECEPTION_STALE} leaves no room for "
        f"a healthy listener idling at {listen_mod.ADAPT_CAP_DEFAULT}s")
    # The other half of the same rule, already fixed once: an armed seat must
    # never read offline (see test_armed_reception_is_not_offline_presence).
    assert presence_mod._ACTIVE_WINDOW <= presence_mod._RECEPTION_STALE


def test_an_idle_seat_at_the_listener_ceiling_still_counts_as_live():
    """The consequence the constant exists for: a seat whose last arm is one
    full idle ceiling old is IDLE, not dark, and must still count toward fleet
    liveness. At 900s this seat was dark and dragged the fleet into a
    permanent FLEET DARK episode."""
    from agora import listen as listen_mod

    client = make_client()
    op = register(client, "op", operator=True)
    for seat in ("a", "b", "c"):
        register(client, seat)
    service = client.app.state.service
    for seat in ("a", "b", "c"):
        service.presence.mark_reception(seat)
        service.presence._last_reception[seat] -= listen_mod.ADAPT_CAP_DEFAULT + 60
    assert all(service._fleet_seat_live(s) for s in ("a", "b", "c"))
    assert service._fleet_liveness_sweep() == []
    assert not any(b.startswith("FLEET DARK") for b in alert_bodies(client, op))


def test_armed_seat_gets_no_dark_alert_but_a_silent_one_still_does():
    """Live hub-alerts, 2026-08-03 00:49:29: 'AGENT DARK: reader is offline
    holding 1 SLA-breached obligation(s)' fired while reader's listener was
    arming. Alerting is right only when BOTH clocks stopped."""
    client = make_client()
    flow, op = register(client, "flow"), register(client, "op", operator=True)
    reader = register(client, "reader")
    sla_room(client, flow, "room", reader)
    post(client, flow, body="for reader", title="q", status="open",
         to=["reader"], asks=[{"id": "1", "text": "a?"}])
    time.sleep(0.2)
    service = client.app.state.service
    band = (_ACTIVE_WINDOW + _RECEPTION_STALE) / 2
    service.presence._last_seen["reader"] = time.time() - band
    service.presence._last_reception["reader"] = time.time() - band
    assert service.dark_sweep() == []
    assert not any(b.startswith("AGENT DARK") for b in alert_bodies(client, op))

    # The watchdog is not blinded: when the listener really stops, it fires.
    service.presence._last_seen.pop("reader", None)
    service.presence._last_reception.pop("reader", None)
    assert service.dark_sweep() == ["reader"]
    assert any("AGENT DARK: reader" in b for b in alert_bodies(client, op))


def test_armed_seat_keeps_its_escalation_rewake(tmp_path):
    """The SILENT half of the same defect: a seat wrongly recorded as dark is
    also excluded from the escalation re-ring (`_escalation_rewake_suppressed`),
    so the hub quietly stops re-delivering escalated notify lines to a seat
    that is listening for them."""
    import json

    client = make_client(notify_dir=str(tmp_path / "notify"))
    flow = register(client, "flow")
    uic = register(client, "uic")
    sla_room(client, flow, "room", uic)
    post(client, flow, body="for uic", title="q", status="open", to=["uic"],
         asks=[{"id": "1", "text": "a?"}])
    client.get("/owed", headers={**uic, "X-Agora-Reception": "arm"})
    time.sleep(0.2)
    service = client.app.state.service
    band = (_ACTIVE_WINDOW + _RECEPTION_STALE) / 2
    service.presence._last_seen["uic"] = time.time() - band
    service.presence._last_reception["uic"] = time.time() - band

    log = tmp_path / "notify" / "uic-inbox.log"

    def escalated_lines() -> int:
        return sum("escalated" in json.loads(line).get("flags", "")
                   for line in log.read_text().strip().split("\n") if line)

    before = escalated_lines()
    service.dark_sweep()
    assert "uic" not in service._dark_since
    assert escalated_lines() == before + 1      # was 0: silently suppressed


# --------------------------------------------------------------------------
# 2. The watchdog must not manufacture the debt it then alarms about.

def _delegate_hub():
    client = make_client()
    flow, op = register(client, "flow"), register(client, "op", operator=True)
    reader, uic = register(client, "reader"), register(client, "uic")
    sla_room(client, flow, "room", uic, reader)
    client.put("/admin/delegation",
               json={"agent_id": "reader", "powers": ["reporting"]},
               headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    ask = post(client, flow, body="for uic", title="q", status="open",
               to=["uic"], asks=[{"id": "1", "text": "a?"}])
    time.sleep(0.2)
    service = client.app.state.service
    service.presence._last_seen.pop("uic", None)
    service.presence._last_reception.pop("uic", None)
    return client, service, op, reader, uic, ask


def test_alert_addressed_to_the_delegate_does_not_alert_about_the_delegate():
    """Every silence alert is ADDRESSED TO the reporting delegate, so it is
    the delegate's own open obligation seconds after the hub writes it. The
    dark-delegate widening then read that row as cause to alert about the
    delegate — live #930 (00:49:25, about `agora`, to=[reader]) followed by
    #931 (00:49:29) claiming reader held an 'SLA-breached obligation, oldest
    ~0 min'. The widening is gone: escalated debt is the only bar."""
    client, service, op, reader, _uic, _ask = _delegate_hub()
    assert service.dark_sweep()[0] == "uic"
    owed = client.get("/owed", headers=reader).json()
    assert owed["counts"]["to_answer"] >= 1      # the hub's own alert
    assert all(not row["escalated"] for row in owed["to_answer"])

    service.presence._last_seen.pop("reader", None)
    service.presence._last_reception.pop("reader", None)
    assert service.dark_sweep() == []
    assert not any("AGENT DARK: reader" in b for b in alert_bodies(client, op))


def test_silence_alert_is_closed_when_the_episode_ends():
    """0093's bounded-debt contract, applied to the silence watchdog: an open
    alert is an OBLIGATION on its addressees. Live count before this fix: 141
    unresolved hub-authored open alerts, 40 of them permanent escalating debt
    on the current reporting delegate."""
    client, service, op, reader, uic, ask = _delegate_hub()
    service.dark_sweep()
    before = client.get("/owed", headers=reader).json()["counts"]["to_answer"]
    assert before >= 1
    assert any("AGENT DARK: uic" in b for b in alert_bodies(client, op))

    # The episode ends: uic answers and comes back.
    post(client, uic, body="a!", status="reply", reply_to=ask["id"],
         answers=["1"])
    service.presence.touch("uic")
    service.presence.mark_reception("uic")
    service.dark_sweep()
    after = client.get("/owed", headers=reader).json()["counts"]["to_answer"]
    assert after < before
    assert any("dark episode for 'uic' ended" in b
               for b in alert_bodies(client, op))


def test_one_standing_silence_alert_per_seat_survives_hub_restarts():
    """The episode closer only closes alerts whose episode ENDED, so a seat
    that stays dark accumulates one duplicate per hub restart: each restart
    clears `_dark_since`, re-opens the episode and mints a fresh alert, while
    the previous one stands forever because its subject now reads as live.

    Measured on the live hub AFTER the episode closer had run: 99 standing
    silence alerts over 28 subjects — 71 pure duplicates, `camera` alone
    holding 9 rows of the same sentence, every one of them an escalating,
    undischargeable obligation on the reporting delegate."""
    client, service, op, reader, _uic, _ask = _delegate_hub()
    for _ in range(4):                       # four hub bounces, uic still dark
        service.dark_sweep()
        service._dark_since.clear()          # what a restart does to memory
        service._mark_alerted("dark", "uic", 0.0)   # ... and what an elapsed
        #                                             DARK_REALERT_SECONDS does
        #                                             to the durable flap guard
    standing = [b for b in alert_bodies(client, op)
                if b.startswith("AGENT DARK: uic")]
    assert len(standing) > 1, "precondition: the duplicates were minted"

    service.dark_sweep()                     # the sweep that must dedupe them
    owed = client.get("/owed", headers=reader).json()["to_answer"]
    alive = [r for r in owed if r["title"].startswith("AGENT DARK: uic")
             or "AGENT DARK: uic" in r.get("body", "")]
    assert len(alive) <= 1, f"delegate still holds {len(alive)} duplicate alerts"
    assert any("superseded by the current dark alert for 'uic'" in b
               for b in alert_bodies(client, op))
    # The seat is still dark: the hub keeps exactly one live row about it.
    assert any(b.startswith("AGENT DARK: uic") for b in alert_bodies(client, op))


def test_standing_fleet_alerts_close_after_a_hub_restart():
    """`_close_standing_fleet_alerts` used to run ONLY on the in-process
    dark -> recovered transition. A hub that bounced while the fleet was
    healthy therefore never closed the previous process's FLEET rows: 34
    FLEET DARK + 20 FLEET RECOVERED standing on the live operator, permanent
    obligations for events that were over."""
    from agora.hub import service as hub_service

    client = make_client()
    op = register(client, "op", operator=True)
    for seat in ("a", "b", "c"):
        register(client, seat)
    service = client.app.state.service
    _fleet_observed_then_vanished(service)
    service._fleet_dark_since = time.time() - hub_service.FLEET_DARK_CONFIRM_SECONDS - 1
    assert service._fleet_liveness_sweep() == ["fleet-dark"]
    owed_dark = client.get("/owed", headers=op).json()["counts"]["to_answer"]
    assert owed_dark >= 1

    # The hub restarts: the episode memory is gone, the alert is not.
    service._fleet_dark_alerted = False
    service._fleet_dark_since = None
    for seat in ("a", "b", "c"):
        service.presence.mark_reception(seat)
    assert service._fleet_liveness_sweep() == []        # no transition to ride
    owed_back = client.get("/owed", headers=op).json()["counts"]["to_answer"]
    assert owed_back < owed_dark, "standing FLEET DARK outlived its episode"
    assert any("fleet liveness recovered" in b for b in alert_bodies(client, op))


def test_standing_fleet_alerts_are_bounded_on_a_permanently_dark_hub():
    """The recovery path can only run when the fleet is HEALTHY, and on a hub
    whose roster has outgrown its drivers it never is: 8 of 50 registered
    seats live is a permanent collapse by the liveness fraction, so every past
    episode's alert stands forever. Live: 35 FLEET DARK + 20 FLEET RECOVERED
    = 55 permanent operator obligations that no recovery would ever reach."""
    from agora.hub import service as hub_service

    client = make_client()
    op = register(client, "op", operator=True)
    for seat in ("a", "b", "c"):
        register(client, seat)
    service = client.app.state.service
    _fleet_observed_then_vanished(service)

    for _ in range(4):          # four dark episodes across four hub lifetimes
        service._fleet_dark_since = (
            time.time() - hub_service.FLEET_DARK_CONFIRM_SECONDS - 1)
        service._fleet_dark_alerted = False
        assert service._fleet_liveness_sweep() == ["fleet-dark"]
    dark, _ = service._standing_fleet_alerts()
    assert len(dark) > 1, "precondition: the duplicates were minted"

    # Fleet STILL collapsed — the recovery path is unreachable, yet the bound
    # on standing debt must hold anyway.
    service._fleet_dark_since = (
        time.time() - hub_service.FLEET_DARK_CONFIRM_SECONDS - 1)
    service._fleet_liveness_sweep()
    dark, recovered = service._standing_fleet_alerts()
    assert len(dark) == 1, f"{len(dark)} standing FLEET DARK rows on a dark hub"
    assert recovered == []
    assert any("superseded by the current FLEET DARK alert" in b
               for b in alert_bodies(client, op))


def test_fleet_recovered_is_news_not_a_new_debt():
    """FLEET RECOVERED was posted as an addressed OPEN — good news minting a
    permanent operator obligation (live: 20 unresolved), while the FLEET DARK
    it recovers from was never closed either (34 more)."""
    from agora.hub import service as hub_service

    client = make_client()
    op = register(client, "op", operator=True)
    for seat in ("a", "b", "c"):
        register(client, seat)
    service = client.app.state.service
    _fleet_observed_then_vanished(service)
    service._fleet_dark_since = time.time() - hub_service.FLEET_DARK_CONFIRM_SECONDS - 1
    assert service._fleet_liveness_sweep() == ["fleet-dark"]
    owed_dark = client.get("/owed", headers=op).json()["counts"]["to_answer"]
    assert owed_dark >= 1

    for seat in ("a", "b", "c"):
        service.presence.mark_reception(seat)
    assert service._fleet_liveness_sweep() == ["fleet-recovered"]
    owed_back = client.get("/owed", headers=op).json()["counts"]["to_answer"]
    assert owed_back < owed_dark
    assert any(b.startswith("FLEET RECOVERED") for b in alert_bodies(client, op))


# --------------------------------------------------------------------------
# 3. Per-ask scoping, applied consistently.

def test_answered_assigned_ask_stops_being_your_debt():
    """A canvass ask ASSIGNED to at1 is answered by a third seat. The
    envelope says `to_me=false` and `asks_naming_you=[]`, but /owed kept the
    row (an unscoped `assignees` term beside the pending-scoped one) — and
    the watchdogs read /owed, so at1 was named in AGENT DARK for a discharged
    ask."""
    client = make_client()
    flow, op = register(client, "flow"), register(client, "op", operator=True)
    at1, at2, at3 = (register(client, "at1"), register(client, "at2"),
                     register(client, "at3"))
    sla_room(client, flow, "room", at1, at2, at3)
    m = post(client, flow, body="canvass", title="c", status="open",
             asks=[{"id": "1", "text": "at1's part", "assignee": "at1"},
                   {"id": "2", "text": "at2's part", "to": ["at2"]}])
    post(client, at3, body="did at1's part", status="reply",
         reply_to=m["id"], answers=["1"])
    time.sleep(0.2)
    service = client.app.state.service

    owed = client.get("/owed", headers=at1).json()
    assert not [r for r in owed["to_answer"] if r["id"] == m["id"]]
    assert service._escalated_debts("at1") == []
    # at2's row is untouched: its ask is still pending.
    owed2 = client.get("/owed", headers=at2).json()
    assert [r for r in owed2["to_answer"] if r["id"] == m["id"]]

    service.presence._last_seen.pop("at1", None)
    service.dark_sweep()
    assert not any("AGENT DARK: at1" in b for b in alert_bodies(client, op))


def test_assignee_of_a_pending_ask_still_owes_it():
    """The scoping cuts one way only: an UNanswered assigned ask is debt."""
    client = make_client()
    flow = register(client, "flow")
    at1 = register(client, "at1")
    make_channel(client, flow, "room", at1)
    m = post(client, flow, body="canvass", title="c", status="open",
             asks=[{"id": "1", "text": "at1's part", "assignee": "at1"}])
    owed = client.get("/owed", headers=at1).json()
    assert [r for r in owed["to_answer"] if r["id"] == m["id"]]


# --------------------------------------------------------------------------
# 4. agora doctor: one screen, and honest about its blind spots.

def _stalled_arc():
    """laurent asks; reader (delegate) claims it, declares a next step, and
    dispatches two asks; one addressee acked past its ask without replying,
    the other was never served."""
    client = make_client()
    laurent = register(client, "laurent", operator=True)
    reader, editor, at1 = (register(client, "reader"), register(client, "editor"),
                           register(client, "at1"))
    make_channel(client, laurent, "at-test", reader, editor, at1)
    client.put("/admin/delegation",
               json={"agent_id": "reader", "powers": ["reporting"]},
               headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    req = post(client, laurent, "at-test", body="regenerate the images",
               title="revision request", status="open")
    client.put(f"/channels/at-test/store/claim:msg-{req['seq']}",
               json={"value": {"owner": "reader", "status": "in_progress",
                               "source_message_id": req["id"],
                               "next_step": "Regenerate, re-run the gate, "
                                            "post the receipt."}},
               headers=reader)
    ask = post(client, reader, "at-test", body="two inputs", title="inputs",
               status="open",
               asks=[{"id": "1", "text": "gate them", "to": ["editor"]},
                     {"id": "2", "text": "confirm path", "to": ["at1"]}])
    client.post("/inbox/ack", json={"cursors": {"at-test": ask["seq"]}},
                headers=editor)
    client.get("/owed", headers={**reader, "X-Agora-Reception": "arm"})
    return client, req, ask


def test_doctor_answers_the_stall_question_in_one_payload():
    client, req, _ask = _stalled_arc()
    doc = client.get("/admin/doctor",
                     headers={"Authorization": f"Bearer {ADMIN_KEY}"}).json()
    seats = {s["agent_id"]: s for s in doc["seats"]}

    reader = seats["reader"]
    assert reader["delegate_powers"] == ["reporting"]
    assert reader["reachable"]["reception"] == "armed"
    # It WORKED (it posted and wrote a claim row), not merely received.
    assert reader["did_work"]["last_work_seconds"] is not None
    assert reader["working_on"][0]["key"].startswith("claim:msg-")
    assert reader["working_on"][0]["next_step"].startswith("Regenerate")
    # A seat that only polls is visibly distinct: at1 never worked.
    assert seats["at1"]["did_work"]["last_work_seconds"] is None

    request = next(r for r in doc["requests"] if r["id"] == req["id"])
    assert request["owned_by"] == ["reader"]
    assert request["claims"][0]["status"] == "in_progress"
    waiting = {a["waiting_on"]: a["state"] for a in request["outstanding_asks"]}
    assert waiting == {"editor": "acked-past-no-reply", "at1": "not-yet-served"}
    assert all(a["age_seconds"] >= 0 for a in request["outstanding_asks"])

    # Hub health and the honesty clause travel with it.
    assert doc["hub"]["sweeps"]["dark"]["last_run_seconds"] is None  # never ran
    assert doc["hub"]["votes_past_deadline"] == 0
    assert any("driver-side" in line for line in doc["hub_cannot_see"])


def test_doctor_shows_what_holds_a_seat_up_and_narrows_to_one_seat():
    client, _req, _ask = _stalled_arc()
    op = register(client, "op", operator=True)
    at5 = register(client, "at5")
    # A refused send is a hold the operator must SEE, not infer from silence.
    assert client.post("/channels/at-test/messages", json={"body": "x"},
                       headers=at5).status_code == 403
    client.post("/hub/blocks",
                json={"agent": "at5", "seconds": 3600, "reason": "operator hold"},
                headers=op)
    doc = client.get("/admin/doctor", params={"agent": "at5"},
                     headers={"Authorization": f"Bearer {ADMIN_KEY}"}).json()
    assert [s["agent_id"] for s in doc["seats"]] == ["at5"]
    held = {h["kind"]: h for h in doc["seats"][0]["held_up_by"]}
    assert "hub-block" in held and held["hub-block"]["seconds_left"] > 0
    assert held["send-refused"]["last_code"] == 403


def test_doctor_shows_silent_addressees_of_an_unstructured_open():
    """Measured on the live hub, 118 of 177 peer multi-addressee opens were
    discharged with named seats still silent, and the asker was told
    nothing. Two layers now answer that: the 2026-08-11 peer-addressed rule
    keeps every named seat's own row live until they engage (another
    addressee's reply never clears YOUR debt), and the doctor names the
    silent addressees for the operator."""
    client = make_client()
    laurent = register(client, "laurent", operator=True)
    reader, editor, at1 = (register(client, "reader"), register(client, "editor"),
                           register(client, "at1"))
    make_channel(client, laurent, "at-test", reader, editor, at1)
    client.put("/admin/delegation",
               json={"agent_id": "reader", "powers": ["reporting"]},
               headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    post(client, laurent, "at-test", body="regenerate", title="req",
         status="open")
    ask = post(client, reader, "at-test", body="both of you please",
               title="canvass", status="open", to=["editor", "at1"])
    post(client, editor, "at-test", body="on it", status="reply",
         reply_to=ask["id"])

    owed = client.get("/owed", headers=at1).json()
    assert [r for r in owed["to_answer"] if r["id"] == ask["id"]], \
        "a silent named seat keeps its own row (2026-08-11 rule)"
    doc = client.get("/admin/doctor",
                     headers={"Authorization": f"Bearer {ADMIN_KEY}"}).json()
    silent = [a for r in doc["requests"] for a in r["outstanding_asks"]
              if a["ask"] == "(unstructured)"]
    assert [a["waiting_on"] for a in silent] == ["at1"]


def test_doctor_requires_the_admin_key():
    client, _req, _ask = _stalled_arc()
    op = register(client, "op", operator=True)
    assert client.get("/admin/doctor", headers=op).status_code == 403


def test_doctor_reports_sweep_runs_and_unclosed_alert_debt():
    client, service, _op, _reader, _uic, _ask = _delegate_hub()
    service.dark_sweep()
    doc = client.get("/admin/doctor",
                     headers={"Authorization": f"Bearer {ADMIN_KEY}"}).json()
    assert doc["hub"]["sweeps"]["dark"]["last_run_seconds"] is not None
    # One live episode -> exactly one standing, still-open silence alert.
    assert doc["hub"]["unclosed_silence_alerts"] == 1
