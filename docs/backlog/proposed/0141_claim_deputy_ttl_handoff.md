# 0141 — Claim deputy, TTL, and mandatory handoff

**Status:** proposed (design only — no hub change in this pass)
**Rank:** 1 of 5 in the collaboration-model gap set (0141–0145)
**Source:** `0140_collaboration_v2.md` P0-3, field test 1 (8-seat at-test,
2026-07-31); model page: `docs/collaboration.md` §8.

## Field evidence

A single claim owner went dark and froze the whole fleet for **234 minutes**
while five finished drop-ins sat unmerged. The seat that noticed and refused
to open a competing claim was **correct** under the rules — one live claim per
task, conflict means taken — which is exactly what makes this a protocol gap
rather than a discipline gap. There is no legitimate move available to a seat
that can see the stall.

The dark seat itself was not misbehaving either: forensics
(`docs/proofs/14-silence-incident-2026-07-31.txt`) show a provider rate-limit
outage. Claims survive their owner's session by design; nothing in the model
says what happens when the owner does not come back.

## Diagnosis

`claim:` rows encode ownership but not **succession**. The row answers "who is
advancing this"; it cannot answer "who advances it if that seat is gone", nor
"when does this ownership expire". Every other stalling mechanism in the hub
has a clock (obligations escalate, votes close, leases cap, delegations
expire). Claims are the one durable ownership record with no clock at all.

## Design sketch

Three optional fields on the claim value, none of them changing the CAS shape:

- `deputy: "<seat>"` — a named successor the owner nominates. Purely
  declarative until a TTL fires.
- `ttl_minutes: N` (owner-set, floored like `cadence_minutes`) — after N
  minutes without a row touch, the hub **releases** the claim: to `deputy` if
  one is named (row rewritten with `owner=deputy`, `handoff_from=<old owner>`),
  otherwise to open (`status: "released"`, version bumped, `owner` cleared).
  Either way the transition is a hub write, attributable and visible.
- `next_step` becomes **mandatory** for a row that declares a TTL or deputy:
  a claim that can change hands must say what the next hands should do. A
  release with an empty `next_step` is the same freeze with extra paperwork.

Release is announced the way phase writes are: a non-blocking doorbell to the
old owner, the deputy, and the channel's steward. Nothing is refused; nothing
is deleted; the released row keeps its full history.

## Why the TTL and not a "steal the claim" verb

A verb lets a impatient seat take work off a busy one, which converts a
coordination problem into a social one. A TTL is the owner's own declaration —
"if I go quiet for N minutes, hand this on" — which keeps the model's
attention-not-initiative line: the hub surfaces a debt the owner authored.

## Open questions

- Should TTL default to the channel SLA rather than being opt-in? Opt-in
  under-covers exactly the seat that dies unexpectedly; a default risks
  churning long, legitimately slow claims. Leaning: opt-in first, and make
  the steward sweep *report* claims older than the SLA with no TTL.
- Interaction with `0129` focus lease: a leased heads-down seat is
  deliberately silent, and its row touches stop. The lease must suppress TTL
  release for its duration (the lease is itself the liveness proof).

## Validation

Rerun the 8-seat exercise with any single seat killed mid-run: zero
integration stalls over 30 minutes, and the released work picked up by the
deputy without a competing claim ever being opened.
