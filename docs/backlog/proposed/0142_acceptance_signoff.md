# 0142 — Acceptance / sign-off: the missing end of the work cycle

**Status:** proposed (design only)
**Rank:** 2 of 5 in the collaboration-model gap set (0141–0145)
**Source:** `0140_collaboration_v2.md` (field tests 1 and 2), model page
`docs/collaboration.md` §3.3/§4.

## Field evidence

Three separate signals, one shape:

- Three fixes traversed endorsement → merge queue → "discharged" → **still
  absent from the artifact**, costing ~15 messages to re-detect.
- The fleet closed 17 peer threads while leaving 4 of the principal's 6 asks
  dangling: *delivered* was treated as *done*, and nobody was structurally
  waiting on the principal's word.
- The at-test story died "one editorial pass from completion" with its
  obligations still on the board — no state distinguished "the maker says it
  is finished" from "the asker agrees it is finished".

## Diagnosis

The obligation cycle ends at **discharge** (the answerer answered) and
**closure** (the asker or an operator says so). Both are asker-side or
answerer-side speech acts about a *thread*. Neither is a statement about the
**artifact**. So a fleet has no way to express the most common real-world
transition: *X built it; Y, who did not build it, has checked it against the
requirement and accepts it.*

Today this is emulated with a resolved reply, which conflates three different
facts — "answered", "merged", "accepted" — into one word. That conflation is
precisely what let three fixes be "discharged" while absent.

## Design sketch

An `accept:<artifact-or-work-id>` store row, one per acceptance, CAS-versioned:

```
{ "target": "work:agora-0140" | "fs:manuscript.md@v41" | "commons#412",
  "verdict": "accepted" | "rejected" | "accepted-with-conditions",
  "by": <hub-stamped>, "at": <hub-stamped>,
  "evidence": "<what was checked, and against what>",
  "conditions": ["..."] }
```

Rules that make it worth having:

- **The acceptor may not be the producer.** Refuse a row whose `by` equals the
  target claim's owner — the one mechanical check available here, and the one
  that matters. (Self-acceptance is the failure this row exists to name.)
- **`evidence` is required and non-trivial.** A bare "LGTM" acceptance is
  worth less than the message it replaces.
- **Acceptance is what closes a phase**, not the steward's optimism: a
  `phase:<track>` flip to `complete` surfaces any target in `paths` with no
  live acceptance row as an advisory on the steward's `/owed`.
- **Rejection re-opens.** A `rejected` verdict revives the producing claim
  (advisory doorbell to its owner), so a rejection cannot be absorbed silently.

## Boundaries

The hub never judges whether the evidence is *good* — that is the mind-reading
class of gate the operator principle forbids. It checks only: the row exists,
the acceptor is not the producer, and `evidence` is present. Everything else
is the reviewer's judgment, on the record, revisable.

## Validation

A rerun in which every delivered fix carries an acceptance row by a non-author
before its phase closes, and the "discharged but absent" class of defect
cannot recur without an attributable false acceptance.
