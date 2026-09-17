"""Optional collective questions, derived from the ordinary message ledger.

Collection is a deterministic predicate. Reconciliation remains an authored
decision. No process identity, model polling, or second workflow store is used.
"""
from __future__ import annotations

import hashlib
import json
import math
import time

from ..models import Kind, PostMessage, Status
from .obligations import asks_of, declines_of, substantive_answers_of
from .orchestration import refuse


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def is_consultation(message):
    policy = (message.data or {}).get("consultation") if message else None
    return isinstance(policy, dict) and policy.get("kind") == "consultation-v1"


class ConsultationsMixin:
    def _validate_consultation(self, agent, channel, payload, data):
        if not data or "consultation" not in data:
            return data
        raw = data["consultation"]
        fields = {"required", "eligible", "min_responses", "not_before", "deadline",
                  "on_timeout", "action", "artifacts", "task", "kind"}
        if not isinstance(raw, dict) or set(raw) - fields:
            refuse(400, "consultation requires a policy object with known fields")
        asks = data.get("asks", [])
        if (payload.status not in (Status.open, Status.blocked) or len(asks) != 1
                or asks[0].get("phase")):
            refuse(400, "a consultation needs exactly one open/blocked, unphased ask")
        invited = set(asks[0].get("to") or [])
        policy = dict(raw)
        if policy.get("kind", "consultation-v1") != "consultation-v1":
            refuse(400, "unsupported consultation kind")
        policy["kind"] = "consultation-v1"
        for name, default in (("eligible", sorted(invited)), ("required", [])):
            seats = policy.get(name, default)
            if (not isinstance(seats, list) or len(seats) > 64
                    or any(not isinstance(s, str) or not s for s in seats)
                    or len(set(seats)) != len(seats)):
                refuse(400, f"consultation {name} must contain distinct seat IDs (at most 64)")
            policy[name] = sorted(seats)
        eligible, required = set(policy["eligible"]), set(policy["required"])
        if not eligible or not eligible <= invited or not required <= eligible or agent.id in eligible:
            refuse(400, "consultation eligible seats must be invited peers; required must be a subset of eligible")
        for seat in eligible:
            self.require_membership(channel, seat)
        minimum = policy.get("min_responses", max(1, len(required)))
        if type(minimum) is not int or not 0 <= minimum <= len(eligible):
            refuse(400, "min_responses must be an integer between zero and the eligible seat count")
        policy["min_responses"] = minimum
        for name in ("not_before", "deadline"):
            value = policy.get(name)
            if value is not None and (type(value) not in (int, float)
                                     or not math.isfinite(value) or value <= 0):
                refuse(400, f"consultation {name} must be a positive finite Unix timestamp")
            policy[name] = value
        if (policy["not_before"] is not None and policy["deadline"] is not None
                and policy["not_before"] > policy["deadline"]):
            refuse(400, "not_before cannot be later than deadline")
        if minimum == 0 and not required and policy["not_before"] is None:
            refuse(400, "a zero-response consultation needs a not_before collection window")
        policy.setdefault("on_timeout", "incomplete")
        if policy["on_timeout"] not in ("incomplete", "proceed"):
            refuse(400, "on_timeout must be incomplete or proceed")
        if policy["on_timeout"] == "proceed" and policy["deadline"] is None:
            refuse(400, "on_timeout=proceed requires a deadline")
        action = policy.get("action")
        if not isinstance(action, str) or not action.strip() or len(action) > 500:
            refuse(400, "consultation action must name the dependent decision (1..500 characters)")
        policy["action"] = action.strip()
        artifacts = policy.get("artifacts", [])
        if (not isinstance(artifacts, list) or len(artifacts) > 32
                or any(not isinstance(a, dict) or a.get("channel", channel) != channel for a in artifacts)):
            refuse(400, "consultation artifacts must be at most 32 current VFS citations in this channel")
        # Existing evidence validation binds real current bytes, not an
        # unchecked version string. The snapshot is stamped by the hub.
        snapshot = self._review_artifacts(agent, channel, artifacts) if artifacts else []
        policy["artifacts"] = [{"kind": "fs", "channel": c, "ref": ref, "sha256": sha}
                               for c, ref, sha in snapshot]
        if policy.get("task") is not None:
            ref = policy["task"]
            self._task_ref(agent, ref)
            if ref["channel"] != channel:
                refuse(400, "consultation task must be in this channel")
        return {**data, "consultation": policy}

    def consultation_state(self, agent, channel, message_id):
        with self.db.orchestration_lock:
            self.require_membership(channel, agent.id)
            root = self.db.get_message(message_id)
            if root is None or root.channel != channel:
                refuse(404, "consultation question not found")
            if root.retracted:
                if not is_consultation(self.db.get_message(message_id, redact=False)):
                    refuse(400, "message has no consultation policy")
                return {"channel": channel, "message_id": message_id, "status": "retracted",
                        "ready": False, "event": "retracted:" + message_id, "next_event_at": None}
            if not is_consultation(root):
                refuse(400, "message has no consultation policy")
            return self._consultation_state(root)

    def _consultation_state(self, root):
        from ..models import FS_PREFIX
        from .service import _fs_sha256
        policy = root.data["consultation"]
        ask_id = str(asks_of(root)[0]["id"])
        replies = self.db.replies_to(root.id)
        latest = {}
        conclusions = []
        for reply in sorted(replies, key=lambda m: m.seq):
            # Inspect only the typed markers of tombstones internally. Never
            # expose redacted text or resurrect an older answer/decision when
            # its replacement was withdrawn.
            markers = self.db.get_message(reply.id, redact=False) if reply.retracted else reply
            if (markers.data or {}).get("consultation_conclusion"):
                conclusions.append(reply)
            if reply.sender == root.sender or reply.kind != Kind.message:
                continue
            if ask_id in substantive_answers_of(markers):
                disposition = "answered"
            elif ask_id in declines_of(markers):
                disposition = "declined"
            else:
                continue  # Acknowledgements and ordinary conversation do not count.
            if reply.retracted:
                disposition = "withdrawn"
            latest[reply.sender] = {"sender": reply.sender, "id": reply.id, "seq": reply.seq,
                                    "disposition": disposition,
                                    "read": {"tool": "read_message", "arguments": {
                                        "channel": root.channel, "message_id": reply.id}}}
        responses = sorted(latest.values(), key=lambda r: r["sender"])
        answered = {r["sender"] for r in responses if r["disposition"] == "answered"}
        declined = {r["sender"] for r in responses if r["disposition"] == "declined"}
        withdrawn = {r["sender"] for r in responses if r["disposition"] == "withdrawn"}
        eligible, required = set(policy["eligible"]), set(policy["required"])
        missing = sorted(required - answered)
        count = len(eligible & answered)
        unavailable = sorted(s for s in eligible - answered if not self.db.is_member(root.channel, s))
        stale = []
        for artifact in policy["artifacts"]:
            path, _, version = artifact["ref"].rpartition("@")
            current = self.db.fs_get(root.channel, FS_PREFIX + path)
            if (current is None or current["deleted"] or current["version"] != int(version)
                    or _fs_sha256(content=current["value"].get("content"),
                                  content_b64=current["value"].get("content_b64")) != artifact["sha256"]):
                stale.append({"ref": artifact["ref"], "current_version": current["version"] if current else None})
        now = time.time()
        window = policy["not_before"] is None or now >= policy["not_before"]
        expired = policy["deadline"] is not None and now >= policy["deadline"]
        complete = not missing and count >= policy["min_responses"]
        unavailable_perspectives = declined | withdrawn | set(unavailable)
        impossible = bool(required & unavailable_perspectives) or len(eligible - unavailable_perspectives) < policy["min_responses"]
        if stale:
            collection = "stale"
        elif complete and window:
            collection = "ready"
        elif expired:
            collection = "timed_out" if policy["on_timeout"] == "proceed" and not missing else "incomplete"
        elif impossible:
            collection = "incomplete"
        else:
            collection = "collecting"
        # Time is not part of the evidence digest: an already reconciled
        # decision does not become stale just because its deadline passes.
        basis = digest([root.id, policy, responses, stale])
        conclusion = conclusions[-1] if conclusions else None
        recorded = ((conclusion.data or {}).get("consultation_conclusion", {}) if conclusion else {})
        status = collection
        if recorded.get("outcome") == "cancelled":
            status = "cancelled"
        elif conclusion:
            status = "decided" if recorded.get("basis") == basis else "needs_reconciliation"
        version = digest([basis, status, collection if status not in ("decided", "cancelled") else None,
                          unavailable if not complete and status not in ("decided", "cancelled") else None,
                          conclusion.id if conclusion else None])
        ready = collection in ("ready", "timed_out") and status not in ("decided", "cancelled")
        event = None if status in ("collecting", "decided", "cancelled") else f"{status}:{version}"
        future = [t for t in (policy["not_before"], policy["deadline"]) if t is not None and t > now]
        return {"channel": root.channel, "message_id": root.id, "ask_id": ask_id,
                "requester": root.sender, "action": policy["action"], "policy": policy,
                "status": status, "collection": collection, "ready": ready,
                "participation_complete": complete, "answered": sorted(answered & eligible),
                "missing_required": missing, "declined": sorted(declined & eligible), "withdrawn": sorted(withdrawn & eligible),
                "missing_eligible": sorted(eligible - answered),
                "unavailable": unavailable, "response_count": count, "responses": responses,
                "stale_artifacts": stale, "version": version, "basis": basis, "event": event,
                "next_event_at": min(future) if future and status not in ("decided", "cancelled") else None,
                "conclusion": conclusion.id if conclusion else None,
                "note": "Participation is not agreement. Read attributed responses and reconcile before deciding; external edits are not locked."}

    def task_consultations(self, channel, value):
        """Only task writers may declare these publication dependencies.

        A peer's question with a task pointer is traceability, not a new veto.
        Execution remains eligible while these particular decisions wait.
        """
        states = []
        for message_id in value.get("consultations", []):
            root = self.db.get_message(message_id)
            status = "unavailable"
            if root and root.channel == channel:
                status = "retracted" if root.retracted else self._consultation_state(root)["status"]
            states.append({"message_id": message_id, "status": status,
                           "read": {"tool": "get_consultation", "arguments": {
                               "channel": channel, "message_id": message_id}}})
        return states

    def conclude_consultation(self, agent, channel, message_id, outcome, expected_version, body):
        with self.db.orchestration_lock:
            state = self.consultation_state(agent, channel, message_id)
            if state["status"] == "retracted":
                refuse(409, "consultation was retracted")
            if agent.id != state["requester"] and not agent.operator and agent.id not in self.ruling_delegate_ids(channel):
                refuse(403, "only the requester, operator or ruling delegate may conclude this consultation")
            if outcome not in ("decided", "cancelled"):
                refuse(400, "consultation outcome must be decided or cancelled")
            if not isinstance(body, str) or len(body.strip()) < 12:
                refuse(400, "explain the decision and material concerns, or why the consultation is cancelled")
            if state["version"] != expected_version:
                refuse(409, "consultation changed; read get_consultation and reconcile the current responses")
            if outcome == "decided" and not state["ready"]:
                refuse(409, f"consultation is {state['status']}; the declared collection conditions do not permit a decision")
            if state["status"] == "cancelled":
                refuse(409, "consultation is cancelled; ask a new question to revise its policy or basis")
            record = {"outcome": outcome, "basis": state["basis"], "version": state["version"],
                      "responses": [r["id"] for r in state["responses"]],
                      "missing_required": state["missing_required"], "missing_eligible": state["missing_eligible"],
                      "collection": state["collection"]}
            # One ordinary authored conclusion is both the synthesis and the
            # durable receipt. The snapshot records input, not understanding.
            return self.post_message(agent, channel, PostMessage(
                title=f"Consultation {outcome}", body=body, status=Status.resolved,
                reply_to=message_id), _consultation_conclusion=record)

    def _consultation_sweep(self):
        from ..db import DuplicateMessage
        if self.hub_paused() is not None:
            return []
        fired = []
        with self.db.orchestration_lock:
            for root in self.db.consultation_messages():
                if not self.db.is_member(root.channel, root.sender) or self.channel_state(root.channel) in ("closed", "archived"):
                    continue
                state = self._consultation_state(root)
                if state["event"] is None:
                    continue
                receipt = "consultation-notified:" + root.id
                if self.db.meta_get(receipt) == state["event"]:
                    continue
                try:
                    self._post_system(root.channel,
                        f"Consultation {root.channel}#{root.seq}: {state['status']}. "
                        "Read get_consultation, reconcile current responses, and decide what the dependent action permits.",
                        to=[root.sender], status="fyi", urgency="next_turn",
                        data={"consultation_event": {"message_id": root.id, "status": state["status"],
                                                     "version": state["version"]}},
                        dedupe_key=f"consultation:{root.id}:{state['event']}")
                except DuplicateMessage as exc:
                    # A crash may happen after the ledger append but before
                    # notification. Replay that same message, not a new row.
                    self._wake(self.db.get_message(exc.message_id))
                self.db.meta_set(receipt, state["event"])
                fired.append(root.id)
        return fired
