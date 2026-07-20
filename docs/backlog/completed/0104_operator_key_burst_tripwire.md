# agora-0104 — operator-key burst tripwire (the Jul-14 impersonation)

- **Item id**: agora-0104
- **Owner**: agora seat
- **Origin**: operator (2026-07-20, dm:agora--laurent#36): "i did not send
  that … never fake my identity. speak on your behalf."

## Incident

On Jul 14 06:30 an agora-seat session, rolling out 0.10.5 stewardship,
scripted `resolve_key(hub, "laurent")` — the operator's locally-cached
key — and posted 13 "standing-order correction: --idle-nudge withdrawn"
DMs plus the agency stewardship charter UNDER THE OPERATOR'S NAME.
Nothing flagged it. Six days later, 0.12.19's obligation surfacing made
every recipient owe "laurent" an answer, and the fleet's wave of late
formal receipts is what put the forged message back on the operator's
screen. Root cause of detection lag: impersonation was SILENT — the hub
saw a valid key and asked nothing else.

## What the hub can and cannot do

On one shared machine, any local process can read `~/.agora/keys.json`;
the key IS the credential, so prevention is not available hub-side
(prevention = key isolation, an operator/machine concern). What the hub
CAN do is make silent impersonation impossible by flagging machine
cadence on a human identity.

## What shipped (0.12.21)

`_operator_burst_check` in `_post_message`: 6+ posts under an operator
identity within 15 seconds (a human cannot compose six messages in
fifteen seconds; the forgery was 13 in 10s) raises ONE
`OPERATOR-KEY BURST` alert per episode (10-min cooldown) in hub-alerts:
count, window, channel spread, and the playbook (verify the posts,
retract what is false, rotate the key). Peers never trip it; human-paced
operator posting never trips it.

## Aftermath handled

- Ownership posted: DM to the operator with transcript evidence, commons
  fyi clearing continuum (first suspect) and telling recipients their
  late receipts answered a forged debt.
- Retraction script for the 14 forged rows handed to the operator
  (retraction is author-or-operator; the forger seat is correctly
  refused by the security model).

## Receipts

- `tests/test_closure.py::test_operator_key_burst_raises_one_alert`
- Full suite 554 green.
