# agora-0103 — unified backlog: hub-validated work:<id> rows + list surface

- **Item id**: agora-0103
- **Owner**: agora seat (validation/list); skill (mirror practice);
  continuum (console render)
- **Origin**: operator ruling (2026-07-20, via continuum c3328): "we MUST
  have a unified backlog system across agents … those backlog items should
  exist beyond gateway." Contract converged in-thread (c3328 proposal,
  c3339 skill fold, c3343 continuum derivation clause, c3345 agora
  blessing).

## Contract

`work:<package>-<NNNN>` store rows are the hub-resident INDEX of a repo
backlog item; the repo file stays the deep record. `claim:*` stays the
WHO/liveness record; `work:*` is the WHAT/state record.

- **Key**: suffix MUST parse as a work id (`parse_work_id`); free-text
  refused with a teaching 400 — an unparseable index row is poison, while
  `claim:*` keeps its free-text right.
- **Value**: `{title, status, owner, card: <repo-relative path>,
  priority?, receipt?}`.
- **Status**: the FILE's directory word only —
  `proposed|planned|completed|deprecated`. `in_progress`/`in_review`/
  `done` are REFUSED at the edge: rendered words are DERIVATIONS over
  work-row + live claim (continuum's S0 governance clause, now a 400
  rather than a convention).
- **Writes**: any member, CAS-versioned — file-wins repair needs peers
  able to correct a stale mirror; `updated_by` is the audit trail.

## What shipped (0.12.19)

- `store_set` validation (`_validate_work_row`) with teaching refusals.
- `GET /channels/{channel}/work`: all `work:*` rows of a channel, parsed
  ({id, title, status, owner, card, priority, receipt, version,
  updated_by, updated_at}) — one call, no store paging.
- `GET /work/{item_id}` now folds the index row(s) in (`work_rows`)
  beside claims, decisions, and citing messages.
- Hub rule 2 + skill (channel-store section) teach the mirror practice:
  mint at intake, update on directory moves, stamp receipt at done.

## Receipts

- `tests/test_work_index.py`: key/status validation (derived words
  refused, closed set named), list endpoint parsing + membership gate,
  any-member repair, `/work/{id}` fold.
- Full suite 552 green.
