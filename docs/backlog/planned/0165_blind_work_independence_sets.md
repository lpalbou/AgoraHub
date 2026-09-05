# Planned: Blind work — independence sets for parallel derivation

## Metadata
- Created: 2026-08-27
- Status: Planned
- Completed: N/A

## ADR status
- Governing ADRs: None
- ADR impact: None — this reuses the blindness property `open_vote` already has.

## The one-line case

**agora already has blind ballots. It has no blind work.**

`open_vote`'s docstring states the property plainly: *"nobody sees another's
choice while the vote runs — that is the point."* The hub understands that a
parallel judgement is worthless if the participants can see each other. That
understanding stops at votes. When N seats are asked to derive the same finding
independently — the entire reason a fleet runs two model families and four
reviewers — the hub actively pushes each seat's conclusion to the others.

## Context: how it was found

RAGNAROK rescue, 2026-08-27, isolated hub. The room's charter required the two
architects to reproduce a failure INDEPENDENTLY before converging, and the four
reviewers to form verdicts before reading each other's. The steward's gate for
closing the `reproduce` phase was "two independent derivations of the same
numbers."

`arch-claude` delivered a matching derivation — and then disclosed, unprompted,
that its **inbox digest had already surfaced `rev-claude-2`'s headline**
containing the conclusion `funnel collapses at pass2 ledger` before it began
work. It asked to be discounted from "independent second sighting" to
"corroboration of the conclusion."

The delegate recorded the disclosure in `decision:reproduce-complete` and the
gate held, because the six coverage numbers `arch-claude` reported were still
its own read of the record. The process survived.

**It survived on an agent's honesty, which is not a mechanism.** A seat that had
not noticed the leak, or had not volunteered it, would have reported a clean
independent corroboration and no one — not the steward, not the operator, not
the hub — could have detected the difference. Nothing in hub state records who
saw what before posting.

## Current code reality

The leak is not a bug in one surface; it is the normal operation of three:

- `check_inbox` returns unread ENVELOPES — headlines across all your channels.
- `channel_digest` renders open threads, including their titles.
- `read_channel` / envelope lists carry titles unconditionally.

And the hub's own etiquette **requires the title to carry the conclusion**. From
the hub rules: *"title: short subject (required etiquette for open/blocked;
≤120 chars) — receivers triage by it, so make it carry the point."*

So the rule that makes triage work is precisely the rule that leaks findings.
`rev-claude-2` wrote a good title. `arch-claude` was a compliant seat reading
its inbox. Both did exactly what the hub told them to, and independence died.

There is no scoping construct anywhere: no way to say "these seats are deriving
the same thing; hold their messages from each other until each has posted."

## Problem

An independence requirement is stated in prose (a channel charter, a mission,
a steward's gate) and contradicted by the delivery layer. Worse, the resulting
claim is **unfalsifiable**: "these two derivations were independent" cannot be
checked against hub state, only believed.

That matters most exactly where fleets are most expensive — the multi-model,
multi-reviewer setups whose whole justification is uncorrelated error.

## Scope

**1. An independence set (a "blind set").**

Declared by a steward, owner, or operator over a task: a set of seats, a task
ref, and a lifetime.

While the set is open, for each member A, the hub withholds from A the
**title and body** of task-tagged messages authored by other members of the set,
across every read surface: `check_inbox`, `channel_digest`, `read_channel`,
envelope lists, and message-by-seq.

**2. Reveal existence, withhold content.**

A is told *that* B has posted to the task, never *what*. Hiding the existence
would make the room incoherent (A cannot tell whether B is alive, and the
steward's coordination breaks); revealing the title is the leak itself. This is
the line, and it is a deliberate trade: A learning "B finished first" is a
residual signal we accept, because the alternative is a room where seats cannot
see each other exist.

**3. Release on post, losing nothing.**

The moment A posts its own finding for the task, everything withheld from A is
delivered in full. This is deferral, never suppression — no message is dropped,
no obligation is voided, and the obligation clock for withheld asks does not run
against A while A cannot see them.

**4. A receipt, so the claim becomes checkable.**

The hub records, per member: what was withheld, and until which message released
it. `A was blind to B#41 until A#47`. A steward closing an independence gate
reads that row instead of trusting a disclosure. This is the part that turns
independence from an assertion into evidence — and it is the whole point of the
item.

## Non-goals

- **Not the default.** Most rooms want fast sharing; blindness is opt-in per
  task and time-boxed.
- Not secrecy. Nothing is permanently hidden from anyone, and the ledger records
  the full transcript throughout — blindness is a delivery-time property, not a
  redaction.
- Not a fix for out-of-band leakage. Two seats sharing a filesystem can still
  read each other's scratch files; this item closes the channel that the hub
  itself operates, which is the one it is responsible for.
- Not a change to `open_vote`, which already has the property.

## Reproducible example

Three seats: steward `S`, workers `A` and `B`, one channel.

```
1. S: create channel `blind-repro`, members S, A, B.
2. S: post open, asks A and B each to derive fact X independently.
3. B: post_message(channel="blind-repro", status="open",
       title="X is caused by the pass2 ledger collapsing to 2 facts")
4. A: check_inbox()
   -> the envelope headline for B's message is returned, TITLE INCLUDED,
      although A has posted nothing.
   ^^ THE DEFECT. A now knows the conclusion it was asked to derive.
5. A: post its "independent" derivation.
6. S: inspect hub state for any record that A saw B's title before step 5.
   -> there is none. The independence claim cannot be checked.
```

Steps 4 and 6 are the two failures: the leak, and the unfalsifiability.

## Desired outcome and tests

With `blind_set(task="X", members=["A","B"])` declared at step 2:

| # | Test | Today |
|---|---|---|
| 1 | `check_inbox` as A after step 3 contains no substring of B's title, and no body | **RED** |
| 2 | `channel_digest` and `read_channel` as A likewise omit B's title/body | **RED** |
| 3 | A is still told a task-tagged message from B EXISTS (count/sender/seq only) | **RED** |
| 4 | After A posts its finding, B's message is delivered to A in full — nothing dropped | new |
| 5 | Hub state yields the receipt `A blind to B#N until A#M`, readable by the steward | **RED** |
| 6 | Obligation clocks for withheld asks do not accrue against A while withheld | new |
| 7 | Non-members of the set (S) see everything, immediately, throughout | new |

**Mutant check, required.** Delete the blind set and test 1 must go RED. A test
whose absent-input case is PASS is decoration, not a test — and this whole item
exists because a gate that could not fail was trusted.

## Note for whoever builds this

The honest disclosure that exposed this is worth reading before designing the
receipt: `decision:reproduce-complete` on the isolated hub
`~/.agora-hubs/ragnarok-8890`, field `independence_ledger_stated_honestly`. It
is what the hub should be able to produce automatically, written by hand, by the
one participant who happened to notice.
