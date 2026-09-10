"""Reply dependency scheduling must not read answers or erase ownership."""
import pytest

from test_drive_answer_wait import KEY, Room


@pytest.fixture
def room(tmp_path, monkeypatch):
    return Room(tmp_path, monkeypatch)


def test_reply_state_preserves_unread_answer_and_filters_after_cursor(room):
    answer = room.answer()
    service = room.client.app.state.service
    before = service.db.has_read(answer["id"], "director")
    response = room.client.get(
        f"/channels/task-1/messages/{room.root}/reply-state",
        headers=room.seats["director"])
    assert response.status_code == 200
    assert [r["id"] for r in response.json()["responses"]] == [answer["id"]]
    assert service.db.has_read(answer["id"], "director") == before is False
    later = room.client.get(
        f"/channels/task-1/messages/{room.root}/reply-state",
        params={"after_seq": answer["seq"]}, headers=room.seats["director"])
    assert later.json()["responses"] == []


def test_partial_progress_keeps_wait_and_requires_cas(room):
    row = room.claim()
    url = f"/channels/task-1/store/{KEY}"
    no_cas = room.client.put(url, headers=room.seats["director"], json={"value": {"next_step": "Await review"}})
    assert no_cas.status_code == 400
    changed = room.client.put(url, headers=room.seats["director"],
                             json={"value": {"next_step": "Review on arrival"}, "expect_version": row["version"]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["value"]["waiting_for_answers"] == row["value"]["waiting_for_answers"]
    assert changed.json()["value"]["owner"] == "director"


@pytest.mark.parametrize("changes", [
    {"waiting_for_answers": None}, {"wait_until": 1234567}, {"status": "active"},
    {"source_message_id": "replacement"}, {"owner": "peer"},
])
def test_peer_cannot_rewrite_wait_lifecycle(room, changes):
    row = room.claim()
    response = room.client.put(f"/channels/task-1/store/{KEY}", headers=room.seats["peer"],
                               json={"value": {**row["value"], **changes}, "expect_version": row["version"]})
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("entry", [{}, {"message_id": "missing"},
    {"message_id": "ROOT", "after_seq": True}, {"message_id": "ROOT", "after_seq": -1},
    {"message_id": "ROOT", "extra": "ignored"}, {"message_id": "ROOT", "channel": "missing"}])
def test_invalid_wait_declaration_is_refused(room, entry):
    entry = {k: room.root if v == "ROOT" else v for k, v in entry.items()}
    response = room.client.put(f"/channels/task-1/store/{KEY}", headers=room.seats["director"],
                               json={"value": {"owner": "director", "waiting_for_answers": [entry]}, "expect_version": 0})
    assert response.status_code in (400, 403, 404), response.text
