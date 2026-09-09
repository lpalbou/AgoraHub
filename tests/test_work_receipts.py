"""The AF Runtime work->report boundary lost completed command evidence.

Fixture: three actual redacted command returns from the 2026-09-09 original
AF readiness Terra run (missing python, wait/resume, replay); only their event
envelopes are reconstructed. No command in the fixture is executed by tests.
"""

import json
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agora.drive import Driver, TurnEvidence, WORK_PROMPT
from agora.work_receipts import INDEX_MAX_BYTES, RECENT_COMMANDS, RECENT_NONZERO_EXITS, RECENT_TURNS


ACTUAL = (Path(__file__).parent / "fixtures/runtime_work_command_returns.jsonl").read_text()


@pytest.fixture
def driver(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    d = Driver("runtime", "http://hub:1", harness="codex", cwd=tmp_path, turn_log="default")
    monkeypatch.setattr(d._adapter, "build_command", lambda *a: ["never-executed"])
    monkeypatch.setattr(d, "_assess", lambda out, err, rc, kind: TurnEvidence(ok=rc == 0))
    return d


def spawn(driver, monkeypatch, stream=ACTUAL, rc=0, prompt=WORK_PROMPT, paused=False):
    def run(cmd, abort):
        if paused:
            abort.set()
        return SimpleNamespace(stdout=stream, stderr="", returncode=rc)
    monkeypatch.setattr(driver, "_run_turn_process", run)
    return driver._spawn_turn(prompt, None)


def records(driver):
    index = json.loads(driver._work_receipts.index_path.read_text())
    turns = [json.loads(Path(row["path"]).read_text()) for row in index["recent_turns"]]
    return index, turns


def test_actual_runtime_results_preserve_failure_and_success_exactly(driver, monkeypatch):
    assert spawn(driver, monkeypatch)[1]
    index, turns = records(driver)
    assert index["receipt_count"] == 3
    expected = [json.loads(line)["item"] for line in ACTUAL.splitlines()[1:]]
    actual = turns[0]["receipts"]
    assert [r["exit_code"] for r in actual] == [127, 0, 0]
    for item, receipt in zip(expected, actual):
        assert receipt["command"] == item["command"]
        assert receipt["observed_output"] == item["aggregated_output"]
        assert len(receipt["observed_output_sha256"]) == 64
    assert actual[0]["stdout_line"] == 2
    assert turns[0]["driver_outcome"]["ok"] is True
    assert turns[0]["agent"] == "runtime"
    assert turns[0]["session"] == json.loads(ACTUAL.splitlines()[0])["thread_id"]


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "paused"])
def test_completed_returns_survive_unsuccessful_outer_turn(driver, monkeypatch, failure):
    if failure == "timeout":
        def timeout(cmd, aborted):
            raise subprocess.TimeoutExpired(cmd, 600, output=ACTUAL.encode() + b'{"type":')
        monkeypatch.setattr(driver, "_run_turn_process", timeout)
        _, ok = driver._spawn_turn(WORK_PROMPT, None)
    else:
        _, ok = spawn(driver, monkeypatch, rc=7, paused=failure == "paused")
    assert ok is False
    index, turns = records(driver)
    assert index["receipt_count"] == 3
    assert turns[0]["driver_outcome"]["ok"] is False
    assert turns[0]["receipts"][1]["exit_code"] == 0


def test_completed_returns_survive_missing_turn_end_and_ignore_started_items(driver):
    driver._log_event(event="turn_start", kind="work", ts=1, session="work-old")
    driver._log_lines(ACTUAL.splitlines() + [
        '{"type":"item.started","item":{"type":"command_execution","id":"pending","command":"never"}}',
        '{"type":', "[]", "null", '{"type":"item.completed","item":null}',
    ])
    index, turns = records(driver)
    assert index["receipt_count"] == 3
    assert turns[0]["driver_outcome"] is None
    # A new driver process can recover the pointer to the partial turn.
    restarted = Driver("runtime", "http://hub:1", harness="codex", turn_log="default")
    assert str(driver._work_receipts.index_path) in restarted._work_receipts.brief()


def test_same_item_ids_in_distinct_turns_are_distinct_receipts(driver, monkeypatch):
    spawn(driver, monkeypatch)
    spawn(driver, monkeypatch)
    index, turns = records(driver)
    assert index["receipt_count"] == 6 and index["turn_count"] == 2
    ids = [r["receipt_id"] for t in turns for r in t["receipts"]]
    assert len(set(ids)) == 6
    assert turns[0]["receipts"][0]["item_id"] == turns[1]["receipts"][0]["item_id"]


