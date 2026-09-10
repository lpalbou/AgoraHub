"""Validate explicit reply dependencies without interpreting claim prose."""
import math


def validate_answer_waits(hub, agent, channel, value, prior, expect_version):
    from .service import HubError

    old = prior.get("waiting_for_answers")
    present = isinstance(value, dict) and value.get("waiting_for_answers") is not None
    if old is None and not present:
        if isinstance(value, dict) and value.get("wait_until") is not None:
            raise HubError(400, "wait_until requires waiting_for_answers")
        return value
    if not isinstance(value, dict):
        raise HubError(400, "answer-dependent claims require an object; clear waiting_for_answers explicitly")
    if expect_version is None:
        raise HubError(400, "answer-dependent claims require expect_version")
    effective = {**{k: prior[k] for k in (
        "owner", "status", "state", "done", "waiting_for_answers", "wait_until",
        "source", "source_message_id") if k in prior}, **value}
    owner = prior.get("owner") or effective.get("owner") or agent.id
    effective.setdefault("owner", owner)
    protected = ("owner", "waiting_for_answers", "wait_until", "source", "source_message_id")
    changed = any(effective.get(k) != prior.get(k) for k in protected)
    lifecycle = any(effective.get(k) != prior.get(k) for k in ("status", "state", "done"))
    if not agent.operator and agent.id != owner and (changed or lifecycle):
        raise HubError(403, "only the current claim owner or operator may change answer waits or lifecycle")
    waits = effective.get("waiting_for_answers")
    if waits is None:
        effective["wait_until"] = None
        return effective
    if not isinstance(waits, list) or not 1 <= len(waits) <= 64:
        raise HubError(400, "waiting_for_answers needs 1..64 exact message references")
    normalized = []
    seen = set()
    for item in waits:
        if (not isinstance(item, dict) or "message_id" not in item
                or set(item) - {"channel", "message_id", "after_seq"}):
            raise HubError(400, "answer wait needs {channel?, message_id, after_seq?}")
        target_channel = item.get("channel", channel)
        message_id = item["message_id"]
        after = item.get("after_seq", 0)
        if (not isinstance(target_channel, str) or not isinstance(message_id, str)
                or not message_id or type(after) is not int or after < 0):
            raise HubError(400, "answer wait requires a channel, message id and nonnegative integer after_seq")
        identity = (target_channel, message_id)
        if identity in seen:
            raise HubError(400, "duplicate answer wait")
        seen.add(identity)
        # Unchanged progress/closure is still possible after a dependency
        # becomes unavailable; a new declaration must point to visible state.
        if waits != old or effective.get("owner") != prior.get("owner"):
            hub.require_membership(target_channel, agent.id)
            hub.require_membership(target_channel, effective["owner"])
            message = hub.db.get_message(message_id)
            if message is None or message.channel != target_channel or message.retracted_at:
                raise HubError(404, "answer wait message is missing or retracted")
        normalized.append({"channel": target_channel, "message_id": message_id, "after_seq": after})
    deadline = effective.get("wait_until")
    if deadline is not None and (type(deadline) not in (int, float)
                                 or not math.isfinite(deadline) or deadline <= 0):
        raise HubError(400, "wait_until must be a positive finite Unix timestamp")
    effective["waiting_for_answers"] = sorted(normalized, key=lambda r: (r["channel"], r["message_id"]))
    return effective
