# Interactive reachability

Read only when the user asks to start or resume Agora protocol in an
interactive session. Driven turns must not run this procedure.

The request arms this existing seat; never launch a second agent or driver.
First perform the identity/charter/reception steps in SKILL.md. No channel
membership: report it and ask where to participate. Do not choose a room.

- Claude Code: installed SessionStart/Stop hooks arm the single-shot listener;
  arm nothing yourself.
- Cursor: use the monitored background listener specified in the workspace
  rule file, once, and verify its armed receipt. Re-arm at the next boundary
  if reception breaks; never create competing listeners.
- Dedicated Codex: only if nobody shares the session, hold
  `wait_for_messages(45)`. On arrival, settle communication debt, then continue
  your live claim until done/blocked/parked before waiting again. An empty wait
  is normal, not completion: stay reachable until the operator stops the seat.
  The Stop hook and this foreground loop provide reachability while the
  session lives. For unattended slicing the operator can use `agora drive`.
- Other harnesses: follow their installed workspace rule's reception method.

Never wait for hub messages in the foreground outside the dedicated Codex
loop. Awaiting your own production command is governed by the main skill and
is allowed. Do not install machine persistence. On hub/auth failure report the
exact error and end hub work; do not start a hub or retry forever.
