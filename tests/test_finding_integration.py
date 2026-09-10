"""Opt-in task-finding delivery gate: content identity, not prose mention."""
from __future__ import annotations

import hashlib

import pytest

from agora.db import Database
from agora.hub.service import HubError, HubService
from agora.models import PostMessage, Status


def _lab():
    hub = HubService(Database(":memory:"), rate_per_minute=600.0)
    op, _ = hub.register_agent("op", "Op", operator=True)
    lead, _ = hub.register_agent("lead", "Lead")
    worker, _ = hub.register_agent("worker", "Worker")
    hub.create_channel(op, "room", private=False)
    hub.join_channel(lead, "room", None)
    hub.join_channel(worker, "room", None)
    root = hub.post_message(op, "room", PostMessage(title="Audit", body="deliver roadmap", status=Status.open, to=["lead"]))
    task = f"task:msg-{root.seq}"
    hub.store_set(op, "room", task, {"coordinator": "lead"})
    return hub, op, lead, worker, root, task


def _artifact(hub, lead, text="The cancellation result must be suppressed in history."):
    f = hub.fs_write(lead, "room", "ROADMAP.md", content=text, description="current roadmap")
    return {"path": f.path, "version": f.version,
            "sha256": f.sha256, "excerpt": text}


def _accepted(hub, lead, task, root, suffix="cancel"):
    key = f"finding:{task[5:]}:{suffix}"
    hub.store_set(lead, "room", key, {
        "kind": "task-finding-v1", "task": {"channel": "room", "key": task},
        "state": "accepted", "source": f"room#{root.seq}",
        "evidence": [{"kind": "store", "ref": task}],
        "contract": "Cancelled work never publishes a result into history.",
    }, expect_version=0)
    return key


def _dispose(hub, lead, key, artifact, disposition="incorporated", **more):
    current = hub.db.store_get("room", key)
    value = {**current.value, "state": "disposed", "disposition": disposition,
             "artifact": artifact,
             "disposition_evidence": [{"kind": "fs", "ref": f"{artifact['path']}@{artifact['version']}"}],
             **more}
    hub.store_set(lead, "room", key, value, expect_version=current.version)


def _deliver(hub, lead, worker, root, artifact, body="delivery"):
    hub.store_set(lead, "room", "decision:done", {"what": "done"})
    return hub.post_message(lead, "room", PostMessage(
        body=body, status=Status.resolved, reply_to=root.id,
        data={"evidence": [{"kind": "fs", "ref": f"{artifact['path']}@{artifact['version']}"},
                           {"kind": "store", "ref": "decision:done"}]}))


def test_accepted_finding_blocks_even_when_report_mentions_it():
    hub, _op, lead, worker, root, task = _lab()
    artifact = _artifact(hub, lead)
    _accepted(hub, lead, task, root)
    with pytest.raises(HubError, match="accepted findings"):
        _deliver(hub, lead, worker, root, artifact, body="random unrelated finding is mentioned here")
    assert hub.db.replies_to(root.id) == []


def test_current_integrated_artifact_cited_by_delivery_is_green():
    hub, _op, lead, worker, root, task = _lab()
    artifact = _artifact(hub, lead)
    key = _accepted(hub, lead, task, root)
    _dispose(hub, lead, key, artifact)
    _deliver(hub, lead, worker, root, artifact)
    assert hub.db.store_get("room", task).value["status"] == "delivered"


def test_new_artifact_revision_without_excerpt_turns_delivery_red():
    hub, _op, lead, worker, root, task = _lab()
    artifact = _artifact(hub, lead)
    key = _accepted(hub, lead, task, root)
    _dispose(hub, lead, key, artifact)
    hub.fs_write(lead, "room", "ROADMAP.md", content="replacement without the finding", expect_version=artifact["version"])
    summary = hub.finding_integration_summary("room", task)
    assert summary["pending"] == [key]
    assert summary["rows"][0]["integration_status"] == "stale"
    with pytest.raises(HubError, match="stale"):
        _deliver(hub, lead, worker, root, artifact)
    assert hub.db.replies_to(root.id) == []


def test_disposition_requires_authority_and_real_proof():
    hub, op, lead, worker, root, task = _lab()
    artifact = _artifact(hub, lead)
    key = _accepted(hub, lead, task, root)
    row = hub.db.store_get("room", key)
    with pytest.raises(HubError, match="rejecting/superseding"):
        hub.store_set(lead, "room", key, {**row.value, "state": "disposed",
            "disposition": "rejected", "reason": "Coordinator cannot reject this accepted claim alone.",
            "disposition_evidence": [{"kind": "store", "ref": task}]}, expect_version=row.version)
    with pytest.raises(HubError, match="task row is written"):
        hub.store_set(worker, "room", key, {**row.value, "state": "disposed",
            "disposition": "incorporated", "artifact": artifact,
            "disposition_evidence": [{"kind": "external", "ref": "fake", "sha256": "0" * 64, "size_bytes": 0}]}, expect_version=row.version)
    with pytest.raises(HubError, match="excerpt"):
        _dispose(hub, lead, key, {**artifact, "excerpt": " "})


