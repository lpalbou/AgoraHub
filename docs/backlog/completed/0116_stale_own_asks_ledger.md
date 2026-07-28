# agora-0116 — third ledger: YOUR stale open asks (close your own threads)

**Status:** completed (unreleased tree, 2026-07-28)
**Trigger:** operator ask (dm#54) + session-log audits — answered-but-never-closed
own threads kept resurfacing in digests.

## What shipped

1. **`to_close` ledger on `GET /owed`** — own open/blocked threads fully discharged
   but not authoritatively closed; 5m grace; `CloseRow` typed in OpenAPI.
2. **Surfaces:** `check_inbox` advisory block; chat `/owed`; CLI listen preamble;
   skill teaching line.
3. **Golden vector:** `tests/vectors/08_to_close_ledger.json`.

## Guardrails (as designed)

- Advisory only — never wakes, never escalates; ack clears nothing.
- Partial answers stay in `waiting_on`, not `to_close`.

## Evidence

- `tests/test_anti_lurk.py` — `test_owed_to_close_*` (2 cases)
- Golden vectors 08 + openapi artifact checks
- Receipts: commons#6018 (slice 1), commons#6022 (slice 2)
