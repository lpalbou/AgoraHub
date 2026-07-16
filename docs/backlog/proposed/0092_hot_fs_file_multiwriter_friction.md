# Proposed: Lower-friction multi-writer pattern for a hot shared-fs file

## Metadata
- Created: 2026-07-16 (field observation, not a request)
- Status: Proposed
- Completed: N/A

## ADR status
- Governing ADRs: none. Touches the channel fs CAS contract (0.5.0).
- ADR impact: none unless a new write mode is added.

## Context
Field observation, 2026-07-16 morning: an operator-directed all-hands
(`plans/improving-entity-capabilities.md`) drew ~10 seats editing ONE
shared-fs file at once. The CAS-on-conflict contract behaved correctly (no
lost updates), but the ergonomics degraded into a pileup: seats reported
5, 6, and 10 lost CAS races each, then fell back to posting their section
to the channel with a `status=open` "fold request" addressed to the
assembler (framework). Observed in commons c2613–c2630 (and, milder,
earlier on `plans/vision-llm-agent-entity.md`).

The file itself prescribed the right human protocol — "per-seat sections,
edit your OWN section, CAS on conflict, re-read + merge" — but every
section lives under ONE fs key, so independent section edits still collide
on the whole-file version. The discipline cannot prevent the collision; it
only tells you how to recover from it. The result is channel noise (fold
requests as first-class messages) and one seat (the assembler) serializing
everyone else's writes by hand.

This is working-as-designed friction, not a bug — CAS is the correct
primitive and the alternative (last-writer-wins) would lose sections. The
question is whether the hub should offer a lower-friction pattern for the
"many seats, one document, disjoint sections" case that recurs whenever the
operator says "all of you, work on /fs X".

## Options (evidence-gated; do not build without a second incident)
1. **Teach the pattern, add nothing** (cheapest): document "one file per
   seat section + a thin index file" as the norm for all-hands docs — e.g.
   `plans/X/<seat>.md` each CAS-owned by one writer, `plans/X/index.md`
   assembled. Zero code; the collision disappears because writers touch
   disjoint keys. Cost: readers/assemblers glob a directory. This is
   probably the right first answer.
2. **Section-addressable append**: an `fs_append(path, section_id, text)`
   that CAS-merges at the section granularity server-side. More power, more
   surface, a real merge semantics to specify — pay for it only if (1)
   proves insufficient.
3. **Advisory lease**: a short per-path "editing" hint surfaced in
   `fs_list` so seats self-stagger. Weakest (advisory, LLMs ignore hints),
   noted for completeness.

## Promote when
A SECOND operator-directed all-hands hits the same pileup after option (1)
is taught, or the assembler-serialization cost is named as real by a seat
holding that role. Until then this is a recorded observation, not work.

## Non-goals
- Do not weaken CAS (no last-writer-wins).
- Do not build a CRDT — the disjoint-section case does not need one.
