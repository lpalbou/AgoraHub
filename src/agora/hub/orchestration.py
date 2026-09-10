"""Task assignments and bounded personal context, derived from existing state.

This layer never chooses a solution, accepts evidence, or grants authority.
"""
from __future__ import annotations

import json


def refuse(code, detail):
    from .service import HubError
    raise HubError(code, detail)


class OrchestrationMixin:
    def prepare_task_delivery(self, agent, channel, key):
        """Read-only, race-bounded facts needed to draft a task delivery.

        This is deliberately not a reservation, approval, or posting shortcut:
        the normal resolved-reply gate remains authoritative at send time.
        """
        with self.db.orchestration_lock:
            self.require_membership(channel, agent.id)
            row = self._task_ref(agent, {"channel": channel, "key": key})
            task = row.value
            source = self._resolve_source(channel, task.get("source"))
            if (source is None or source.retracted or source.channel != channel
                    or key != f"task:msg-{source.seq}"
                    or task.get("source") != f"{channel}#{source.seq}"):
                refuse(409, "task source is not a live canonical source; read get_task and store_get before delivery")
            if task.get("status") != "open":
                refuse(409, "only an open canonical task can prepare delivery")
            summary = self.finding_integration_summary(channel, key)
            if summary["pending"]:
                return {"task_version": row.version, "source_id": source.id,
                        "blockers": [{"key": finding, "read": {"tool": "store_get", "arguments": {
                            "channel": channel, "key": finding}}} for finding in summary["pending"]],
                        "note": "pending or stale typed findings block delivery preparation; no post arguments are issued"}
            refs = []
            for finding, value in self._typed_findings(channel, key):
                if value.get("disposition") not in ("incorporated", "merged"):
                    continue
                artifact = self._finding_artifact(channel, value.get("artifact"), current=True)
                refs.append({"finding": finding, "ref": f"{artifact['path']}@{artifact['version']}",
                             "sha256": artifact["sha256"]})
            evidence = {item["ref"]: {"kind": "fs", "ref": item["ref"],
                                       "sha256": item["sha256"]} for item in refs}
            review_evidence, review_blockers = self._prepare_review_evidence(
                agent, channel, key, row, {(channel, item["ref"]) for item in refs})
            if review_blockers:
                return {"task_version": row.version, "source_id": source.id,
                        "blockers": review_blockers,
                        "note": "current typed review is required; no post arguments are issued"}
            for item in review_evidence:
                evidence[(item["kind"], item.get("channel", channel), item["ref"])] = item
            # Review snapshots may include coverage/evidence artifacts beyond
            # the finding artifacts. Deduplicate their existing citations.
            prepared = {(item["kind"], item.get("channel", channel), item["ref"]): item
                        for item in evidence.values()}
            return {"task_version": row.version, "source_id": source.id,
                    "integrated_fsrefs": refs,
                    "post_message": {"channel": channel, "reply_to": source.id,
                                     "status": "resolved", "evidence": list(prepared.values())},
                    "note": "add truthful title/body, claim/plan proof and any review evidence not supplied; normal posting checks still apply"}

    def reply_state(self, agent, channel, message_id, after_seq=0):
        """Scheduling metadata only: no message bodies or read receipts."""
        from .obligations import declines_of, substantive_answers_of
        self.require_membership(channel, agent.id)
        root = self.db.get_message(message_id)
        if root is None or root.channel != channel:
            refuse(404, "reply dependency message not found")
        replies = self.db.replies_to(root.id)
        responses = []
        for reply in replies:
            if reply.retracted_at or reply.seq <= after_seq or reply.sender == root.sender:
                continue
            answers, declines = substantive_answers_of(reply), declines_of(reply)
            if answers or declines:
                responses.append({"id": reply.id, "seq": reply.seq, "sender": reply.sender,
                                  "answers": list(answers), "declines": list(declines)})
        responses.sort(key=lambda r: r["seq"])
        return {"channel": channel, "message_id": root.id, "seq": root.seq,
                "closed": self._closed_authoritatively(root, replies),
                "retracted": bool(root.retracted_at),
                "responses": responses[:64], "omitted": max(0, len(responses) - 64)}

    def _task_ref(self, agent, ref):
        if (not isinstance(ref, dict) or set(ref) != {"channel", "key"}
                or not all(isinstance(ref[k], str) for k in ref)
                or not ref["key"].startswith("task:")):
            refuse(400, "task reference needs exactly {channel, key: 'task:…'}")
        self.require_membership(ref["channel"], agent.id)
        row = self.db.store_get(ref["channel"], ref["key"])
        if row is None or not isinstance(row.value, dict):
            refuse(404, "referenced task does not exist")
        return row

    def _validate_task_graph(self, agent, channel, key, value, prior):
        """Called under the hub's task-write lock, including persistence."""
        for field in ("primary_channel", "director", "depends_on", "work_type"):
            if field not in value and field in prior:
                value[field] = prior[field]
        for field in ("work_type", "primary_channel"):
            if prior.get(field) is not None and value.get(field) != prior[field]:
                refuse(409, f"{field} is immutable once assigned")
        primary = value.get("primary_channel")
        if primary is not None:
            if key != "task:msg-" + value["source"].rsplit("#", 1)[1]:
                refuse(400, "a primary task uses its canonical task:msg-<source seq> key")
            # New task rooms have one canonical record. Older commissions
            # may retain multiple records/rooms until explicitly adopted.
            if primary != channel or channel.startswith("dm:") or channel == "commons":
                refuse(400, "primary_channel must be this task's dedicated shared channel")
            for entry in self.db.store_keys(channel):
                if entry["key"].startswith("task:") and entry["key"] != key:
                    other = self.db.store_get(channel, entry["key"])
                    if other and isinstance(other.value, dict) and other.value.get("primary_channel") == channel:
                        refuse(409, "this channel already has a primary task")
        for field in ("coordinator", "director"):
            seat = value.get(field)
            changed = seat != prior.get(field) or (primary and not prior.get("primary_channel"))
            if changed and seat is not None and (not isinstance(seat, str)
                    or not self.db.is_member(channel, seat)):
                refuse(400, f"task {field} must be a current member of {channel}")
        if primary and not value.get("coordinator"):
            refuse(400, "a primary task needs a coordinator (its manager)")
        if value.get("rooms", []) != prior.get("rooms", []):
            for room in value.get("rooms", []):
                self.require_membership(room, agent.id)
        work_type = value.get("work_type")
        if work_type is not None:
            if not isinstance(work_type, str) or not work_type.strip() or len(work_type) > 64:
                refuse(400, "work_type must be a nonempty string <=64 characters")
            if prior.get("work_type") is None and self.db.reputation_totals(channel):
                refuse(409, "cannot label preexisting reputation with a new work_type")
            if prior.get("work_type") not in (None, work_type):
                refuse(409, "work_type is immutable; historical work must not be relabeled")
        deps = value.get("depends_on", [])
        if not isinstance(deps, list) or len(deps) > 32:
            refuse(400, "depends_on must be at most 32 task references")
        seen = set()
        for ref in deps:
            self._task_ref(agent, ref)
            node = (ref["channel"], ref["key"])
            if node in seen:
                refuse(400, "duplicate task dependency")
            seen.add(node)
        target = (channel, key)
        pending = list(seen)
        visited = set()
        while pending:
            node = pending.pop()
            if node == target:
                refuse(409, "task dependencies must be acyclic")
            if node in visited:
                continue
            visited.add(node)
            if len(visited) > 4096:
                refuse(400, "dependency graph exceeds 4096 nodes")
            row = self.db.store_get(*node)
            if row and isinstance(row.value, dict):
                pending.extend((r["channel"], r["key"]) for r in row.value.get("depends_on", []))
        value["depends_on"] = deps

    def task_context(self, agent, channel, key):
        row = self._task_ref(agent, {"channel": channel, "key": key})
        value = row.value
        waiting = []
        for ref in value.get("depends_on", []):
            if not self.db.is_member(ref["channel"], agent.id):
                waiting.append({"reason": "dependency not visible; ask your manager"})
                continue
            dep = self.db.store_get(ref["channel"], ref["key"])
            if dep is None or not isinstance(dep.value, dict) or dep.value.get("status") != "accepted":
                waiting.append({**ref, "version": dep.version if dep else None,
                                "status": dep.value.get("status") if dep else "missing"})
        routes = {}
        for role, field in (("manager", "coordinator"), ("director", "director"), ("requester", "requester")):
            seat = value.get(field)
            routes[role] = seat if seat and self.db.is_member(channel, seat) else None
        return {"channel": channel, "key": key, "version": row.version,
                "source": value.get("source"), "title": value.get("title"),
                "status": value.get("status"), "primary_channel": value.get("primary_channel"),
                "work_type": value.get("work_type"), "routes": routes,
                "ready": not waiting and value.get("status") == "open",
                "waiting_on": waiting, "report": value.get("report"),
                "verdict": value.get("verdict"),
                "integration": self.finding_integration_summary(channel, key),
                "reviews": self.task_review_summary(channel, key),
                "proxy_available": self.proxy_allowed(agent.id, channel, value.get("requester"))}

    def route_task(self, agent, channel, key, role, expect_version, message):
        """Resolve a responsible function at send time; never grant its power."""
        with self.db.orchestration_lock:
            context = self.task_context(agent, channel, key)
            if context["version"] != expect_version:
                refuse(409, "task changed; read get_task before routing")
            if role not in ("manager", "director", "requester"):
                refuse(400, "role must be manager, director or requester")
            target = context["routes"].get(role)
            if not target:
                refuse(409, f"task has no reachable {role}; ask the requester to assign one")
            payload = message.model_dump(mode="json")
            asks = payload["asks"] if payload.get("asks") is not None else (payload.get("data") or {}).get("asks", [])
            if not isinstance(asks, list) or any(not isinstance(a, dict) for a in asks):
                refuse(400, "asks must be a list of objects")
            if payload.get("to") or any(a.get("to") for a in asks):
                refuse(400, "route_task assigns recipients; leave message and ask to fields empty")
            payload["to"] = [target]
            for ask in asks:
                ask["to"] = [target]
            payload["asks"] = asks or None
            if payload.get("data"):
                payload["data"].pop("asks", None)
            from ..models import PostMessage
            from pydantic import ValidationError
            try:
                normalized = PostMessage(**payload)
            except ValidationError:
                refuse(400, "invalid structured asks; each needs id and text")
            normalized, _ = self._apply_mention_addressing(agent, channel, normalized)
            recipients = set(normalized.to or [])
            for ask in normalized.asks or []:
                recipients.update(ask.to or [])
                if ask.assignee:
                    recipients.add(ask.assignee)
            if recipients != {target}:
                refuse(400, "route_task recipients must match the resolved role; remove other assignees or @mentions, or use post_message")
            return self.post_message(agent, channel, normalized)

    def briefing(self, agent):
        """Personal state, not the privileged operator desk or another inbox."""
        sections = {"tasks": [], "claims": [], "phases": [], "decisions": []}
        for channel in sorted(self.db.channels_of(agent.id)):
            tasks = []
            for entry in self.db.store_keys(channel):
                if entry["key"].startswith("task:"):
                    context = self.task_context(agent, channel, entry["key"])
                    if context["status"] != "accepted":
                        tasks.append(context)
            managed = any(agent.id in (t["routes"]["manager"], t["routes"]["director"])
                          for t in tasks) or self._delegation_reaches(agent.id, channel, ("reporting",))
            sections["tasks"].extend(tasks)
            for entry in self.db.store_keys(channel):
                kind = entry["key"].split(":", 1)[0]
                if kind not in ("claim", "phase", "gate"):
                    continue
                row = self.db.store_get(channel, entry["key"])
                if not row or not isinstance(row.value, dict):
                    continue
                v = row.value
                if kind == "claim" and (v.get("owner") == agent.id or managed) and not self._claim_done(v):
                    section, fields = "claims", ("owner", "status", "next_step", "needs", "task",
                                                 "waiting_for_answers", "waiting_for_artifacts", "wait_until")
                elif kind == "phase" and v.get("status") == "open":
                    section, fields = "phases", ("steward", "current", "next", "status")
                elif kind == "gate" and v.get("status") == "asked" and (managed or v.get("owner") == agent.id):
                    section, fields = "decisions", ("owner", "status", "q", "asked_by")
                else:
                    continue
                sections[section].append({"channel": channel, "key": entry["key"],
                                          "version": row.version,
                                          **{f: v[f] for f in fields if f in v}})
        assignments = sorted({role for task in sections["tasks"]
                              for role, seat in task["routes"].items() if seat == agent.id})
        sections["tasks"].sort(key=lambda task: (
            agent.id not in task["routes"].values(), task["channel"], task["key"]))
        owed = self.owed(agent).model_dump(mode="json")
        # Keep actionable identifiers, not full quoted messages or evidence.
        for field in ("to_answer", "to_consume", "to_close"):
            sections[field] = []
            for debt in owed[field]:
                row = {k: v for k, v in debt.items() if k in {
                    "channel", "seq", "message_id", "sender", "title", "pending_asks",
                    "asks_naming_you", "id", "thread_id", "answer_id", "answer_seq",
                    "answered_by", "your_asks"}}
                # Consumption rows identify the original question with `id`.
                # Their answer can be older than the seat's channel cursor;
                # reading the question does not fetch its later replies.
                target = row.get("answer_id") if field == "to_consume" else row.get("id")
                if target:
                    row["read"] = {"tool": "read_message", "arguments": {
                        "channel": row["channel"], "message_id": target}}
                sections[field].append(row)
        result = {"seat": agent.id, "assignments": assignments, "sections": {}, "omitted": {},
                  "lookup": "get_task(channel,key), store_get(channel,key), check_inbox; private notes: get_colleague_notes",
                  "authority": "Task assignments route work; whoami.delegations grants authority."}
        # One total budget, not a cap per field that permits unbounded nesting.
        for name in ("to_answer", "to_consume", "to_close", "decisions", "tasks", "claims", "phases"):
            rows = sections[name]
            kept = []
            result["sections"][name] = kept
            for row in rows[:12]:
                encoded = json.dumps(row, ensure_ascii=False)
                if len(encoded.encode()) > 1800:
                    row = {k: v for k, v in row.items() if k in
                           {"channel", "key", "version", "message_id", "seq", "id",
                            "answer_id", "answer_seq", "answered_by", "read"}}
                    row["detail"] = "read source; record exceeds briefing item budget"
                kept.append(row)
                if len(json.dumps(result, ensure_ascii=False).encode()) > 10500:
                    kept.pop()
                    break
            result["omitted"][name] = len(rows) - len(kept)
        return result

    def advisors(self, agent, work_type):
        """Visible same-type work evidence; no global trust or invented ranking."""
        rooms = []
        for channel in sorted(self.db.channels_of(agent.id)):
            if channel.startswith("dm:"):
                continue
            matches = [t for t in self.task_rows(channel)
                       if t.get("primary_channel") == channel and t.get("work_type") == work_type]
            if not matches:
                continue
            board = self.reputation_leaderboard(agent, channel)["leaderboard"]
            rooms.append({"channel": channel, "task": matches[0]["key"],
                          "work_type": work_type, "ratings": board[:12],
                          "omitted_ratings": max(0, len(board) - 12)})
        notes = [n for n in self.get_notes(agent) if work_type.lower() in n["note"].lower()]
        return {"work_type": work_type, "rooms": rooms[:8],
                "omitted_rooms": max(0, len(rooms) - 8),
                "private_notes": [{**n, "note": n["note"][:500]} for n in notes[:8]],
                "omitted_notes": max(0, len(notes) - 8),
                "interpretation": "No history is unknown, not poor performance. Ratings are judgments; inspect evidence before choosing advice. Notes match text, not inferred expertise. No authority or delivery depends on these scores."}
