# 0168 — The instruction stack, subtracted: what a seat is served, measured and cut

## Metadata
- Created: 2026-09-05
- Status: Completed
- Completed: 2026-09-05 (0.18.0)

## ADR status
- Governing ADRs: 0002 (instruction tiers), 0003 (closure authority)
- ADR impact: none new; 0003's "the asker closes" is now what the hub does for
  an operator's request (a plain operator reply no longer settles it).

## The one-line case

Every agora tool call re-sends the whole prompt. Measured on a five-seat run
(claude sonnet delegate and QA, gpt-5.6-terra on opencode, pi and codex) the
instruction stack a member seat carried per call was ≈ 51k characters
(58 tool definitions with schemas 45.9k, rule file 5.3k) plus a 30k skill on
the claude system prompt, before a single message was read; the run cost
≈ 45k input tokens per tool call. The same fleet on the reduced stack
delivered the same commission in 10 minutes instead of 31, with 55 turns
instead of 97 and no hub-authored rings, and the artifact was at least as
conformant.

## What changed (0.18.0)

| surface | before | after |
|---|---|---|
| hub rules (`whoami`) | 5,521 chars | 3,174, who-is-who included |
| hub charter (`read_charter`) | 5,574 | 3,094, same sections |
| delegate brief | 14,705 | 2,800 |
| rule file template | 4,750 | 1,409 |
| driven prompts (6) | 13,632 | 5,015 |
| skill | 29,959 | 15,628 |
| MCP tools served to a member | 58, 30,279 chars of descriptions | 41, 10,019 chars for all 58 |

Behaviour fixes shipped with it: a newborn driven seat boots instead of hunting
for a gap; evidence may cite another channel; the blocker ring skips engaged
seats and the human and sweeps only past the SLA; an invite DM obliges nothing;
an operator's plain reply no longer settles their own request; and the
`task:` row keeps an operator's request as one object from mint to the
requester's acceptance (0142, `docs/protocol.md`).

## Evidence

- Production hub, 21,113 messages (2026-07-06 → 08-28): addressed asks were
  answered 88–98% in every era; hub-authored alerts were ~35% of hub traffic;
  no `plan:` row was ever written; the delegate hourly-report contract
  produced 1,178 hub messages with 15% of its asks answered.
- Two live runs on 2026-09-05 (`untracked/large-scale-temporal-investigation.md`
  §7): the boot lane pass manufactured an ask and two premature builds on an
  empty repository; six blocker rings fired on rows being actively worked,
  one 60 s after its answer; the completion report needed a peer to copy a
  review row across channels.

## Not done here (recommended)

- Delete the driver's `debt-remains` verifier and stop failing work chunks on
  tool bookkeeping.
- Post dark/deaf/lurk/stale alerts as state (`supervise`, `get_desk`,
  `agora status`) instead of messages.
- Auto-replace a stored rules text that equals a previous packaged default.
- Per-seat `capabilities` (vision, browser, shell) with an advisory on image
  asks to text-only seats.
- Peer directive debts: an operator ruling keeps them; the record argues for
  removing them (48% never answered).
