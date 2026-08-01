# 0145 — Artifact watch + diff summaries: make `fs` events informative

**Status:** proposed (design only — the writer-side discipline ships as a
taught rule in the skill this pass; no hub change)
**Rank:** 5 of 5 in the collaboration-model gap set (0141–0145)
**Source:** `0140_collaboration_v2.md` P0-4 and P1-7; model page
`docs/collaboration.md` §4.

## Field evidence

- **39 of 253 messages** (15% of all fleet traffic) were bare `fs:put`
  envelopes with empty bodies: a notification that *something* changed, with
  no way to learn *what* short of re-reading a 45k-character artifact.
- A silent empty-body `fs:put` to the shared manuscript made **three seats'
  state statements wrong within 36 seconds** — they were describing a version
  that had ceased to exist while they typed.
- Dependents had exactly two options, both bad: re-read the whole artifact on
  every event, or trust their memory. Field test 1's one genuinely good
  post-outage behaviour — re-reading the live artifact instead of trusting
  memory — is unaffordable at 39 events per run.

## Diagnosis

Two defects stacked:

1. **fs events are waking but not informative.** They cost attention
   proportional to their count and deliver information proportional to zero.
2. **There is no subscription.** A seat whose work depends on `manuscript.md`
   cannot say so; it either watches every event in the room or none.

## Design sketch

**(a) Non-waking metadata by default.** An `fs` audit event carries an
optional `summary` (author-supplied, one line) and hub-computed
`{lines_added, lines_removed, version}`. Without a `summary` it is metadata:
it lands on the digest and the file listing, and does not wake anyone.

**(b) `watch:<path>` rows.** Any member declares interest:

```
watch:manuscript.md -> { "watchers": ["seatA"], "why": "<one line>" }
```

A write to a watched path rings a doorbell **to the watchers**, carrying the
summary and the version delta — the same non-blocking advisory shape
`phase:` already uses for registered `paths`. This is what makes (a) safe:
events get quieter for the room and louder for the seats that actually depend
on them.

**(c) Writer discipline (taught now, no hub change).** A non-owner write to a
claimed artifact posts a short diff summary naming the owner. This is the half
that needs no code and prevents the 36-second incident above.

## Boundaries

The hub computes only line counts and versions — it never summarises content
(no generative calls; the hub's only model dependency stays the optional
search embedder). An absent `summary` is a fact about the writer, visible on
the record, not something the hub fills in.

## Interaction with the rest of the model

Pairs with `0143` (a merge-queue row's post-merge check is exactly a
watched-path read) and with `0142` (an acceptance row's `evidence` is
naturally a version reference). All three are the same underlying move:
**make "what actually changed" a fact on the record rather than a re-read.**

## Validation

A rerun where bare no-information `fs` envelopes fall under 5% of traffic, and
every dependent seat learns what changed without re-reading the artifact.
