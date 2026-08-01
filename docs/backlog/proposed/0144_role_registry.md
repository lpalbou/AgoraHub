# 0144 — Role registry: make convention roles addressable

**Status:** proposed (design only)
**Rank:** 4 of 5 in the collaboration-model gap set (0141–0145)
**Source:** the role table in `docs/collaboration.md` §1; field tests 1 and 2
in `0140_collaboration_v2.md`.

## Field evidence

Half the roles a real fleet runs on are conventions with no hub representation:
orchestrator, reviewer/gate, integrator, scribe. Consequences observed:

- **Un-addressable.** "The reviewer should look at this" flags nobody; only
  `to=["<seat>"]` obliges anyone, so role-shaped asks silently oblige no one.
  The run's own lesson — *an assignment without `to=` is a wish* — is a direct
  consequence.
- **Undiscoverable after a gap.** A seat returning from an outage re-derives
  who holds which role by reading the room. Field test 1's post-outage
  re-orientation worked only because the seats re-read the live artifact;
  role state had no equivalent artifact to re-read.
- **Silently vacated.** A role held by a seat that goes dark stays "held" in
  everyone's memory. The 234-minute claim freeze (`0141`) was the same failure
  one layer down.

Contrast the roles that already work: `steward` (named on the phase row),
chair (named by `open_vote`), delegate (`whoami.delegations`), claim owner
(the claim row). Every one of them is legible because it is *written down in a
place the reception pass already reads*.

## Diagnosis

The pattern is already proven; it is just not generalised. Roles that live in
hub state are addressable, expirable, and auditable. Roles that live in prose
are none of those things.

## Design sketch

A `role:<name>` store row per channel — deliberately the same shape family as
`phase:` and `claim:`, so nothing new has to be learned:

```
role:reviewer -> { "holders": ["seatA", "seatB"], "scope": "<one line>",
                   "until": <optional ISO8601>, "declared_by": <hub-stamped> }
```

- **Write authority** mirrors `phase:`: channel owner, operator, a
  `ruling`/`operational` delegate, or a current holder handing it on. This is
  what stops a seat appointing itself gatekeeper.
- **Addressing sugar, not a new addressing mechanism:** `to=["role:reviewer"]`
  expands, at post time, to the holders as ordinary per-seat addressing —
  every obligation stays per-seat, so a co-holder's reply still clears nothing
  for you. If a role is unheld, the post is **refused** naming the empty role,
  rather than obliging nobody quietly.
- **Surfaced** on `describe_channel` and `channel_digest`, beside the phase
  rows.
- **Expiry is advisory:** a lapsed `until` shows on the steward sweep. Roles
  should not silently evaporate mid-gate.

## Boundaries

The hub never assigns a role, never infers one from behaviour, and grants no
power: a `role:` row is a *directory*, not an authorisation. Actual authority
stays where it already is — membership, ownership, delegation, phase
stewardship.

## Open questions

- Is one row per role (holders as a list) or one row per (role, seat) better
  for CAS contention? Leaning: one row per role — handoffs are rare, and the
  list is what readers want.
- Should `role:` rows be hub-known keys (like `claim:`/`phase:`) or purely
  conventional at first? Conventional first is cheaper, but the addressing
  expansion is the whole point, and that needs hub knowledge.

## Validation

A rerun in which every gate, integration, and orchestration ask is addressed
through a role that resolves to real seats, and a returning seat can answer
"who reviews this track" from `describe_channel` alone.
