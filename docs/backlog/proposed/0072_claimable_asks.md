# Proposed: claimable broadcast asks

## Metadata
- Created: 2026-07-12
- Status: Proposed (research-sourced; awaiting operator ruling)
- Completed: N/A

## ADR status
- Governing ADRs: ADR-0003
- ADR impact: None

## Context — research finding (messaging-systems adversary, 2026-07-12)
The residual attention tax after 0064/0066 is UNADDRESSED broadcast asks:
they must pin everyone (someone has to pick them up), so every member pays
until discharge. Queue-group semantics (NATS/MQTT shared subscriptions) and
the Contract-Net AWARD step both solve exactly this: one consumer takes the
task, the rest are released. Random broker assignment is wrong for
heterogeneous specialist seats — but CLAIMING is not assignment.

## What we want to do
`claimable=true` on an open/blocked broadcast: first member to claim (CAS —
one winner) retargets the obligation to itself; everyone else's envelope
demotes to fyi-grade and stickiness releases. Rot-proofing: unclaimed at
SLA escalates to ALL (the broadcast floor returns); a claim idle past SLA/2
releases back to broadcast. Surface: POST /messages/{id}/claim or a
store-key convention (claim:<seq> with expect_version=0 — zero new
endpoints; the inbox consults it). Prefer the store convention first.

## Promote when
Field data shows unaddressed broadcast asks are a measured residual pain
after 0064/0066 deploy, or a room adopts the convention socially and wants
it mechanical.

## Status note (2026-11-09, 0.18.0 audit)

The premise has changed. Under ADR-0006 (accepted 2026-09-09), a bare ask on
a room-wide message obliges nobody: anyone may answer it and it is nobody's
debt. So "unaddressed broadcast asks must pin everyone" no longer holds. The
remaining risk is the opposite one, an un-named ask that nobody picks up,
which is closer to `planned/0166`. This item is a candidate for deprecation at
the next hygiene pass; the maintainer decides.
