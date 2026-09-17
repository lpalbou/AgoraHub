"""A retired unchanged claim gets a fresh context, without extra admission."""

import pytest

from agora.drive import Driver, WORK_BOOT_PROMPT, WORK_STRIKES, WORK_STRIKE_TTL


@pytest.fixture
def stalled_driver(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    calls, rotations = [], []

    def spawn(prompt, session):
        calls.append((prompt, session))
        driver._last_turn_stage = "mcp-use"
        return session or "fresh-work", False

    driver = Driver("worker", "http://127.0.0.1:1", spawn=spawn)
    driver._activate_work_claim("room", "claim:work")
    driver.work_session_id = "old-work"
    driver._write_session(driver._work_session_path, "old-work")
    driver._work_turns_on_session = 12
    driver.reception_session_id = "reception"
    snapshot = ("room", "claim:work", 7)
    monkeypatch.setattr(driver, "_continuation_snapshot", lambda: snapshot)
    monkeypatch.setattr(driver, "_read_work_row", lambda *_: None)
    monkeypatch.setattr(driver, "_receipt_elsewhere", lambda *_: False)

    def rotate(lane):
        # The native invocation must have returned before resetting context.
        assert driver._turn_kind is None
        rotations.append((lane, len(calls)))

    monkeypatch.setattr(driver._adapter, "rotate_session", rotate)
    return driver, snapshot, calls, rotations


def test_retirement_rotates_only_work_without_granting_a_retry(stalled_driver):
    driver, snapshot, calls, rotations = stalled_driver
    key = "room/claim:work@7"
    for _ in range(WORK_STRIKES - 1):
        assert driver._chain_step(snapshot)
        assert driver.work_session_id == "old-work"
        assert rotations == []
    assert driver._chain_step(snapshot)
    assert rotations == [("work", WORK_STRIKES)]
    assert driver.work_session_id is None
    assert not driver._work_session_path.exists()
    assert driver._work_turns_on_session == 0
    assert driver.reception_session_id == "reception"
    assert driver._work_claim_ref == "room/claim:work"
    assert driver._strike_count(key) == WORK_STRIKES
    assert len(driver._work_times) == WORK_STRIKES
    assert not driver._chain_step(snapshot)
    assert len(calls) == WORK_STRIKES
    assert rotations == [("work", WORK_STRIKES)]

    # Normal expiry, not rotation, makes the unchanged row eligible again.
    driver._work_strike_at[key] -= WORK_STRIKE_TTL + 1
    assert driver._chain_step(snapshot)
    assert calls[-1] == (WORK_BOOT_PROMPT, None)
    assert driver.work_session_id == "fresh-work"


def test_off_row_progress_preserves_context(stalled_driver, monkeypatch):
    driver, snapshot, calls, rotations = stalled_driver
    monkeypatch.setattr(driver, "_receipt_elsewhere", lambda *_: True)
    for _ in range(WORK_STRIKES + 1):
        assert driver._chain_step(snapshot)
    assert driver.work_session_id == "old-work"
    assert driver._strike_count("room/claim:work@7") == 0
    assert rotations == []


def test_transport_outage_does_not_trigger_stall_rotation(stalled_driver, monkeypatch):
    driver, snapshot, calls, rotations = stalled_driver

    def unavailable(prompt, session):
        driver._last_turn_stage = "infrastructure"
        return None, False

    driver._spawn = unavailable
    monkeypatch.setattr(driver, "_backoff_retry_after", lambda: 0)
    for _ in range(WORK_STRIKES + 1):
        assert driver._chain_step(snapshot)
    assert driver._strike_count("room/claim:work@7") == 0
    assert rotations == []
