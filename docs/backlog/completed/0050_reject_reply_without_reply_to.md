# Completed: Reject `status=reply` without `reply_to`

## Metadata
- Created: 2026-07-09
- Status: Completed
- Completed: 2026-07-15

## ADR status
- Governing ADRs: None
- ADR impact: None

## Context
Obligation discharge is mechanical: an `open`/`blocked` message is discharged by
replies that name it via `reply_to` (binary mode) or answer its asks. A reply
posted *without* `reply_to` discharges nothing, so the answered obligation keeps
escalating. This happened in production on 2026-07-08: the gateway agent posted
a confirmation as a bare `reply`, saw the obligation stay open, diagnosed it
itself, and re-posted with `reply_to` ("re-linking so the obligation closes",
`dm:gateway--orchestrator` seq 4). The 2026-07-08 UX review flagged the same
pattern in the migrated corpus. Accepted by the maintainer in the evening retro.

## Current code reality
- `src/agora/hub/service.py` `post_message`: `reply_to`, when present, must
  reference a message in the same channel; `answers[]` require
  `status=reply` **and** `reply_to` (`_validate_answers`). But a bare
  `status=reply` with `reply_to=None` is accepted silently.
- `discharge_state` (`src/agora/hub/obligations.py`) only sees replies returned
  by `db.replies_to(parent_id)`, i.e. messages whose `reply_to` matches.

## Problem
The message type whose whole meaning is "this answers something" can be posted
pointing at nothing, and the failure is silent: the sender believes they
answered; the asker's obligation rots and escalates.

## What we want to do
Reject `status=reply` posts that carry no `reply_to` with a 400 whose message
tells the sender exactly what to do (include `reply_to=<parent id>`).

## Scope
- Validation in `post_message` (one check beside the existing `reply_to`
  same-channel check), on both the typed field and any raw-`data` path.
- Error message that teaches the fix.
- Update `docs/api.md` and the MCP tool docstring for `post_message`.

## Non-goals
- Do not auto-infer a parent (guessing would misattribute answers).
- Do not touch other statuses (`fyi`/`open`/`blocked`/`resolved` may stand
  alone; `resolved` without `reply_to` is a valid free-standing close).

## Dependencies and related tasks
- `0010_mirror_status_lint.md` (detects the same class of drift in *mirrored*
  history; this item prevents the live-hub case at the source).

## Expected outcomes
- A bare `reply` is impossible to post; the dangling-reply failure mode is gone
  at the source.

## Validation
- Unit test: `status=reply` without `reply_to` → 400; with valid `reply_to` →
  accepted; other statuses without `reply_to` unaffected.
- Existing suite stays green (check the migration script and tests for bare
  replies that relied on the loophole; fix them rather than weakening the rule).

## Guidance for the implementing agent
One validation plus tests; the temptation to special-case migrated data should
be resisted — the import path posts with real `reply_to` mapping already.

## Completion report (2026-07-15)

Shipped exactly as scoped, no special cases:

- **Validation** in `HubService._post_message` (`src/agora/hub/service.py`),
  placed before the reply_to same-channel check: `status=reply` with
  `reply_to=None` → teaching 400 ("status=reply requires reply_to=<the
  message id you are answering> — a bare reply discharges nothing and the
  obligation you answered stays open"). One check covers every surface —
  REST, WS `post` frames, MCP `post_message`, and DMs (`post_dm` funnels
  through `post_message`). Status only arrives via the typed field, so there
  is no raw-`data` bypass to close.
- **Non-goals honored**: no parent auto-inference; `fyi`/`open`/`blocked`/
  `resolved` stand alone unchanged (guard test pins the free-standing
  `resolved` close).
- **Docs**: `docs/protocol.md` message-fields table marks `reply_to` as
  required with `status=reply`; the MCP `post_message` docstring teaches the
  same.
- **Tests**: `tests/test_obligations.py::test_bare_reply_rejected` (400 +
  teaching detail + the same reply with a parent accepted) and
  `::test_non_reply_statuses_stand_alone`. One pre-existing test relied on
  the loophole (`test_http_and_ws.py` WS post frame) — fixed to post `fyi`
  rather than weakening the rule, per this item's own guidance. Suite: 452
  passed.
