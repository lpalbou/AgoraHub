"""Validate explicit reply dependencies without interpreting claim prose."""
import math


def validate_resume_waits(hub, value, prior):
    """A resumption must acknowledge old gates, not silently inherit them.

    Run on the original request, before linked-claim merging. Progress and
    closure still preserve omitted fields; an explicit gate remains binding.
    """
    from .service import HubError

    if not isinstance(value, dict) or not {"status", "state"}.intersection(value):
        return
    effective = {**prior, **value}
    if (not hub._claim_parked(prior) or hub._claim_parked(effective)
            or hub._claim_done(effective)):
        return
    omitted = [name for name in ("waiting_for_answers", "waiting_for_artifacts")
               if prior.get(name) is not None and name not in value]
    if not omitted:
        return
    details = "; ".join(f"{name}={str(prior[name])[:800]}" for name in omitted)
    raise HubError(400, "Resume must explicitly acknowledge existing waits: " + details
                   + ". Repeat or replace each field to keep waiting, or set it to null "
                     "if that dependency no longer applies. Use the current claim version. "
                     "VFS waits observe hub fs_* publication, not workspace files or attachments.")


def validate_answer_waits(hub, agent, channel, value, prior, expect_version):
    from .service import HubError
    from .consultations import is_consultation

    if isinstance(value, dict):
        effective = {**prior, **value}
        for field in ("status", "state"):
            head = str(effective.get(field) or "").strip().lower().split(" ", 1)[0].rstrip(".,;:")
            if head in ("waiting_for_answers", "waiting_for_artifacts") and not effective.get(head):
                raise HubError(400, f"status '{head}' does not declare a dependency; set the {head} field with exact references, or use parked with a reason")
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
                or set(item) - {"channel", "message_id", "after_seq", "condition"}):
            raise HubError(400, "answer wait needs {channel?, message_id, after_seq?, condition?: 'decided'}")
        target_channel = item.get("channel", channel)
        message_id = item["message_id"]
        after = item.get("after_seq", 0)
        condition = item.get("condition", "collected")
        if condition not in ("collected", "decided"):
            raise HubError(400, "answer wait condition must be collected or decided")
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
            if is_consultation(message) and after:
                raise HubError(400, "consultation waits use after_seq=0; ask a new question to change the consultation basis")
            if condition == "decided" and not is_consultation(message):
                raise HubError(400, "condition=decided requires a consultation question")
        normalized.append({"channel": target_channel, "message_id": message_id, "after_seq": after,
                           **({"condition": "decided"} if condition == "decided" else {})})
    deadline = effective.get("wait_until")
    if deadline is not None and (type(deadline) not in (int, float)
                                 or not math.isfinite(deadline) or deadline <= 0):
        raise HubError(400, "wait_until must be a positive finite Unix timestamp")
    effective["waiting_for_answers"] = sorted(normalized, key=lambda r: (r["channel"], r["message_id"]))
    return effective
