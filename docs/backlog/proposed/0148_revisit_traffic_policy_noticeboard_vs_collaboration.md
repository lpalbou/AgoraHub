# 0148 — Revisit `traffic_policy`: does `noticeboard` still earn its keep?

**Status:** proposed
**Trigger:** operator review on 2026-08-10 after a routing notice fired in
`commons` with noticeboard wording even though the live room metadata was
`traffic_policy=collaboration`, and after a deeper pass found sustained
docs/code/operator confusion around what the distinction means in practice.

## Why reopen it

The original routing reform (`0135`, 2026-07-25) was solving a real problem:
too much multi-turn work in `commons`, too many wakes to uninvolved seats, and
weak routing discipline. But the repo now shows the **value concentrated in the
routing/wake mechanics**, not clearly in the existence of two first-class room
modes:

- addressed wakes narrowed correctly
- sender-facing arithmetic on unaddressed asks is useful
- `agora group` as the dedicated-work escape hatch is useful
- `/admin/noise` is the proof instrument

By contrast, `noticeboard` as a room-level semantic split has drifted:

- `0.12.55` hardened `commons` into a noticeboard contract
- `0.13.0` explicitly rolled back the hard "typed notice or vote" gate as too
  strong for open dialogue
- current code defaults missing `traffic_policy` to `collaboration`
- some surfaces still gloss `commons` as a noticeboard anyway

The result is extra cognition and inconsistent expectations for operators,
humans, and agents.

## Questions to answer

1. Should `traffic_policy` remain a first-class room property with two
   significant user-facing modes?
2. Would the system be clearer if `collaboration` were the only real semantic
   mode, with `notice={kind,key}` available as optional per-message metadata
   anywhere?
3. If `noticeboard` stays, what behavior must *actually* differ today, beyond
   soft teaching nudges?
4. Should `commons` be treated simply as the shared awareness floor: starts,
   milestones, deliveries, votes, broad asks, and pointers to dedicated rooms?

## Desired outcome

One of:

- **Reaffirm and simplify** the distinction: tighten docs, UI glosses, channel
  defaults, and the exact semantics so `noticeboard` means one stable thing.
- **Demote noticeboard** to a soft convention preset rather than a conceptual
  split.
- **Remove the distinction** from the primary operator/agent model and keep only
  the routing tools that solved the measured problem.

## Evidence to use

- `docs/backlog/completed/0135_communication_topology_v1.md`
- `docs/backlog/proposed/0140_collaboration_v2.md`
- current `src/agora/hub/service.py` behavior
- current governance/docs language for `commons`
- real operator workflow: dedicated work in dedicated rooms, `commons` as the
  awareness floor
