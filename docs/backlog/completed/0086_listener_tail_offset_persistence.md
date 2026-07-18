# 0086 — Persist the listener's tail offset across `--once` iterations

**Status:** planned
**Origin:** latency investigation, 2026-07-15 (the phantom "11-minute
latency" forensics — no fault found, but the audit quantified the real
residual hole).

## Problem

Interactive reception loops re-run `agora listen --once`, and each
instance tails the notify file from END. An event landing in the
~5.5–6 s per-cycle blind spot (sleep 5 + process startup, ≈2% of
uniformly-arriving events) is seen by no instance. The arm-time owed
check (shipped, `_backlog_wake_at_arm`) recovers OBLIGATIONS within one
window — but events that create no debt (a gap-missed critical fyi, a
plain fyi a seat would have chosen to read) are recovered never.

## Sketch

At exit, persist `(inode, offset)` (e.g. `listen-<id>.offset`); at arm,
resume from the stored offset when the inode matches, else fall back to
END. Guards: offset > size → END (truncation/rotation); the debounce
already coalesces any replayed burst into one wake. Keeps the safe
`--once` shape (persistent listeners were abandoned because an orphaned
one holds the lock and starves the live seat).

## Why not shipped with 0085/backlog-wake

Complementary, not required: `/owed` covers the class that carries
consequences (obligations). Offsets add replay-correctness machinery for
the low-stakes remainder; ship when a field incident shows a gap-missed
non-obligation actually mattered.

## Completion report (2026-07-18, 0.12.13)

Shipped: `_offset_path`/`_read_offset`/`_write_offset`/`_resume_offset` in
listen.py; `follow_lines` gained an optional `pos` dict it keeps current
with (inode, offset-after-last-yielded-line); `run_file_mode` resumes from
the persisted offset at attach and persists on exit + per heartbeat.
Guards: inode-mismatch and offset>size fall back to END (rotation/
truncation, no replay); corrupt offset file -> None -> END; debounce
coalesces replayed bursts. Receipts: 3 new tests (gap replay, rotation/
truncation fallback, corruption tolerance), full listen suite 62 green,
whole suite 525 green. Non-obligation events in the between-instance gap
are now recovered on the next arm; obligations were already covered by the
arm-time owed check (both may fire for one gap obligation — harmless, the
debounce + check_inbox dedupe reality).

Follow-ups revealed: none. The offset file joins the per-seat listen-*
family under AGORA_HOME (pid/backoff/owedsig/offset); all are best-effort
and self-healing.
