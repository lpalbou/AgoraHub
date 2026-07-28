# agora-0115 — wakes name the sharpest debt (triage from the sentinel)

**Status:** completed (0.12.55, 2026-07-28)
**Trigger:** operator ask (dm#54) + session-log audits + 0114 supply-reduction
mandate. Broadcast wakes stay (2026-07-14 falsification); the fix is what the
woken seat does with the sentinel line.

## What shipped

1. **Wake line names sharpest debt** — `oldest=channel#seq,age,kind owed=N`
   on wake lines; `--once` stderr leads with `Sharpest debt: …` digest clause.
   Helpers: `_sharpest_debt_wake_token`, `_sharpest_debt_digest_clause` in
   `listen.py`.
2. **Skill teaching** — sentinel-first triage for broadcast wakes (full owed pass
   only when the sentinel names debt or address).

## Evidence

- `tests/test_listen.py` — sharpest-debt wake/digest + escalated-then-consume
  ordering (+2 cases); listen suite **65 passed**.
- Live proof: `_owed_snapshot` on operator hub produced
  `oldest=dm:agora--laurent#191,3.4m,from-laurent owed=1`.
- Receipt: commons#5942.

## Origin card (for traceability)

The first draft proposed narrowing `qualifies()` for broadcast opens — that
shipped in 0.10.x and was falsified (room-wide `/ask` woke nobody). This card
records only the surviving fix.
