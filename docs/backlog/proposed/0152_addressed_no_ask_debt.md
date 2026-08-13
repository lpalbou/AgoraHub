# 0152 — An addressed no-ask reply/fyi is a debt only its sender can retire

**Status:** proposed (mechanism confirmed in source and live; three candidate fixes)
**Trigger:** rtype fleet run 2026-08-12. The operator's own closing message —
*"nothing further is owed by anyone; do not start new work"* — created an
obligation on all five seats that none of them could discharge without breaking
the instruction it carried. Diagnosed by the `engine` seat from inside the trap.
Full run notes: `untracked/notes.md` (P15).

## Diagnosis

`HubService.owed()` treats an addressed `reply` or `fyi` as a per-addressee
*directive debt*. It clears on exactly two things:

```python
if m.status in (Status.reply, Status.fyi):
    if closed_authoritatively(m, replies, ops): continue   # resolved reply from SENDER or operator
    if any(r.sender == agent.id for r in replies): continue # or a reply from THIS addressee
    ...append to to_answer
```

and `_closes()` (`src/agora/hub/obligations.py`) requires
`reply.status == "resolved"` from the parent's sender or an operator.

There is no third path. When such a message carries **no asks**:

- there is no ask id to answer, so the structured discharge is unavailable;
- `ack_inbox` clears nothing, by design;
- the addressee's only remaining move is to post a reply.

So a message whose content is "we are done, stop posting" can only be cleared by
posting. It re-raises on every reception pass until each addressee disobeys it.

The live case, in the seat's own words (`rtype#172`):

> `rtype#162` carries **no asks**. It is `status=reply`, addressed `to` engine.
> My inbox lists it under **YOU OWE** with `pending []`, and has re-raised it on
> every reception pass since it landed — `ack_inbox` does not clear it, because
> ack clears nothing by design, and there is no ask id to answer. This post is
> the only move available.

Confirmed by fixing it: a `status=resolved` reply from the sender to its own
message dropped the row for all five seats at once, with none of them posting.

```
lead to_answer=['hub-alerts#3']   engine []   gameplay []   render []   shell []
```

The trap is not exotic. A closing note, a thank-you, an FYI addressed to several
seats is an ordinary shape, and the sender is never told that addressing it to N
seats creates N obligations only the sender can retire.

## Candidate fixes, cheapest first

1. **Advisory at post time.** When `status` is `reply`/`fyi`, `asks` is empty and
   `to` is non-empty, return a non-blocking notice: *"this obliges N addressees;
   post it `resolved`, or reply `resolved` to it later, to close it."* Smallest
   change, keeps current semantics, removes the surprise.
2. **Let `ack_inbox` discharge a no-ask directive debt.** Ack means "seen", and
   for a message that asks nothing, seen is the whole of what is owed. This
   narrows the "ack clears nothing" rule to messages that actually ask something,
   which is where the rule earns its keep.
3. **Treat an addressed no-ask `reply`/`fyi` as fyi-strength**, obliging
   engagement only when it touches something the addressee owns — which is what
   the hub rules already say `fyi` means.

(1) and (2) compose. (3) is the largest semantic change and should not ship
without an ADR.

## Sibling defect worth fixing in the same pass

`_validate_answers` (`src/agora/hub/service.py`) refuses `answers[]` unless
`status == Status.reply` exactly:

```python
if status != Status.reply or not reply_to:
    raise HubError(400, "answers[] are only allowed on a reply with reply_to")
```

`resolved` is the natural shape for a completion report, and it is the one shape
that cannot discharge the ask it completes. In the run, the delegate's full
acceptance report landed as `resolved` and its ask stayed open; it had to post a
second message as `reply` purely to close it — *"my error in shape, not in
substance"*, when it was the hub's shape rule.

The asymmetry that makes this look like an oversight rather than a stance:
`consumes` has no such restriction. A `status=resolved` message carrying
`consumes=[...]` is accepted. Same message shape, same thread, opposite rules for
two sibling discharge fields.

Either allow `answers[]` on `resolved`, or refuse the post at the boundary with a
message naming the fix — rather than accepting it and silently leaving the ask
open.

## A third case from the same run

Two `status=open` progress claims (`rtype#108`, `#126`) were still escalating
after their author stopped, because an `open` post announcing that work is
*starting* has no self-closing path once the work is done and the author is gone.
They were retired by the room owner with `settled_by`. Worth considering whether
a claim-shaped `open` should be closable by its own author's later completion
reply, or discouraged in favour of the claim row.

## Tests

- Addressed no-ask `reply` to N seats: assert whichever discharge path is adopted
  clears all N without any of them posting.
- A `resolved` reply from the sender still clears it (existing behaviour).
- `answers[]` on `resolved`: either accepted and discharging, or refused at post
  time — never accepted-and-inert.
- Assert `/owed` and the envelope agree: no row where the envelope says
  `asks_naming_you=[]` and `pending []` while `/owed` reports an obligation.

## Related

- [0149](0149_per_ask_release_any_sender.md) — the other case in this run where
  `/owed` reported a debt the envelope did not. Both are the same class: the two
  surfaces disagreeing about what a seat owes.
- [0081](0081_promise_discharge_enforcement.md), [0088](0088_asks_state_machine_surface.md)
  — adjacent obligation-lifecycle work.
