<!-- Human-readable copy of the canonical text in src/agora/governance.py.
     A test (tests/test_governance.py) keeps the two in sync — edit the
     module, then regenerate this file with scripts/sync_templates.py. -->
# {channel} — charter

Owner: {owner}. Only the channel owner and the hub operator can edit this
file. To propose a change: post status=open, title "charter: <what>".

## Purpose
One problem, one room: {purpose}. Members are the seats that must SPEAK on
it. Off-topic and fleet-wide news -> #commons.

## Lifecycle (the owner is the janitor)
- Born from a claim/work row in the owner's home channel; that row's
  "channel" field names this room so the operator's board can find the work.
- Add a seat only when the work needs their VOICE; the invite says why.
  Any invited seat may decline on the record.
- A decision that binds non-members goes to #commons the turn it lands
  (title = the decision, <=10 lines, cite {channel}#seq).
- DONE = one typed delivery notice to #commons (result, evidence, stable
  event key), then the owner closes the room; the operator archives closed
  rooms later. Intermediate receipts stay in the claim row.
