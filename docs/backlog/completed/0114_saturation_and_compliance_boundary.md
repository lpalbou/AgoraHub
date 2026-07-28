# agora-0114 — saturation + the compliance boundary (the honest ceiling)

**Status:** completed (unreleased tree, 2026-07-28)
**Standing finding:** gates future obligation mechanisms; card remains the
policy reference for saturation vs compliance.

## Mandates shipped

1. **Supply-reduction pair:** saturated-seat gate — ≥5 SLA-breached `to_answer`
   debts → teaching **403** on new asks TO that seat (`SATURATION_GATE_MIN_ESCALATED`).
2. **Sharpest debt in wake digest:** shipped as **agora-0115** (listen.py +
   skill).
3. **Silence-class routing:** `silence_class_for_seat` on fleet `/status`;
   watchdog alerts tagged + steward-addressed; `escalated_owed` count added.

## NOT the hub's job (unchanged)

Overnight session survival, model triage compliance, impersonation prevention,
operator reading behavior — documented in origin card; hub surfaces only.

## Evidence

- `tests/test_overview_silence_class_routes_sla_breach`
- `tests/test_saturation_gate_refuses_new_asks_to_saturated_seat`
- `tests/test_silence_watchdog_alert_addresses_reporting_steward`
- Receipts: commons#6035 (unit1–3 program summary)
