# 0149 — Per-ask debt release must not depend on who sent the message

**Status:** proposed (root cause located, patch drafted, one-file change)
**Trigger:** rtype fleet run 2026-08-12 (five driven `claude` seats, 177
messages). Three of four worker seats had reception turns scored
`status=error reason=debt-remains` while `GET /owed` showed them
`asks_naming_you=[]`. Full run notes: `untracked/notes.md` (P8).

## Diagnosis

`HubService.owed()` releases an addressee from a structured message once it has
ENGAGED and no pending ask names it. That release is nested inside a branch that
only runs when the sender is an operator:

```python
if any(r.sender == agent.id for r in replies):
    if (m.sender in ops and (agent.id in m.to
                             or self._operator_delegate_debt(agent.id, m))):
        if (asks_of(m) and agent.id not in named_pending
                and not self._operator_delegate_debt(agent.id, m)):
            continue                      # released
    else:
        ...claim-source excusal only...
```

The `else` branch — every non-operator sender — has no per-ask release at all.
Its only escape is the claim-source excusal, which requires the seat's claim row
to cite the source message and did not apply.

**The shape that triggers it is the normal fleet shape.** A delegate fans work
out as ONE message carrying one ask per seat. Every seat that answers its own ask
keeps the `/owed` row until the LAST seat answers theirs. The hub then tells one
seat two different things: the envelope says `asks_naming_you=[]`, the `/owed`
row says it owes.

Measured, with `lead` (a delegate, not an operator) as sender of `rtype#10`:

```
shell     to_answer rtype#10  pending_asks=['1']  asks_naming_you=[]
gameplay  to_answer rtype#10  pending_asks=['1']  asks_naming_you=[]
render    to_answer rtype#10  pending_asks=['1']  asks_naming_you=[]
engine    to_answer rtype#10  pending_asks=['1']  asks_naming_you=['1']
```

Ask 1 was `engine`'s. `engine` was the slowest lane (largest slice), so the three
faster seats were error-scored and re-woken for the duration of its work.

The code comment above the release dates it 2026-08-11 (fund1) and describes this
exact defect — *"their per-ask row was the whole of their debt, and the envelope
already tells them `asks_naming_you=[]` — keeping the /owed row told one seat two
things at once, and the louder one re-woke it forever."* The fix was applied only
to the operator-sender path. `drive.py:382` already records the consequence from
an earlier run: *"fund4: 8 of 11 delegate turns failed `debt-remains`"*.

## The change

Hoist the release above the sender test in `HubService.owed()`
(`src/agora/hub/service.py`, the `to_answer` loop):

```python
if any(r.sender == agent.id for r in replies):
    # A STRUCTURED message releases an addressee who has ENGAGED and has no
    # pending ask naming them — WHOEVER sent it. In a driven fleet the
    # assigning message is a DELEGATE's, not the human's.
    if (asks_of(m) and agent.id not in named_pending
            and not self._operator_delegate_debt(agent.id, m)):
        continue
    if (m.sender in ops and (...)):      # unchanged below
```

Purely additive by case analysis. The release predicate is already False in every
case the operator branch treats differently: a reporting delegate carrying the
commission is excluded by `_operator_delegate_debt`, and an ask-less commission by
`asks_of(m)`. Only the delegate/peer-sender case changes.

## Tests

- Non-operator sender, N asks with distinct per-ask `to`, each addressee answers
  its own: every answered addressee drops off `/owed` immediately; the unanswered
  one stays.
- Ask-less commission from an operator still pins every addressee (the
  75-second-discharge protection).
- Reporting delegate still carries an operator commission after answering its own
  ask.
- Regression: the `asks_naming_you=[]` + row-present contradiction becomes
  unreachable — assert the two surfaces agree.

## Why it matters beyond tidiness

`debt-remains` is deliberately a diagnosis and not a penalty (`drive.py`
`_SEMANTIC_STAGES`), so no seat went deaf. But the false verdict feeds
[0150](0150_adopt_session_on_semantic_failure.md): a turn scored `error` never
persists its session, so each false failure also costs the seat its memory. The
two together account for most of the wasted turns in the run — `shell` 53 driven
turns and `render` 49 against the delegate's 29.

## Related

- [0150](0150_adopt_session_on_semantic_failure.md) — the multiplier.
- [0152](0152_addressed_no_ask_debt.md) — the other case where `/owed` reports a
  debt the envelope does not.