def test_reject_is_a_valid_recorded_disposition_and_generic_rows_remain_freeform():
    hub, op, lead, worker, root, task = _lab()
    key = _accepted(hub, lead, task, root)
    row = hub.db.store_get("room", key)
    # This is only minimum internal verifiability: a self-authored store row
    # proves it exists, not that the rejection is semantically adequate.
    hub.store_set(op, "room", "decision:operator-counterexample", {"what": "counterexample"})
    hub.store_set(op, "room", key, {**row.value, "state": "disposed", "disposition": "rejected",
        "reason": "Counterexample shows the originally reported path is unreachable.",
        "disposition_evidence": [{"kind": "store", "ref": "decision:operator-counterexample"}]}, expect_version=row.version)
    generic = hub.store_set(lead, "room", "finding:freeform", {"verified": True, "anything": "allowed"})
    assert generic.value["verified"] is True
    artifact = _artifact(hub, lead)
    _deliver(hub, lead, worker, root, artifact)


def test_accepted_evidence_is_historical_and_a_later_store_edit_cannot_poison_disposition():
    hub, op, lead, worker, root, task = _lab()
    hub.store_set(lead, "room", "decision:accepted-proof", {"observed": "v1"})
    key = f"finding:{task[5:]}:historical-evidence"
    hub.store_set(lead, "room", key, {
        "kind": "task-finding-v1", "task": {"channel": "room", "key": task},
        "state": "accepted", "source": f"room#{root.seq}",
        "evidence": [{"kind": "store", "ref": "decision:accepted-proof"}],
        "contract": "The accepted evidence remains the observed historical fact.",
    }, expect_version=0)
    accepted = hub.db.store_get("room", key)
    hub.store_set(lead, "room", "decision:accepted-proof", {"observed": "v2"})
    hub.store_set(op, "room", key, {**accepted.value, "state": "disposed", "disposition": "rejected",
        "reason": "The later review rejects the finding on an independent counterexample.",
        "disposition_evidence": [{"kind": "store", "ref": task}]}, expect_version=accepted.version)
    disposed = hub.db.store_get("room", key).value
    assert disposed["evidence"] == accepted.value["evidence"]


def test_cross_task_target_cycle_and_omitted_binding_are_refused():
    hub, op, lead, _worker, root, task = _lab()
    artifact = _artifact(hub, lead)
    a = _accepted(hub, lead, task, root, "a")
    other_root = hub.post_message(op, "room", PostMessage(title="Other", body="other", status=Status.open))
    other = f"task:msg-{other_root.seq}"
    hub.store_set(op, "room", other, {"coordinator": "lead"})
    b = _accepted(hub, lead, other, other_root, "b")
    with pytest.raises(HubError, match="for this task"):
        _dispose(hub, lead, a, artifact, "merged", target=b)
    row = hub.db.store_get("room", a)
    # Partial updates preserve the typed discriminator/task binding rather
    # than turning the row back into generic freeform state.
    preserved = hub.store_set(lead, "room", a, {"state": "accepted"}, expect_version=row.version)
    assert preserved.value["kind"] == "task-finding-v1"
    c = _accepted(hub, lead, task, root, "c")
    _dispose(hub, lead, a, artifact, "merged", target=c)
    with pytest.raises(HubError, match="cycle"):
        _dispose(hub, lead, c, artifact, "merged", target=a)


def test_typed_writes_need_cas_and_accepted_claim_fields_are_immutable():
    hub, _op, lead, worker, root, task = _lab()
    key = f"finding:{task[5:]}:cas"
    raw = {"kind": "task-finding-v1", "task": {"channel": "room", "key": task},
           "state": "accepted", "source": f"room#{root.seq}",
           "evidence": [{"kind": "store", "ref": task}], "contract": "A bounded accepted contract that must not mutate."}
    with pytest.raises(HubError, match="expect_version"):
        hub.store_set(lead, "room", key, raw)
    hub.store_set(lead, "room", key, raw, expect_version=0)
    row = hub.db.store_get("room", key)
    with pytest.raises(HubError, match="immutable"):
        hub.store_set(lead, "room", key, {**row.value, "contract": "A different claim."}, expect_version=row.version)


def test_forged_current_digest_cannot_hide_removed_excerpt_or_insert_race(monkeypatch):
    hub, _op, lead, worker, root, task = _lab()
    artifact = _artifact(hub, lead)
    key = _accepted(hub, lead, task, root)
    _dispose(hub, lead, key, artifact)
    replacement = "A current roadmap revision without the accepted cancellation sentence."
    newer = hub.fs_write(lead, "room", "ROADMAP.md", content=replacement, expect_version=artifact["version"])
    forged = {"path": newer.path, "version": newer.version,
              "sha256": hashlib.sha256(replacement.encode()).hexdigest(),
              "excerpt": artifact["excerpt"]}
    row = hub.db.store_get("room", key)
    with pytest.raises(HubError, match="proof does not match"):
        hub.store_set(lead, "room", key, {**row.value, "artifact": forged}, expect_version=row.version)

    # The delivery path validates once for early feedback and once under the
    # lock immediately before insert; a VFS write between them turns it red.
    fresh = _artifact(hub, lead, artifact["excerpt"])
    row = hub.db.store_get("room", key)
    hub.store_set(lead, "room", key, {**row.value, "artifact": fresh}, expect_version=row.version)
    original = hub._validate_task_delivery
    calls = 0
    def raced(*args, **kwargs):
        nonlocal calls
        original(*args, **kwargs)
        if calls == 0:
            calls += 1
            hub.fs_write(lead, "room", "ROADMAP.md", content="raced away", expect_version=fresh["version"])
    monkeypatch.setattr(hub, "_validate_task_delivery", raced)
    with pytest.raises(HubError, match="stale"):
        _deliver(hub, lead, worker, root, fresh)
    assert hub.db.replies_to(root.id) == []
