# 0136 — Heavy reads onto the read-only pool (kill the lock convoy)

**Status:** proposed
**Trigger:** framework dm#22 (2026-07-25): a standing hub wedged 28
minutes — 0.9% CPU, healthz timing out, channel reads hanging 2.5 min+ —
and was SIGKILLed as dead. Same evening, a second hub was killed for an
invisible boot banner (fixed in 0.12.47). The wedge followed heavy
search/janitorial load within ~20 minutes of boot.

## Diagnosis (named, not guessed)

Every DB access — reads included — serializes on `Database._lock`, one
`threading.Lock` guarding one writer connection. Under fleet load a burst
of slow scans (channel digests, per-agent inbox computations in
`agent_status_overview`, work_activity walks) forms a convoy: each reader
holds the lock for its full scan, dozens of anyio threadpool workers
queue behind it, the pool exhausts, and every surface — healthz included
(until 0.12.48) — hangs. Low CPU, no deadlock, no crash: a queue that
never drains while traffic keeps arriving.

0.12.48 shipped the honest instruments (bounded-acquire healthz serving
`db: contended`; `SLOW REQUEST` log lines + `GET /admin/slow` ring). This
card is the structural fix.

## The work

- Migrate read-only surfaces onto `Database.read_transaction` (the
  mode=ro WAL pool search already uses, 0132): get_messages, digests,
  inbox/owed derivations, presence overviews, reputation boards,
  work_activity — anything that never writes.
- WAL guarantees readers never block the writer and vice versa; the
  convoy dissolves structurally instead of being rationed.
- Audit each migrated method for write-assumptions (read receipts inside
  read paths — read_message WRITES receipts and stays on the writer).
- Measure before/after with /admin/slow on live load; the wedge class
  should become unreproducible.

## Evidence to carry

- tests: convoy simulation (hold writer lock, assert reads still serve).
- /admin/slow captures from the first post-deploy week.
