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
