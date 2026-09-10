"""The documented claim shape must reach both hub and driver debt readers."""
from urllib.parse import quote, urlsplit

import pytest
from fastapi.testclient import TestClient

from agora.drive import Driver, TurnEvidence
from agora.hub.app import create_app


@pytest.fixture
def room(tmp_path, monkeypatch):
    client = TestClient(create_app(db_path=":memory:", admin_key="admin", rate_per_minute=6000))
    seats = {}
    for name in ("operator", "worker", "peer"):
        r = client.post("/agents", json={"id":name, "operator":name == "operator"},
                        headers={"Authorization":"Bearer admin"})
        assert r.status_code == 200, r.text
        seats[name] = {"Authorization":f"Bearer {r.json()['api_key']}"}
    for channel in ("task-1", "other"):
        assert client.post("/channels", json={"name":channel,"private":False},
                           headers=seats["operator"]).status_code == 200
        for name in ("worker", "peer"):
            assert client.post(f"/channels/{channel}/join",json={},headers=seats[name]).status_code == 200
    monkeypatch.setenv("AGORA_HOME",str(tmp_path))
    monkeypatch.setattr("agora.config.get_cached_key", lambda *a: seats["worker"]["Authorization"].split()[1])
    monkeypatch.setattr("httpx.get",lambda url,**kw: client.get(urlsplit(url).path,headers=seats["worker"]))
    driver = Driver("worker", "http://isolated", harness="codex",cwd=tmp_path)
    return client,seats,driver


def ask(room, *, sender="operator", channel="task-1"):
    client,seats,_ = room
    r=client.post(f"/channels/{channel}/messages",headers=seats[sender],json={
        "status":"open","to":["worker"],"title":"Commission: native swarm design",
        "body":"Audit the native swarm design and deliver a cited implementation plan."})
    assert r.status_code == 200,r.text
    return r.json()


def engage(room,message):
    client,seats,_=room
    r=client.post(f"/channels/{message['channel']}/messages",headers=seats["worker"],json={
        "status":"reply","reply_to":message["id"],"body":"I have started the assigned audit."})
    assert r.status_code == 200,r.text


def put(room,value,*,key="claim:swarm",version=0,writer="worker"):
    client,seats,_=room
    return client.put(f"/channels/task-1/store/{key}",headers=seats[writer],
                      json={"value":value,"expect_version":version})


def claim(source,**extra):
    return {"owner":"worker","status":"in_progress","next_step":"Run the lifecycle discriminator",
            "source":source,**extra}


def owed(room):
    client,seats,_=room
    return client.get("/owed",headers=seats["worker"]).json()["to_answer"]


@pytest.mark.parametrize("field",["source","source_message_id"])
def test_public_operator_claim_reaches_driver_without_completing_commission(room,field):
    client,seats,driver=room
    message=ask(room)
    driver._reception_debt_verification_required=True
    driver._reception_debt_before=driver._reception_debt()
    engage(room,message)
    assert not driver._verify_reception_debt(TurnEvidence(ok=True),"wake").ok
    ref=f"task-1#{message['seq']}"
    value={"owner":"worker","status":"in_progress","next_step":"Run the lifecycle discriminator",field:ref}
    r=put(room,value)
    assert r.status_code == 200,r.text
    assert r.json()["value"]["source_message_id"] == message["id"]
    assert driver._linked_claim_sources() == {message["id"]}
    assert driver._verify_reception_debt(TurnEvidence(ok=True),"wake").ok
    # A claimed commission is ongoing work, never delivered or accepted.
    assert message["id"] in {r["id"] for r in owed(room)}
    assert not any(m.status.value == "resolved" for m in client.app.state.service.db.replies_to(message["id"]))
    got=client.get("/channels/task-1/store/claim:swarm",headers=seats["worker"]).json()
    assert got["value"]["status"] == "in_progress" and got["version"] == 1


def test_public_peer_claim_moves_work_ownership_out_of_reception_debt(room):
    message=ask(room,sender="peer")
    engage(room,message)
    assert message["id"] in {r["id"] for r in owed(room)}
    assert put(room,claim(f"task-1#{message['seq']}")).status_code == 200
    assert message["id"] not in {r["id"] for r in owed(room)}


def test_equivalent_refs_canonicalize_but_conflicting_refs_are_refused(room):
    first,second=ask(room),ask(room)
    value=claim(f"task-1#{first['seq']}",source_message_id=first["id"])
    r=put(room,value)
    assert r.status_code == 200,r.text
    assert r.json()["value"]["source"] == value["source"]
    bad=put(room,claim(value["source"],source_message_id=second["id"]),key="claim:conflict")
    assert bad.status_code == 400 and "different messages" in bad.text


