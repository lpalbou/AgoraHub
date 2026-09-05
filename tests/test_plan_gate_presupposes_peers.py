"""The delegate's plan-citation gate presupposes peers, like its sibling.

REPORTED by @delegate at `agora-and-wui#730`, measured on three of laurent's
own messages pinned open and escalating on HIS desk: `dm#104` ("be more
concise"), `dm#116`, and `commons#604`.

The review check one block above is already skipped when `peers` is empty —
"a delegate working alone keeps the old single-author rule (no deadlock)".
The plan check sat outside that guard. A `plan:` row records "each seat's
slice, the SEAMS between those slices, and how the contested points were
settled in the room"; in a two-party DM with the operator there is one seat,
no seam and no room, so the requirement is not inconvenient — it is
INCOHERENT, and the only way to satisfy it is to invent an agreement between
seats who never discussed anything.

It also ended a contradiction between two surfaces the hub owns: `check_inbox`
prints "Only an opinion to give? Record it as a decision:/finding: row IN THIS
CHANNEL and cite it with kind=store", and this gate returned 400 for exactly
that. The advice was right; the gate was over-broad.

Every test here was run against a mutant of the line it guards.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agora.hub.app import create_app

ADMIN_KEY = "admin-test-key"
AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}


@pytest.fixture()
def client(tmp_path) -> TestClient:
    return TestClient(create_app(db_path=str(tmp_path / "hub.db"),
                                 admin_key=ADMIN_KEY, rate_per_minute=6000.0,
                                 notify_dir=str(tmp_path / "notify")))


def _register(client, agent_id, operator=False):
    body = {"id": agent_id, "mission": f"seat {agent_id}"}
    if operator:
        body["operator"] = True
    r = client.post("/agents", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def _delegation(client, to_agent):
    r = client.put("/admin/delegation", headers=AUTH,
                   json={"agent_id": to_agent,
                         "powers": ["reporting", "operational"]})
    assert r.status_code == 200, r.text
    client.app.state.service._delegations_cache_at = 0.0


@pytest.fixture()
def cast(client):
    """An operator, a delegate holding `reporting`, and a spare peer."""
    boss = _register(client, "boss", operator=True)
    dele = _register(client, "dele")
    other = _register(client, "other")
    _delegation(client, "dele")
    return boss, dele, other


def _dm_instruction(client, boss, dele) -> str:
    """The operator sends a standing instruction by DM: no asks, no artifact,
    nothing to deliver. Returns the message id."""
    r = client.post("/dms/dele/messages", headers=boss,
                    json={"body": "be more concise", "status": "open",
                          "title": "be more concise"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _adopt(client, dele, parent_id, ref="decision:how-i-report-to-laurent"):
    """The close `check_inbox` itself recommends: a decision row, cited."""
    # No /dms/{peer}/store route exists: a DM's store is reached by the
    # channel name, which is alphabetical.
    w = client.put(f"/channels/dm:boss--dele/store/{ref}", headers=dele,
                   json={"value": {"what": "shorter reports, no preamble"},
                         "expect_version": 0})
    assert w.status_code == 200, w.text
    return client.post("/dms/boss/messages", headers=dele,
                       json={"body": "adopted; recorded how I will write",
                             "status": "resolved", "reply_to": parent_id,
                             "data": {"evidence": [{"kind": "store",
                                                    "ref": ref}]}})


def test_the_close_check_inbox_recommends_is_accepted_in_an_operator_dm(client, cast):
    """The whole defect, as one assertion. Before this, a 400 demanding a
    `plan:` row for a two-party DM about writing style."""
    boss, dele, _ = cast
    parent = _dm_instruction(client, boss, dele)

    r = _adopt(client, dele, parent)
    assert r.status_code == 200, r.text


def test_the_refusal_it_used_to_give_named_a_plan_that_could_not_exist(client, cast):
    """Pins the specific wrong refusal rather than 'some 400', so a future
    change that reintroduces a DIFFERENT refusal here still reds."""
    boss, dele, _ = cast
    parent = _dm_instruction(client, boss, dele)

    r = _adopt(client, dele, parent)
    assert "no delivery without the plan" not in r.text


def test_a_peopled_room_still_demands_the_plan(client, cast):
    """The narrowing must not become a hole. Where a plan COULD have been
    agreed, the requirement stands untouched — this is `commons#604`, which
    stays gated and correctly so."""
    boss, dele, other = cast
    r = client.post("/channels", headers=dele, json={"name": "work"})
    assert r.status_code == 200, r.text
    for who in (boss, other):
        token = client.post("/channels/work/invites", json={},
                            headers=dele).json()["invite_token"]
        client.post("/channels/work/join", json={"invite_token": token},
                    headers=who)
    root = client.post("/channels/work/messages", headers=boss,
                       json={"body": "build the board", "status": "open",
                             "title": "board"}).json()
    client.put("/channels/work/store/finding:x", headers=dele,
               json={"value": {"what": "notes"}, "expect_version": 0})
    # A PEER-AUTHORED citation, so the REVIEW gate is satisfied and this test
    # actually reaches the plan gate. Without it the review refusal fires
    # first and this test passes with the plan requirement DELETED — which is
    # exactly what happened on the first draft: the mutant that removes the
    # requirement entirely SURVIVED. A check whose target can be deleted
    # while it stays green is decoration.
    client.put("/channels/work/store/review:x", headers=other,
               json={"value": {"verdict": "cold-read, two defects"},
                     "expect_version": 0})

    r = client.post("/channels/work/messages", headers=dele,
                    json={"body": "delivered", "status": "resolved",
                          "reply_to": root["id"],
                          "data": {"evidence": [
                              {"kind": "store", "ref": "finding:x"},
                              {"kind": "store", "ref": "review:x"}]}})
    assert r.status_code == 400, r.text
    assert "no delivery without the plan" in r.text


def test_an_uncited_resolved_is_still_refused_in_the_dm(client, cast):
    """The narrowing touches the PLAN requirement only. The evidence
    requirement is what stops a delegate closing an operator on prose, and it
    is untouched — otherwise this fix would trade one hole for a worse one."""
    boss, dele, _ = cast
    parent = _dm_instruction(client, boss, dele)

    r = client.post("/dms/boss/messages", headers=dele,
                    json={"body": "done", "status": "resolved",
                          "reply_to": parent})
    assert r.status_code == 400
