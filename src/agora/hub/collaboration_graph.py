"""Caller-visible social and work graph, projected from existing records.

Edges are declarations and recorded actions, not inferred expertise, agreement,
or authority. Message pagination never drops current task/claim relationships.
"""
from __future__ import annotations

from .obligations import asks_of, declines_of, substantive_answers_of
from .orchestration import refuse


def collaboration_graph(hub, agent, channel, since_seq=0, limit=200):
    hub.require_membership(channel, agent.id)
    if type(since_seq) is not int or since_seq < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        refuse(400, "graph needs since_seq >= 0 and limit between 1 and 1000")
    nodes, edges = {}, {}
    hidden = 0

    def node(identity, kind, **data):
        nodes[identity] = {**nodes.get(identity, {}), "id": identity, "kind": kind, **data}
        return identity

    def edge(source, relation, target, **data):
        edges[(source, relation, target)] = {"source": source, "relation": relation, "target": target, **data}

    def seat(identity):
        return node(f"seat:{identity}", "seat", seat=identity)

    def row_ref(c, key):
        nonlocal hidden
        if not hub.db.is_member(c, agent.id):
            hidden += 1
            return None
        return node(f"store:{c}/{key}", key.split(":", 1)[0], channel=c, key=key,
                    read={"tool": "store_get", "arguments": {"channel": c, "key": key}})

    def message_ref(message):
        nonlocal hidden
        if message is None or not hub.db.is_member(message.channel, agent.id):
            hidden += 1
            return None
        identity = f"message:{message.id}"
        return node(identity, nodes.get(identity, {}).get("kind", "message"), channel=message.channel, seq=message.seq,
                    title=message.title, retracted=bool(message.retracted),
                    read={"tool": "read_message", "arguments": {"channel": message.channel, "message_id": message.id}})

    def evidence(source, relation, refs, default_channel=channel):
        nonlocal hidden
        if not isinstance(refs, list):
            return
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            c = ref.get("channel", default_channel)
            if not hub.db.is_member(c, agent.id):
                hidden += 1
                continue
            kind, value = ref.get("kind"), ref.get("ref")
            if not isinstance(value, str):
                continue
            target = node(f"evidence:{kind}:{c}/{value}", "artifact" if kind == "fs" else "evidence",
                          channel=c, citation=ref)
            edge(source, relation, target)

    with hub.db.orchestration_lock:
        for subscription in hub.fs_subscriptions(agent, channel):
            target = node(f"vfs:{channel}/{subscription['path']}", "artifact",
                          channel=channel, path=subscription['path'],
                          read={"tool": "fs_read", "arguments": {"channel": channel, "path": subscription['path']}})
            edge(seat(subscription['agent_id']), "subscribes_to", target,
                 events=subscription['events'], urgency=subscription['urgency'],
                 processed_through_version=subscription['after_version'])
        members = hub.db.list_members(channel)
        for member in members:
            node(seat(member.agent_id), "seat", about=member.about, mission=member.mission, role=member.role)
        for note in hub.get_notes(agent):
            target = note.get("subject")
            if target in {m.agent_id for m in members}:
                edge(seat(agent.id), "expects_from", seat(target), note=note["note"], private=True)
        for entry in hub.db.store_keys(channel):
            key = entry["key"]
            if key.split(":", 1)[0] not in ("task", "claim", "plan", "finding", "decision", "phase"):
                continue
            row = hub.db.store_get(channel, key)
            if row is None or not isinstance(row.value, dict):
                continue
            value = row.value
            current = row_ref(channel, key)
            nodes[current].update(version=row.version, status=value.get("status", value.get("state")),
                                  title=value.get("title"), purpose=value.get("purpose"))
            for field in ("owner", "coordinator", "director", "requester", "steward"):
                if isinstance(value.get(field), str) and value[field]:
                    edge(seat(value[field]), field, current)
            source = hub._resolve_source(channel, value.get("source_message_id") or value.get("source"))
            if source is not None:
                edge(current, "responds_to", message_ref(source))
            for field, relation in (("parent", "contributes_to"), ("task", "implements")):
                ref = value.get(field)
                if isinstance(ref, dict) and "channel" in ref and "key" in ref:
                    target = row_ref(ref["channel"], ref["key"])
                    if target:
                        edge(current, relation, target)
            for ref in value.get("depends_on", []) if key.startswith("task:") else []:
                target = row_ref(ref["channel"], ref["key"])
                if target:
                    edge(current, "requires_acceptance", target)
            for message_id in value.get("consultations", []) if key.startswith("task:") else []:
                target = message_ref(hub.db.get_message(message_id))
                if target:
                    edge(current, "delivery_requires_decision", target)
            for ref in (value.get("waiting_for_answers") or []) if key.startswith("claim:") else []:
                msg = hub.db.get_message(ref["message_id"])
                target = message_ref(msg)
                if target:
                    edge(current, "awaits_decision" if ref.get("condition") == "decided" else "awaits_response", target)
            for ref in (value.get("waiting_for_artifacts") or []) if key.startswith("claim:") else []:
                evidence(current, "awaits_artifact", [{"kind": "fs", "channel": ref["channel"],
                    "ref": f"{ref['path']}@>={ref['min_version']}"}])
            evidence(current, "delivers", value.get("evidence") or [])
            if key.startswith("task:"):
                for review in hub._current_task_reviews(channel, key):
                    reviewed = message_ref(hub.db.get_message(review["message_id"]))
                    edge(reviewed, "reviews", current, verdict=review["verdict"],
                         reviewer=review["reviewer"], stale_task=review["task_version"] != row.version)
                    evidence(reviewed, "reviews_artifact", [{"kind": "fs", "channel": c, "ref": ref, "sha256": sha}
                             for c, ref, sha in review["artifacts"]])
            if value.get("report"):
                report = hub._resolve_source(channel, value["report"])
                target = message_ref(report)
                if target:
                    edge(current, "delivered_by_report", target)
        messages = hub.db.get_messages(channel, since_seq=since_seq, limit=limit + 1)
        for msg in messages[:limit]:
            current = message_ref(msg)
            edge(seat(msg.sender), "authored", current)
            if msg.retracted:
                continue
            if msg.reply_to:
                parent = hub.db.get_message(msg.reply_to)
                target = message_ref(parent)
                if target:
                    edge(current, "replies_to", target)
                    for aid in substantive_answers_of(msg):
                        question = node(f"ask:{parent.id}/{aid}", "question", message_id=parent.id, ask_id=aid)
                        edge(current, "answers", question)
                    for aid in declines_of(msg):
                        question = node(f"ask:{parent.id}/{aid}", "question", message_id=parent.id, ask_id=aid)
                        edge(current, "declines", question)
            for ask in asks_of(msg):
                question = node(f"ask:{msg.id}/{ask['id']}", "question", message_id=msg.id, text=ask["text"])
                edge(current, "asks", question)
                for who in ask.get("to") or []:
                    edge(question, "requests_input", seat(who))
            data = msg.data or {}
            if msg.sender == "hub" and msg.kind.value == "system":
                event = data.get("consultation_event")
                if isinstance(event, dict):
                    node(current, "trigger", event="consultation", status=event["status"])
                    target = message_ref(hub.db.get_message(event["message_id"]))
                    if target:
                        edge(current, "reconsiders", target)
                if isinstance(data.get("mission_changed"), dict):
                    node(current, "trigger", event="mission_changed")
                vfs = data.get('vfs_trigger')
                if isinstance(vfs, dict):
                    node(current, "trigger", event=vfs['event'], version=vfs['version'])
                    target = node(f"vfs:{channel}/{vfs['path']}", "artifact", channel=channel, path=vfs['path'])
                    edge(current, "reports_revision", target, version=vfs['version'])
                if event or data.get("mission_changed") or vfs:
                    for recipient in msg.to:
                        edge(current, "notifies", seat(recipient))
            evidence(current, "cites", data.get("evidence") or [])
            if data.get("task_review"):
                review = data["task_review"]
                target = row_ref(channel, review["task_key"])
                edge(current, "reviews", target, verdict=review["verdict"])
                evidence(current, "reviews_artifact", [{"kind": "fs", "channel": c, "ref": ref, "sha256": sha}
                         for c, ref, sha in review["artifacts"]])
            if data.get("consultation_conclusion") and msg.reply_to:
                edge(current, "concludes", message_ref(hub.db.get_message(msg.reply_to)),
                     outcome=data["consultation_conclusion"]["outcome"])
        # Always show current collective decisions even beyond the message page.
        for root in hub.db.consultation_messages(channel):
            current = message_ref(root)
            state = hub._consultation_state(root)
            nodes[current]["consultation"] = {k: state[k] for k in (
                "status", "ready", "missing_required", "response_count", "next_event_at")}
            action = node(f"action:{root.id}", "decision", action=state["action"], status=state["status"])
            edge(action, "requires_consultation", current)
            for who in state["policy"]["required"]:
                edge(current, "requires_perspective", seat(who))
            for who in state["policy"]["eligible"]:
                edge(current, "eligible_perspective", seat(who))
            if state["policy"].get("task"):
                ref = state["policy"]["task"]
                edge(action, "contributes_to", row_ref(ref["channel"], ref["key"]))
            evidence(current, "considers", state["policy"]["artifacts"])
        return {"channel": channel, "nodes": list(nodes.values()), "edges": list(edges.values()),
                "messages": {"since_seq": since_seq, "limit": limit,
                             "next_since_seq": messages[limit - 1].seq if len(messages) > limit else None},
                "unavailable_references": hidden,
                "note": "Declared relationships and recorded evidence only. Purpose is judged against the source request; membership, roles and private notes do not grant authority. Page messages for history."}