@pytest.mark.parametrize("case",["missing","malformed","foreign_channel","foreign_id","inaccessible","retracted","bad_canonical"])
def test_invalid_explicit_source_never_creates_ownership(room,case):
    client,seats,driver=room
    message=ask(room,channel="other" if case in ("foreign_channel","foreign_id","inaccessible") else "task-1")
    if case == "inaccessible":
        client.app.state.service.db.remove_member("other","worker")
    ref=f"{message['channel']}#{message['seq']}"
    if case == "missing":ref="task-1#999999"
    if case == "malformed":ref="task-1#not-a-number"
    if case == "retracted":
        assert client.post(f"/channels/task-1/messages/{message['id']}/retract",headers=seats["operator"]).status_code == 200
    value=claim(ref)
    if case == "foreign_id":value={"owner":"worker","status":"active","source_message_id":message["id"]}
    if case == "bad_canonical":value={"owner":"worker","status":"active","source_message_id":"not-an-id"}
    r=put(room,value)
    assert r.status_code in (400,403,404),r.text
    assert client.get("/channels/task-1/store/claim:swarm",headers=seats["worker"]).status_code == 404
    assert driver._linked_claim_sources() == set()


def test_prose_is_preserved_and_never_guessed_into_a_link(room):
    _,_,driver=room
    message=ask(room)
    text=f"Audit from task-1#{message['seq']} after the design discussion"
    r=put(room,claim(text))
    assert r.status_code == 200,r.text
    assert r.json()["value"]["source"] == text
    assert "source_message_id" not in r.json()["value"]
    assert driver._linked_claim_sources() == set()


@pytest.mark.parametrize("case",["valid","foreign","missing","retracted","prose"])
def test_legacy_source_rows_are_read_compatibly_without_storage_mutation(room,case):
    client,seats,driver=room
    message=ask(room,sender="peer",channel="other" if case == "foreign" else "task-1")
    engage(room,message)
    ref=f"{message['channel']}#{message['seq']}"
    if case == "missing":ref="task-1#999999"
    if case == "prose":ref=f"Follow-up to {ref} and the current design"
    if case == "retracted":
        assert client.post(f"/channels/task-1/messages/{message['id']}/retract",headers=seats["peer"]).status_code == 200
    value=claim(ref)
    db=client.app.state.service.db
    before=db.store_set("task-1","claim:swarm",value,"worker")
    got=client.get("/channels/task-1/store/claim:swarm",headers=seats["worker"]).json()
    if case != "valid":
        assert "source_message_id" not in got["value"]
        assert driver._linked_claim_sources() == set()
    else:
        assert got["value"]["source_message_id"] == message["id"]
        assert driver._linked_claim_sources() == {message["id"]}
        assert message["id"] not in {r["id"] for r in owed(room)}
    after=db.store_get("task-1","claim:swarm")
    assert after == before and "source_message_id" not in after.value


def test_peer_cannot_retarget_claim_but_can_preserve_source_while_closing(room):
    first,second=ask(room),ask(room)
    value=claim(f"task-1#{first['seq']}")
    assert put(room,value).status_code == 200
    replacement=claim(f"task-1#{second['seq']}")
    assert put(room,replacement,version=1,writer="peer").status_code == 403
    value["status"]="done"
    assert put(room,value,version=1,writer="peer").status_code == 200
    assert room[2]._linked_claim_sources() == set()


def test_closure_only_update_preserves_provenance_but_explicit_erasure_is_owned(room):
    message=ask(room)
    assert put(room,claim(f"task-1#{message['seq']}")).status_code == 200
    r=put(room,{"owner":"worker","source":None,"source_message_id":None},version=1,writer="peer")
    assert r.status_code == 403
    r=put(room,{"done":True},version=1,writer="peer")
    assert r.status_code == 200,r.text
    assert r.json()["value"]["owner"] == "worker"
    assert r.json()["value"]["source_message_id"] == message["id"]
    assert room[2]._linked_claim_sources() == set()


@pytest.mark.parametrize("channel", ["AF-Review", "task+1", "area#1", "équipe", "#"])
@pytest.mark.parametrize("field", ["source", "source_message_id"])
def test_claim_refs_accept_hub_channel_names_and_split_at_last_hash(room, channel, field):
    client, seats, _ = room
    assert client.post("/channels", json={"name":channel,"private":False},
                       headers=seats["operator"]).status_code == 200
    route = f"/channels/{quote(channel, safe='')}"
    assert client.post(f"{route}/join", json={}, headers=seats["worker"]).status_code == 200
    posted = client.post(f"{route}/messages", headers=seats["operator"], json={
        "status":"open", "to":["worker"], "body":"Perform the assigned audit."})
    assert posted.status_code == 200, posted.text
    message = posted.json()
    ref = f"{channel}#{message['seq']}"
    value = {"owner":"worker", "status":"in_progress", "next_step":"Audit", field:ref}
    result = client.put(f"{route}/store/claim:grammar", headers=seats["worker"],
                        json={"value":value,"expect_version":0})
    assert result.status_code == 200, result.text
    assert result.json()["value"]["source_message_id"] == message["id"]
    service = client.app.state.service
    assert service._normalize_claim_source(channel, value)["source_message_id"] == message["id"]
    # Broader channel spelling never makes a source in another room valid.
    foreign = put(room, value)
    assert foreign.status_code == 400 and "claim's channel" in foreign.text
    malformed = dict(value, **{field:f"{channel}#not-a-sequence"})
    bad = client.put(f"{route}/store/claim:malformed", headers=seats["worker"],
                     json={"value":malformed,"expect_version":0})
    assert bad.status_code == 400