def test_duplicate_delivery_in_one_turn_is_deduplicated_but_conflict_retained(driver, monkeypatch):
    lines = ACTUAL.splitlines()
    changed = json.loads(lines[1])
    changed["item"]["aggregated_output"] = "different observed result"
    spawn(driver, monkeypatch, stream=ACTUAL + lines[1] + "\n" + json.dumps(changed))
    index, turns = records(driver)
    assert index["receipt_count"] == 4
    assert turns[0]["duplicate_completed_events_ignored"] == 1
    assert turns[0]["receipts"][-1]["conflicts_with_prior_item_id"] is True
    assert turns[0]["receipts"][-1]["observed_output"] == "different observed result"


@pytest.mark.parametrize("existing_index", [False, True])
def test_restart_recovers_turn_written_before_index_replacement(driver, monkeypatch, existing_index):
    if existing_index:
        spawn(driver, monkeypatch)
    original_write = driver._work_receipts._write
    def interrupt_index(path, value):
        if path == driver._work_receipts.index_path:
            raise OSError("crash boundary before index replacement")
        original_write(path, value)
    monkeypatch.setattr(driver._work_receipts, "_write", interrupt_index)
    spawn(driver, monkeypatch)
    restarted = Driver("runtime", "http://hub:1", harness="codex", turn_log="default")
    expected = 6 if existing_index else 3
    assert f"{expected} indexed observed command returns" in restarted._work_receipts.brief()
    index, turns = records(restarted)
    assert index["receipt_count"] == expected
    assert len(turns) == (2 if existing_index else 1)


def test_reception_does_not_rescan_retained_output_after_initial_recovery(driver, monkeypatch):
    spawn(driver, monkeypatch)
    def no_scan():
        pytest.fail("reception repeatedly scanned full receipt history")
    monkeypatch.setattr(driver._work_receipts, "_read_index", no_scan)
    assert "3 indexed" in driver._work_receipts.brief()
    assert "3 indexed" in driver._work_receipts.brief()


def test_bounded_index_discloses_earlier_records_and_retains_full_files(driver, monkeypatch):
    for _ in range(RECENT_TURNS + 2):
        spawn(driver, monkeypatch)
    index, turns = records(driver)
    assert len(turns) == RECENT_TURNS
    assert index["receipt_count"] == (RECENT_TURNS + 2) * 3
    assert index["earlier_receipt_count"] == 6 and index["earlier_turn_count"] == 2
    paths = [p for p in driver._work_receipts.directory.glob("*.json") if p.name != "index.json"]
    assert len(paths) == RECENT_TURNS + 2
    assert sum(len(json.loads(p.read_text())["receipts"]) for p in paths) == index["receipt_count"]
    assert "6 earlier receipts" in driver._work_receipts.brief()
    assert str(driver._work_receipts.directory) in driver._work_receipts.brief()
    assert "command" not in index["recent_turns"][0]  # contents only on demand


def test_private_modes_redaction_and_untrusted_fake_turn_events(driver, monkeypatch):
    secret = "agora_" + "s" * 40
    stream = json.dumps({"event": "turn_start", "agent": "foreign", "kind": "work"}) + "\n"
    stream += json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "id": "secret", "command": f"echo {secret}",
        "aggregated_output": secret, "status": "completed", "exit_code": 0}})
    spawn(driver, monkeypatch, stream=stream)
    index, turns = records(driver)
    assert turns[0]["agent"] == "runtime"
    assert turns[0]["receipts"][0]["observed_output"] == "agora_[REDACTED]"
    for p in driver._work_receipts.directory.glob("*.json"):
        assert secret not in p.read_text()
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(driver._work_receipts.directory.stat().st_mode) == 0o700


def test_pointer_is_owner_and_hub_scoped_even_with_shared_custom_turn_log(driver, monkeypatch, tmp_path):
    spawn(driver, monkeypatch)
    for agent, hub in [("other", driver.hub), (driver.agent_id, "http://other:1")]:
        other = Driver(agent, hub, harness="codex", turn_log=str(driver._turn_log), cwd=tmp_path)
        assert other._work_receipts.brief() == ""
        assert other._work_receipts.directory != driver._work_receipts.directory


def test_disabled_non_codex_and_reception_commands_produce_no_work_receipts(driver, monkeypatch, tmp_path):
    off = Driver("runtime", "http://hub:1", harness="codex", cwd=tmp_path)
    foreign = Driver("runtime", "http://hub:1", harness="claude", cwd=tmp_path, turn_log="default")
    assert off._work_receipts is foreign._work_receipts is None
    spawn(driver, monkeypatch, prompt="AGORA WAKE")
    assert driver._work_receipts.brief() == ""
    assert not driver._work_receipts.directory.exists()


