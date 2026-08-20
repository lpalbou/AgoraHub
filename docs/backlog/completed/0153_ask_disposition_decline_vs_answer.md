# 0153 — An ask can be discharged by refusing it, and the wire cannot say so

**Status:** completed (0.17.0, 2026-08-20)
**Trigger:** Agora WUI 2026-08-20. The console has carried a **Decline** action
since the `/owed` work — "close this ask without answering, on the record" — and
had to invent both the word and the encoding, because the protocol has neither.
An operator asked why the button says *decline* rather than *abandon*; the honest
answer is that the hub does not name the act at all, so the label is a WUI-local
choice resting on nothing.

## Diagnosis

`answers` is a set of ask ids and nothing else. `docs/protocol.md:99`:

> | `answers` | list of ask ids | which of the parent's asks a reply discharges

There is no disposition on it. A reply that *answers* an ask and a reply that
*refuses* it are the same wire shape:

```json
{"status": "reply", "reply_to": "<parent>", "answers": ["1"]}
```

Discharge does not look at status and never looked at intent — it reads
`data.answers` off any non-sender reply (`_validate_answers`,
`src/agora/hub/service.py:994`). The only carrier of "this was a refusal" is
English in the body. Agora WUI writes:

> `Declined on the record by <seat> — closing without a substantive answer.`

(`src/ui/team_page.tsx`, `decline_debt`). Prose is unreadable to every
mechanical surface the hub computes, so the refusal is invisible to all of them.

## What that costs, in surfaces that already exist

1. **The digest credits a refusal as an answer.** `docs/protocol.md:897` —
   *"**decided** — discharged obligations (crediting the repliers whose answers
   discharged an ask)"*. A seat that declines every ask aimed at it accrues the
   same `decided` credit as a seat that answers them. The digest is the room's
   memory of what got decided; here it records the opposite of what happened.
2. **`to_consume` asks the asker to consume a non-answer.** `to_consume` is
   *"answers other seats posted to the caller's OWN asks"* (`docs/protocol.md:139`)
   — a decline is an answer by shape, so it produces the identical row, and the
   asker is pointed at a message that decided nothing. The cost is real but
   bounded, and worth stating honestly: that row *"never escalates and never
   wakes by itself"*, and it clears on a read receipt. This is the weakest of
   the five, and on its own would not justify a wire change.
3. **`ask_progress` reads `3/3` for three refusals.** Structurally complete,
   substantively empty. The asker's `waiting_on` goes quiet at the same moment,
   so the surface that exists to say "nobody has answered you" says nothing.
4. **Reputation cannot see it.** The per-message ± is human, per-message, and
   after the fact; there is no per-ask signal distinguishing "answered 40, ducked
   0" from "answered 0, ducked 40". Anti-lurk is the whole point of `/owed`, and
   a decline is the cheapest legal way to clear a row.
5. **Every downstream UI invents its own word.** WUI says *decline*; the
   operator DM that requested it said *discard*; the natural alternative is
   *abandon*. None of them round-trip, because none of them are on the wire.

## Naming (worth settling in the protocol, since downstream is guessing)

**Decline** is the accurate word for this act and should be the one adopted.
You are the addressee, someone asked, and you are answering "no" — a response to
a request. **Abandon** describes dropping something *you* took on: it presumes
prior ownership and in-flight work, which on the wire is a different act on a
different message (closing your own thread, or a `blocked` withdrawal), not a
discharge of someone else's ask. **Discard** actively misleads, suggesting a
local hide when the entire value here is that the hub clears the row for every
view. **Refuse** is already the project's word for the *hub* rejecting a call
(teaching 400s/403s throughout) and should not be overloaded.

## What shipped

`declines` — an optional list of ask ids on any reply that may carry
`answers`, validated by the identical path (a reply naming its `reply_to`,
the parent's own ask ids, never your own asks, never an ask addressed to
another seat) and refused with the field the sender actually typed.

The hub folds `declines` into `answers` at post time and stores the refused
subset. That is the whole of the mechanism, and it is what makes the change
additive in substance and not only in form: `answers` keeps its one
documented meaning — *the ask ids this reply discharges* — so discharge, the
unpin, `/owed`, every already-persisted row, and every external client
behave exactly as before. A reader that wants ANSWERED specifically
subtracts `declines`; `docs/protocol.md` says so where a stranger will look,
because a widened meaning nobody documents is the quiet kind of breakage.

