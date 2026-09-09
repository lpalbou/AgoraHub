"""Version availability resumes an owner once; it does not accept evidence."""
import json
from urllib.parse import quote, urlsplit

import pytest
from fastapi.testclient import TestClient

import agora.drive as drive_mod
from agora.drive import Driver, DRIVE_CHAIN_WAIT
from agora.hub.app import create_app

KEY = "claim:af-health-delivery"
PATHS = ("shared/media.md", "shared/media-findings.json", "shared/mgr-core.md", "shared/mgr-apps.md")


class Room:
    def __init__(self, tmp_path, monkeypatch):
        self.client = TestClient(create_app(db_path=":memory:", admin_key="admin", rate_per_minute=100000))
        self.seats = {}
        for name in ("operator", "director", "media", "mgr-core", "mgr-apps", "peer"):
            r = self.client.post("/agents", json={"id": name, "operator": name == "operator"}, headers={"Authorization": "Bearer admin"})
            assert r.status_code == 200, r.text
            self.seats[name] = {"Authorization": "Bearer " + r.json()["api_key"]}
        for channel in ("task-1", "other"):
            r = self.client.post("/channels", json={"name": channel, "private": False}, headers=self.seats["operator"])
            assert r.status_code == 200, r.text
            for name in self.seats:
                if name != "operator":
                    assert self.client.post(f"/channels/{channel}/join", json={}, headers=self.seats[name]).status_code == 200
        r = self.client.post("/channels/task-1/messages", headers=self.seats["operator"], json={
            "status": "open", "to": ["director"], "body": "Audit full AF readiness and deliver HEALTH with evidence."})
        assert r.status_code == 200, r.text
        self.source = r.json()["id"]
        self.spawned = []
        monkeypatch.setenv("AGORA_HOME", str(tmp_path))
        monkeypatch.setattr("agora.config.get_cached_key", lambda *args: self.seats["director"]["Authorization"].split()[1])
        monkeypatch.setattr("httpx.get", lambda url, **kw: self.client.get(urlsplit(url).path,
                            params=kw.get("params"), headers=kw.get("headers", self.seats["director"])))
        self.home = tmp_path
        self.driver = self.new_driver()

    def new_driver(self):
        return Driver("director", "http://isolated", cwd=self.home, harness="codex", spawn=self.spawn, max_wait=1200)

    def spawn(self, prompt, sid):
        self.spawned.append(prompt)
        return "work-session", True

    def value(self, **changes):
        return {"owner": "director", "status": "blocked — Media and manager artifacts awaited",
                "blocked_on": "seat", "needs_from": "media", "needs": "Media and manager synthesis",
                "source_message_id": self.source,
                "waiting_for_artifacts": [{"channel": "task-1", "path": p, "min_version": 1} for p in PATHS], **changes}

    def put(self, value, version=0, writer="director", key=KEY):
        return self.client.put("/channels/task-1/store/" + quote(key, safe=":"),
                               headers=self.seats[writer], json={"value": value, "expect_version": version})

    def claim(self, value=None, version=0, **kw):
        r = self.put(self.value() if value is None else value, version, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    def file(self, path, version=0, channel="task-1", content="Evidence body", writer="operator"):
        r = self.client.put(f"/channels/{channel}/fs/{path}", headers=self.seats[writer],
                            json={"content": content, "expect_version": version})
        assert r.status_code == 200, r.text
        return r.json()

    def ready(self):
        for p in PATHS:
            self.file(p)


@pytest.fixture
def room(tmp_path, monkeypatch):
    return Room(tmp_path, monkeypatch)


def test_arrivals_resume_real_driver_loop_once_without_accepting_content(room, monkeypatch):
    room.claim()
    waits = []
    def listen(**kw):
        waits.append(kw["max_wait"])
        assert kw["max_wait"] <= DRIVE_CHAIN_WAIT
        if len(waits) == 1:
            room.ready()
        assert len(waits) <= 2, "artifact arrival never reached work dispatch"
        return 0
    monkeypatch.setattr(drive_mod, "run_listen", listen)
    assert room.driver.run(max_turns=1) == 0
    assert len(room.spawned) == 1 and "ARTIFACT RECONSIDERATION" in room.spawned[0]
    assert "does not accept content or clear another blocker" in room.spawned[0]
    assert room.driver._continuation_snapshot() is None
    assert room.new_driver()._continuation_snapshot() is None
    value = room.client.get(f"/channels/task-1/store/{KEY}", headers=room.seats["director"]).json()["value"]
    assert value["status"].startswith("blocked")
    assert room.driver._artifact_resume_receipts.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("missing", PATHS)
def test_absent_deleted_or_prefix_collision_cannot_satisfy(room, missing):
    room.claim()
    for p in PATHS:
        if p != missing:
            room.file(p)
    room.file(missing + ".old")
    room.file(missing, channel="other")
    assert room.driver._continuation_snapshot() is None
    assert room.driver._artifact_wait_pending
    room.file(missing)
    assert room.driver._continuation_snapshot() is not None
    r = room.client.delete(f"/channels/task-1/fs/{missing}", params={"expect_version": 1}, headers=room.seats["operator"])
    assert r.status_code == 200, r.text
    assert room.driver._continuation_snapshot() is None


def test_revision_threshold_and_progress_edits_do_not_rearm(room):
    row = room.claim()
    room.ready()
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    row = room.claim({**row["value"], "progress": "Owner rechecked the same remaining blocker"}, row["version"])
    assert room.driver._continuation_snapshot() is None
    row = room.claim({**row["value"], "progress": "Peer adds context only"}, row["version"], writer="peer")
    assert room.driver._continuation_snapshot() is None
    waits = row["value"]["waiting_for_artifacts"]
    waits[0]["min_version"] = 2
    room.claim({**row["value"], "waiting_for_artifacts": waits}, row["version"])
    assert room.driver._continuation_snapshot() is None
    room.file(waits[0]["path"], version=1)
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert len(room.spawned) == 2


@pytest.mark.parametrize("mutation", ["cancelled", "superseded", "stopped", "done", "clear", "dependency", "owner", "source"])
def test_dispatch_rechecks_changed_or_cancelled_claim(room, mutation):
    row = room.claim()
    room.ready()
    snap = room.driver._continuation_snapshot()
    changes = {"status": mutation}
    if mutation == "clear": changes = {"waiting_for_artifacts": None}
    if mutation == "dependency": changes = {"waiting_for_artifacts": [{"path": "shared/future.md", "min_version": 1}]}
    if mutation == "owner": changes = {"owner": "peer"}
    if mutation == "source":
        new_source = room.client.post("/channels/task-1/messages", headers=room.seats["operator"], json={
            "status":"open", "to":["director"], "body":"Superseding commission: wait for the revised manager synthesis."})
        assert new_source.status_code == 200
        changes = {"source_message_id":new_source.json()["id"]}
    room.claim({**row["value"], **changes}, row["version"], writer="operator")
    assert not room.driver._chain_step(snap)
    assert not room.spawned


@pytest.mark.parametrize("status", ["cancelled", "superseded", "stopped", "withdrawn", "retired", "dropped", "obsolete"])
def test_legacy_dependency_never_revives_finished_claim(room, monkeypatch, status):
    monkeypatch.setattr(room.driver, "_waiting_on_satisfied", lambda value: True)
    assert not room.driver._continuable(KEY, {"owner": "director", "status": status, "waiting_on": {"key": "x"}}, "task-1")


def test_provider_failure_and_crash_do_not_consume_success_opportunity(room):
    room.claim(); room.ready()
    def failed(prompt, sid):
        room.driver._last_turn_stage = "infrastructure"
        return None, False
    room.driver._spawn = failed
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert room.driver._work_attempt_unavailable
    assert room.driver._strike_count(f"task-1/{KEY}@1") == 0
    assert room.new_driver()._continuation_snapshot() is not None
    def crash(prompt, sid):
        raise RuntimeError("injected process interruption before owner reconsideration")
    room.driver._spawn = crash
    # Use a fresh driver so unrelated existing provider backoff is immaterial.
    driver = room.new_driver(); driver._spawn = crash
    with pytest.raises(RuntimeError): driver._chain_step(driver._continuation_snapshot())
    assert room.new_driver()._continuation_snapshot() is not None


@pytest.mark.parametrize("changes", [
    {"waiting_for_artifacts": None}, {"waiting_for_artifacts": [{"path": "shared/new.md", "min_version": 1}]},
    {"owner": "peer"}, {"status": "in_progress"},
])
def test_peer_cannot_change_owner_wait_or_resume(room, changes):
    row = room.claim()
    assert room.put({**row["value"], **changes}, row["version"], writer="peer").status_code == 403


def test_cas_nonobject_and_omission_preserve_contract(room):
    row = room.claim()
    assert room.put(row["value"], None).status_code == 400
    assert room.put(row["value"], 0).status_code == 409
    assert room.put("replace object", row["version"], writer="peer").status_code == 400
    closed = room.claim({"done": True}, row["version"], writer="peer")
    assert closed["value"]["waiting_for_artifacts"] == row["value"]["waiting_for_artifacts"]
    assert closed["value"]["owner"] == "director"
    assert room.driver._continuation_snapshot() is None


@pytest.mark.parametrize("waits, status", [([],400), ([{}],400),
    ([{"path":"../escape", "min_version":1}],400), ([{"path":"shared/x", "min_version":True}],400),
    ([{"path":"shared/x", "min_version":0}],400), ([{"path":"shared/x", "min_version":"1"}],400),
    ([{"path":"shared/x", "min_version":1}]*2,400),
    ([{"channel":"missing", "path":"shared/x", "min_version":1}],403),
    ([{"path":f"shared/{i}", "min_version":1} for i in range(65)],400)])
def test_invalid_or_unreadable_declarations_refused(room, waits, status):
    assert room.put(room.value(waiting_for_artifacts=waits)).status_code == status


def test_full_original_capacity_and_no_oldest_window_starvation(room):
    waits = [{"path": f"shared/report-{i}.md", "min_version": 1} for i in range(38)]
    row = room.claim(room.value(waiting_for_artifacts=waits))
    for i in range(105):
        room.claim({"owner": "peer", "status": "done"}, key=f"claim:newer-{i}", writer="peer")
    for dep in waits[:-1]: room.file(dep["path"])
    assert room.driver._continuation_snapshot() is None
    room.file(waits[-1]["path"])
    assert room.driver._continuation_snapshot() == ("task-1", KEY, row["version"])


def test_lost_artifact_readability_does_not_wake_or_prevent_closure(room):
    row = room.claim(room.value(waiting_for_artifacts=[{"channel":"other", "path":"shared/media.md", "min_version":1}]))
    room.file("shared/media.md", channel="other")
    snap = room.driver._continuation_snapshot()
    room.client.app.state.service.db.remove_member("other", "director")
    assert not room.driver._chain_step(snap)
    assert room.claim({"done": True}, row["version"])["value"]["done"]


@pytest.mark.parametrize("old_status", ["cancelled", "superseded", "paused"])
def test_peer_cannot_rearm_terminal_or_paused_history(room, old_status):
    row = room.claim(room.value(status=old_status))
    assert room.put({**row["value"], "status":"blocked"}, row["version"], writer="peer").status_code == 403


def test_peer_partial_progress_preserves_blocked_lifecycle_and_gate(room):
    row = room.claim()
    changed = room.claim({"progress":"New supporting context, no lifecycle decision"}, row["version"], writer="peer")
    assert changed["value"]["status"] == row["value"]["status"]
    assert changed["value"]["waiting_for_artifacts"] == row["value"]["waiting_for_artifacts"]
    assert room.driver._continuation_snapshot() is None


def test_consumed_gate_blocks_initiative_and_phase_but_not_addressed_reception(room, monkeypatch):
    room.claim(); room.ready()
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    # A newer phase is ignition, never permission to bypass the same wait.
    phase = {"steward":"director", "status":"open", "current":"review", "next_step":"Synthesize HEALTH"}
    # Direct storage here isolates selection; phase schema is tested elsewhere.
    room.client.app.state.service.db.store_set("task-1", "phase:health", phase, "operator")
    assert room.driver._continuation_snapshot() is None
    assert room.driver._artifact_wait_pending
    def forbidden_initiative():
        raise AssertionError("consumed dependency bypassed through initiative")
    monkeypatch.setattr(room.driver, "_initiative_step", forbidden_initiative)
    class IdleStop(Exception): pass
    monkeypatch.setattr(drive_mod, "run_listen", lambda **kw: (_ for _ in ()).throw(IdleStop()))
    with pytest.raises(IdleStop): room.driver.run(max_turns=1)
    assert len(room.spawned) == 1
    r = room.client.post("/channels/task-1/messages", headers=room.seats["operator"], json={
        "status":"open", "to":["director"], "body":"Clarify the outstanding Media boundary with evidence."})
    assert r.status_code == 200, r.text
    monkeypatch.setattr(drive_mod, "run_listen", lambda **kw: 2)
    assert room.driver.run(max_turns=1) == 0
    assert len(room.spawned) == 2
    assert "ARTIFACT RECONSIDERATION" not in room.spawned[-1]


def test_readiness_checks_emit_no_hub_messages_and_use_only_four_metadata_reads(room, monkeypatch):
    import httpx
    room.claim(); room.ready()
    before = room.client.app.state.service.db.get_messages("task-1", limit=200)
    actual = httpx.get
    metadata_calls = []
    def observe(url, **kwargs):
        if url.endswith("/fs"): metadata_calls.append((url,kwargs.get("params")))
        return actual(url, **kwargs)
    monkeypatch.setattr(httpx,"get",observe)
    snap = room.driver._continuation_snapshot()
    assert len(metadata_calls) == 4
    assert room.driver._chain_step(snap)
    assert len(metadata_calls) == 8  # fresh dispatch check; consumed-marker scan reads none
    assert [m.id for m in before] == [m.id for m in room.client.app.state.service.db.get_messages("task-1",limit=200)]


def test_peer_unlinked_source_prose_cannot_rearm_consumed_wait_in_driver_loop(room, monkeypatch):
    value = room.value(source="context A")
    value.pop("source_message_id")
    row = room.claim(value)
    room.ready()
    monkeypatch.setattr(drive_mod, "run_listen", lambda **kw: 0)
    assert room.driver.run(max_turns=1) == 0
    assert len(room.spawned) == 1
    edited = room.claim({**row["value"], "source":"context B"}, row["version"], writer="peer")
    assert not edited["value"].get("source_message_id")
    listens = []
    class IdleStop(Exception): pass
    def listen(**kw):
        listens.append(kw)
        if len(listens) > 1: raise IdleStop()
        return 0
    monkeypatch.setattr(drive_mod, "run_listen", listen)
    try:
        room.driver.run(max_turns=1)
    except IdleStop:
        pass
    assert len(room.spawned) == 1, "peer prose edit bought another artifact work turn"


def test_owner_new_canonical_source_can_request_another_reconsideration(room):
    row = room.claim(); room.ready()
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    new_source = room.client.post("/channels/task-1/messages", headers=room.seats["operator"], json={
        "status":"open", "to":["director"], "body":"Use these same artifacts to answer the revised commission."})
    assert new_source.status_code == 200, new_source.text
    room.claim({**row["value"], "source_message_id":new_source.json()["id"]}, row["version"])
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert len(room.spawned) == 2
