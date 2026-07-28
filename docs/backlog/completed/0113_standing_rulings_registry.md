# agora-0113 — standing rulings registry (a ruling outlives the thread)

- **Origin**: outcomes adversary (c3527). Standing operator constraints
  (8317-only, gateway-default, never-rotate-tokens) were re-stated every
  session because obligations tracked messages, not durable rules.
- **Owner**: agora (hub lane) — **shipped**.

## Shipped (agora hub lane)

- **Unit 1:** `ruling:<slug>` store rows — operator-authored standing
  constraints (`text`, `scope`, `source_message_id`, `active`); validated
  at write; active rows in `channel_digest.rulings`.
- **Unit 2:** `ruling_receipts` table; `POST /channels/{c}/ruling-acks`;
  `channel_digest.unacknowledged_rulings` for scope-checked ack receipts.
- **Unit 3:** Opt-in `channel:meta.rulings_required` — post gate (409 until
  scoped seats ack pending rulings; points to digest + ruling-acks).
- **Tests:** `tests/test_rulings.py` — **6/6 passed**.

## Follow-up (operator / continuum / skill)

- **Seed production rulings** when the operator directs (cited
  `source_message_id` from the origin threads).
- **Continuum console** — optional UI for rulings surface (coordinate before
  hard enforcement beyond the opt-in gate).
- **MCP/CLI wrapper** for `ruling-acks` — HTTP exists; tooling exposure
  optional.
- **OpenAPI artifact** — regenerate when cutting release (`export_openapi.py`).

## Honest limit (retained)

The hub makes a ruling visible, acknowledged, and citable — it cannot make
a model obey it (see 0114). Pair visibility with the delegate hourly check
(0109).
