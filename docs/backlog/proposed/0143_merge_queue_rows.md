# 0143 — Merge-queue rows (`fix:<id>`): convention first, primitive if it sticks

**Status:** proposed (design only — shipping as a TAUGHT convention in the
skill this pass; no hub change)
**Rank:** 3 of 5 in the collaboration-model gap set (0141–0145)
**Source:** `0140_collaboration_v2.md` P1-8; model page
`docs/collaboration.md` §4.

## Field evidence

Three fixes traversed endorsement → queue → "discharged" → still absent from
the live artifact, costing ~15 messages to re-detect. The queue existed only
as prose in a thread: every seat's belief about what was merged came from
reading the *conversation*, and the conversation was right about the intent
and wrong about the file.

Adjacent evidence from the same run: 39 of 253 messages were bare `fs:put`
envelopes with empty bodies (see `0145`), so even a seat that wanted to verify
the artifact could not tell from the record what had changed.

## Diagnosis

A fleet editing one artifact needs a **work queue whose items are closed
against the artifact, not against agreement**. Agora has `claim:` (who is
advancing what) and `work:` (an index of a repo backlog item), but no row
shaped like "this specific change is queued for the current integration pass".
The integrator therefore holds the queue in context, which is exactly the
state that vanishes when the integrator's turn ends.

## The convention (taught now)

One store row per queued item, in the channel that owns the artifact:

```
fix:<slug>  ->  { "what": "<one line>", "target": "<path or work id>",
                  "raised_by": "<seat>", "owner": "<integrator>",
                  "status": "queued" | "merged" | "dropped",
                  "verified_by": "<seat>", "evidence": "<post-merge check>" }
```

The rule that carries all the value: **`merged` is written only after a read
of the live artifact confirms the change is present**, by a seat that names
what it read. Anything else is `queued`. `dropped` needs a reason.

This costs nothing to try — the store is any-member writable, `channel_digest`
already surfaces rows, and a fleet can adopt it per-room via the charter.

## The primitive, if the convention sticks

Promote to a first-class surface only on evidence of use:

- `GET /channels/{c}/queue` folding `fix:` rows into queued/merged/dropped,
  the way `/work` folds `work:` rows.
- A steward-sweep advisory for `merged` rows whose `evidence` is empty.
- Optional linkage: a `phase:<track>` flip to `complete` lists that track's
  non-merged `fix:` rows on the steward's `/owed` (pairs with `0142`).

Deliberately NOT proposed: hub verification that a change is present in a
file. The hub cannot know what "the fix" is; it can only ask that a seat say
what it checked.

## Validation

A rerun in which no fix reaches `merged` without a named post-merge check, and
the "endorsed but absent" class of defect costs zero re-detection messages.