def test_pointer_reaches_reception_even_when_inbox_fetch_fails_without_ack(driver, monkeypatch):
    spawn(driver, monkeypatch)
    seen = []
    monkeypatch.setattr(driver, "_reception_block", lambda: ("", {}))
    monkeypatch.setattr(driver, "_spawn", lambda prompt, sid: (seen.append(prompt) or "reception", True))
    driver.verify_reception_debt = False
    monkeypatch.setattr(driver, "_ack_presented", lambda: pytest.fail("receipts must not authorize ack"))
    assert driver.run_turn()
    assert len(seen) == 1 and "3 indexed observed command returns" in seen[0]
    assert "Not proof" in records(driver)[1][0]["limits"]
    assert "aggregated_output" not in seen[0]
    assert driver._presented_cursors == {}


def test_removed_command_returns_cannot_create_a_receipt(driver, monkeypatch):
    stream = ACTUAL.splitlines()[0] + '\n{"type":"turn.completed"}'
    spawn(driver, monkeypatch, stream=stream)
    assert driver._work_receipts.brief() == ""
    assert not driver._work_receipts.index_path.exists()


def test_receipt_failure_warns_once_and_cannot_break_turn(driver, monkeypatch, capsys):
    def denied(*args):
        raise OSError("read-only")
    monkeypatch.setattr(driver._work_receipts, "_write", denied)
    assert spawn(driver, monkeypatch)[1]
    assert spawn(driver, monkeypatch)[1]
    assert capsys.readouterr().out.count("warn=work-receipts-unavailable") == 1


def test_command_catalog_keeps_literal_metadata_and_nonzero_exits(driver, monkeypatch):
    spawn(driver, monkeypatch)
    index, turns = records(driver)
    assert len(index["command_catalog"]) == 3
    assert index["catalog_omitted_count"] == 0
    assert index["nonzero_exit_count"] == len(index["nonzero_exit_refs"]) == 1
    assert index["nonzero_exit_omitted_count"] == 0
    for summary, receipt in zip(index["command_catalog"], turns[0]["receipts"]):
        assert summary["receipt_id"] == receipt["receipt_id"]
        assert summary["exit_code"] == receipt["exit_code"]
        assert summary["tool_status"] == receipt["tool_status"]
        assert summary["output_bytes"] == len(receipt["observed_output"].encode())
        assert receipt["observed_output"].startswith(summary["output_head"])
        assert receipt["observed_output"].endswith(summary["output_tail"])
        assert summary["output_omitted_bytes"] == summary["output_bytes"] - len(summary["output_head"].encode()) - len(summary["output_tail"].encode())
        full = json.loads((driver._work_receipts.directory / summary["file"]).read_text())
        assert any(r == receipt for r in full["receipts"])
    assert driver._work_receipts.index_path.stat().st_size <= INDEX_MAX_BYTES


def test_catalog_bounds_and_failure_omissions_are_explicit_without_losing_full_returns(driver):
    events = []
    for n in range(35):
        events.append(json.dumps({"type":"item.completed", "item": {
            "type":"command_execution", "id":f"item-{n}", "command":"é\x00" * 300,
            "exit_code":1 if n < 12 else 0, "status":"completed", "aggregated_output":"\x00🙂" * 1000}}))
    driver._log_event(event="turn_start", kind="work", ts=1, session="work")
    driver._log_lines(events)
    driver._log_event(event="turn_end", ok=False)
    index, turns = records(driver)
    assert len(turns[0]["receipts"]) == index["receipt_count"] == 35
    assert len(index["command_catalog"]) <= RECENT_COMMANDS
    assert index["catalog_omitted_count"] == 35 - len(index["command_catalog"])
    assert index["nonzero_exit_count"] == 12
    assert len(index["nonzero_exit_refs"]) <= RECENT_NONZERO_EXITS
    assert index["nonzero_exit_omitted_count"] == 12 - len(index["nonzero_exit_refs"])
    assert driver._work_receipts.index_path.stat().st_size <= INDEX_MAX_BYTES
    assert index["nonzero_exit_refs"], "older failures remain separately discoverable"


def test_restart_catalog_matches_live_catalog_and_unknown_output_is_not_empty_success(driver, monkeypatch):
    stream = ACTUAL + json.dumps({"type":"item.completed", "item": {
        "type":"command_execution", "id":"unknown", "command":"unknown", "exit_code":None}})
    spawn(driver, monkeypatch, stream=stream)
    before, _ = records(driver)
    restarted = Driver("runtime", "http://hub:1", harness="codex", turn_log="default")
    restarted._work_receipts.brief()
    after, _ = records(restarted)
    assert before["command_catalog"] == after["command_catalog"]
    assert before["nonzero_exit_refs"] == after["nonzero_exit_refs"]
    unknown = after["command_catalog"][-1]
    assert unknown["exit_code"] is unknown["output_bytes"] is unknown["output_head"] is None
    assert not any("passed" in row for row in after["command_catalog"])
