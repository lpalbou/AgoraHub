# agora-0109 — mandatory hourly delegate digest (hub-owned timer)

- **Origin**: operator ruling dm:agora--laurent#42: "the report digest
  are not optional for a delegate, they are mandatory, i should not have
  to ask for them. make them hourly. they are also useful for the
  delegate to check on the progress and reprompt the agents that are not
  moving forward."
- **Owners**: agora (hub timer + missed-alarm) — **shipped**; framework
  (the delegate who must produce readable prose) — **follow-up**.

## Shipped (agora hub lane)

- **Unit 1:** `_report_digest_sweep()` in `dark_sweep()` — hourly
  `report:<delegate>` contract; desk-facts open ask to reporting
  delegate; MISSED-REPORT to operator DM when period elapses without
  reply; pauses while fleet dark (0110).
- **Unit 2:** `_gloss_channel` / `_gloss_agent` / `_format_desk_fact_line`
  — desk-facts render with who/what/where/age/one-action; embedded
  `DIGEST_PROSE_TEMPLATE` (#65 plain-register skeleton).
- **Unit 3:** `report_digest_snapshot()` on `/status`, `/admin/status`,
  and `agora status` (period age, replied, missed_alerted, overdue,
  paused).
- **Tests:** `test_report_digest_sweep_missed_then_satisfied`,
  `test_report_digest_paused_when_fleet_dark`,
  `test_render_desk_facts_readability_glosses`,
  `test_report_digest_snapshot_on_status`.

## Follow-up (framework lane)

The reporting delegate (`framework`, `reporting` power) must **reply**
to each hub desk-facts post (`hub-alerts`, ask id `digest`) with
#65-readable prose: what shipped, what's blocked, who to nudge (continue,
release, or re-check your gate). Missed replies trigger MISSED-REPORT in
the operator's DM — the mechanism is live; production adoption closes
the card end-to-end.

## Design notes (retained)

Split of labor: hub generates FACTS; delegate writes PROSE. Hourly cadence
pauses when the fleet is dark (0110) so the operator gets one FLEET DARK
not eight empty digest alarms.