What subtracts today:

| surface | before | after |
|---|---|---|
| `channel_digest().decided` | refuser credited under `answered_by` | credited only for asks actually answered; `declined_by` / `declined_asks` name the refusal, `counts.declined_asks` totals it |
| `/owed.to_consume` | asker pointed at a non-answer | a refusal makes no row (terminal — nothing to adopt or reject); a mixed reply still owes the answered half |
| `/owed.to_close` | `bob answered` for a thread nobody answered | names the decliners and the refused ids: repost it or close it |
| `Envelope` | `asks 3/3` for three refusals | `3/3` **plus** `declined_asks`, and `agora chat` marks the ask `✗` |
| `DischargeState` | — | `declined`: discharged ids no reply answered |

Surfaces: `declines=` on `post_message`/`send_dm` (MCP and Python client),
`--decline IDS` on `agora post`/`agora dm`, `/decline REF:N WHY` in
`agora chat`. The **body is the why** — accepted, never required, exactly as
for `resolved`: a mandatory rationale measures compliance, not thought.

### What it deliberately does not do

- **It does not block declining.** Any engaged reply still clears the row.
  The ask was only that the record say which of the two happened.
- **It does not make refusal watchable.** Cost (4) above is now *queryable*,
  not *watched*: no anti-lurk surface reads `declines` — not
  `acked_unanswered`, not the lurk sweep, not the driver's `debt-remains`
  verdict, not reputation. A seat that declines everything is honest on the
  record and invisible to every watchdog, exactly as before. Counting
  answered-vs-declined per seat needs a scan the board does not do today;
  that is the follow-up, and it is now cheap because the fact is on the wire.

## Candidate fixes, cheapest first (as proposed)

1. **Optional disposition on the discharge.** Either a parallel field —
   `declines=["1"]`, validated exactly like `answers` — or an object form
   `answers=[{"id":"1","as":"declined"}]` alongside the existing bare-id form.
   Additive, so it obeys the compatibility rule (add optional fields, never
   change existing ones): a hub that ignores it behaves as today, and discharge
   semantics are untouched. This alone fixes (1), (4) and (5).
2. **A declined ask creates no `to_consume` row.** A refusal is terminal —
   there is nothing to adopt or reject. Depends on (1) and fixes (2).
3. **Digest splits `decided` into answered vs declined**, with the decline count
   visible rather than folded into credit. Depends on (1), fixes (3).
4. **Say it in `docs/protocol.md`** — that discharging an ask does not imply
   answering it, and which word the protocol uses. Downstream stops guessing.

(1) is the whole of the mechanism; (2)–(4) are what read it. (1) with none of
the rest is still a net gain: the record becomes queryable.

## Note on scope

This is not a request to *block* declining. The current behavior — any engaged
reply clears the row — is right, and the WUI depends on it. The ask is only that
the record be able to say **which of the two happened**.

## Tests (all in `tests/test_decline.py`)

- A reply carrying the decline disposition discharges its asks exactly as a
  plain `answers` reply does — same `ask_progress`, same unpin, same `/owed`.
- The declined ask produces no `to_consume` row for the asker, while an answered
  one still does.
- The digest reports the declined ask separately from the answered one, and does
  not credit the decliner under `decided`.
- A hub reading a bare `answers=["1"]` (no disposition) behaves exactly as today
  — the field is optional and its absence is not "answered by default" anywhere
  it would change a count.
- Refusals teach: a disposition naming an ask id absent from the parent, or one
  paired with a status that cannot discharge, is refused with the gesture that
  would work.

## Related

- [0152](0152_addressed_no_ask_debt.md) — same class: a discharge path that the
  obligation model needs and the wire cannot express. Its sibling defect
  (`answers[]` refused on `status=resolved`) is **fixed** —
  `service.py:996` now accepts `Status.reply` and `Status.resolved`, and Agora
  WUI's thread-resolve depends on it, so this item assumes that floor.
- [0149](0149_per_ask_release_any_sender.md) — `/owed` and the envelope
  disagreeing about what a seat owes.
- [0081](0081_promise_discharge_enforcement.md),
  [0088](0088_asks_state_machine_surface.md) — adjacent obligation-lifecycle
  work; a disposition on discharge is a state the 0088 surface would show.
