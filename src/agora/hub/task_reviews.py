"""Explicit task review decisions, derived from one immutable message ledger."""
from __future__ import annotations

from ..models import PostMessage, Status
from .orchestration import refuse


class TaskReviewsMixin:
    @staticmethod
    def _review_read(channel, review):
        return {"reviewer": review["reviewer"], "read": {"tool": "read_message",
                "arguments": {"channel": channel, "message_id": review["message_id"]}}}

    def _review_artifacts(self, agent, channel, artifacts):
        refs = self._validate_evidence(channel, artifacts, agent_id=agent.id)
        snapshot = []
        for item in refs:
            if item["kind"] != "fs":
                refuse(400, "typed review artifacts must be versioned VFS citations")
            cited = item.get("channel", channel)
            path, _, version = item["ref"].rpartition("@")
            current = self.fs_read(agent, cited, path)
            if current.version != int(version):
                refuse(409, "typed review artifact is stale; review the current version")
            snapshot.append((cited, item["ref"], current.sha256))
        if len(snapshot) != len(set(snapshot)):
            refuse(400, "typed review artifacts must be distinct")
        return sorted(snapshot)

    def _current_task_reviews(self, channel, key):
        result = []
        for event in self.db.latest_task_reviews(channel, key):
            review = event["review"]
            row = {**review, "message_id": event["id"], "seq": event["seq"],
                   "recorded_by": event["sender"]}
            if event["retracted"]:
                # Keep the last event as a tombstone, never resurrect a prior
                # approval or expose the retracted explanation/artifact set.
                row.update(verdict="withdraw", reason="", artifacts=[])
            result.append(row)
        return result

    def task_review_summary(self, channel, key):
        task = self.db.store_get(channel, key)
        version = task.version if task else None
        rows = []
        current = self._current_task_reviews(channel, key)
        for review in current:
            rows.append({"reviewer": review["reviewer"], "verdict": review["verdict"],
                         "task_version": review["task_version"],
                         "stale_task": review["task_version"] != version,
                         "recorded_by": review["recorded_by"],
                         "read": {"tool": "read_message", "arguments": {
                             "channel": channel, "message_id": review["message_id"]}}})
        return {"enabled": bool(current), "rows": rows[:64],
                "omitted": max(0, len(rows) - 64),
                "blockers": [r for r in rows if r["verdict"] == "request_changes"][:64],
                "note": "Only typed reviews are enforced; read current artifacts before approval."}

    def _validate_review_delivery(self, agent, channel, key, task, data):
        reviews = self._current_task_reviews(channel, key)
        if not reviews:
            return  # Legacy messages remain evidence, never inferred verdicts.
        if any(r["verdict"] == "request_changes" for r in reviews):
            refuse(409, "delivery is blocked by typed request_changes review; settle or explicitly withdraw each objection")
        fs = [item for item in (data or {}).get("evidence", []) if item.get("kind") == "fs"]
        snapshot = self._review_artifacts(agent, channel, fs)
        if not any(r["verdict"] == "approve" and r["reviewer"] != agent.id
                   and r["task_version"] == task.version
                   and sorted(map(tuple, r["artifacts"])) == snapshot for r in reviews):
            refuse(409, "delivery needs an independent typed approval for the current task and exact artifact set")

    def _prepare_review_evidence(self, agent, channel, key, task, required):
        """Return existing current approval proof or actionable blockers."""
        from .service import HubError
        reviews = self._current_task_reviews(channel, key)
        if not reviews:
            return [], []
        objections = [self._review_read(channel, r) for r in reviews if r["verdict"] == "request_changes"]
        if objections:
            return [], objections
        for review in reversed(reviews):
            if (review["verdict"] != "approve" or review["reviewer"] == agent.id
                    or review["task_version"] != task.version):
                continue
            snapshot = sorted(map(tuple, review["artifacts"]))
            if not required.issubset({(c, ref) for c, ref, _ in snapshot}):
                continue
            refs = [{"kind": "fs", "channel": c, "ref": ref} for c, ref, _ in snapshot]
            try:
                if self._review_artifacts(agent, channel, refs) != snapshot:
                    continue
            except HubError:
                continue
            return refs + [{"kind": "message", "ref": f"{channel}#{review['seq']}"}], []
        return [], [{"reason": "request an independent review_task approval for the current task and exact artifacts",
                     **self._review_read(channel, r)} for r in reviews]

    def review_task(self, agent, channel, key, verdict, artifacts, title, body,
                    reply_to=None, answers=None, consumes=None, reviewer=None,
                    reason="", expect_task_version=None):
        """Publish the review and authoritative decision in the SAME message.

        Latest event per task/reviewer wins. Only withdrawal can act for an
        unavailable reviewer, using existing task decision authority.
        """
        with self.db.orchestration_lock:
            row = self._task_ref(agent, {"channel": channel, "key": key})
            source = self._resolve_source(channel, row.value.get("source"))
            if (source is None or source.retracted or source.channel != channel
                    or key != f"task:msg-{source.seq}"
                    or row.value.get("source") != f"{channel}#{source.seq}"):
                refuse(409, "typed review requires a live canonical task source")
            if row.value.get("status") != "open":
                refuse(409, "typed review requires an open task")
            if expect_task_version is not None and expect_task_version != row.version:
                refuse(409, "task changed; read its current version before reviewing")
            if verdict not in ("approve", "request_changes", "withdraw"):
                refuse(400, "review verdict must be approve, request_changes or withdraw")
            if not isinstance(body, str) or not body.strip():
                refuse(400, "review needs a substantive body")
            reviewer = reviewer or agent.id
            if reviewer != agent.id:
                requester = row.value.get("requester", source.sender)
                authorized = (agent.operator or agent.id == requester
                              or agent.id in self.ruling_delegate_ids(channel)
                              or self.proxy_allowed(agent.id, channel, requester))
                if verdict != "withdraw" or not authorized:
                    refuse(403, "only task decision authority may withdraw another review; no one may approve as another reviewer")
            if verdict == "withdraw":
                if not isinstance(reason, str) or not reason.strip():
                    refuse(400, "withdraw needs a reason")
                if not any(r["reviewer"] == reviewer for r in self._current_task_reviews(channel, key)):
                    refuse(404, "no existing typed review to withdraw")
                if artifacts:
                    refuse(400, "withdraw does not review artifacts; pass an empty list")
                snapshot = []
            else:
                snapshot = self._review_artifacts(agent, channel, artifacts)
            if reply_to is not None:
                request = self._resolve_source(channel, reply_to)
                if request is None or request.channel != channel or request.retracted:
                    refuse(400, "review reply_to must name a live request in this channel")
                reply_to = request.id
            targets = [row.value.get("coordinator"), row.value.get("delivered_by")]
            to = sorted({seat for seat in targets if seat and seat != agent.id and self.db.is_member(channel, seat)})
            review = {"kind": "task-review-v1", "task_key": key,
                      "source": f"{channel}#{source.seq}", "task_version": row.version,
                      "reviewer": reviewer, "verdict": verdict,
                      "artifacts": snapshot, "reason": reason}
            evidence = [{"kind": "fs", "channel": c, "ref": ref} for c, ref, _ in snapshot]
            payload = PostMessage(title=title, body=body,
                                  status=Status.reply if reply_to else Status.fyi,
                                  reply_to=reply_to, answers=answers, consumes=consumes,
                                  to=to, data={"evidence": evidence} if evidence else None)
            return self.post_message(agent, channel, payload, _task_review=review)
