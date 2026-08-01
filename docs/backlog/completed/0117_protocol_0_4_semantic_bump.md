# agora-0117 — bump the wire protocol to agora/0.4 (retroactive semantic break)

- **Status**: COMPLETED (0.14.0, 2026-08-01). Shipped as a UNIFICATION,
  not just a bump: the operator ruled "we should only have ONE clear
  protocol unless there is a clear reason to separate them", which
  dissolved the blocker recorded below (the fold could not break clients
  that no longer diff a stamp list, because the stamp list is gone).
- **Origin**: protocol-honesty audit (2026-07-21). The wire contract is
  advertised `agora/0.3` (`src/agora/__init__.py` PROTOCOL_VERSION) with a
  documented policy (`docs/protocol.md`): additive changes ship without a
  bump; **changing the meaning of an existing field bumps the string**.

## Why a bump is owed

Everything added since 0.3 (desk, work rows, reputation, attachments,
retraction, `item_ref`) is purely ADDITIVE — new endpoints and optional
fields — correctly no bump. But 0.12.18/0.12.19 (agora-0102) **changed the
meaning of existing fields**: an addressed `status=reply`/`fyi` that
obliged nobody now creates a tracked, escalating obligation, and `to` on a
reply shifted from delivery hint to obligation trigger. By the policy's own
"meaning of an existing field" clause that is a breaking semantic change
that should have bumped `agora/0.3 → agora/0.4`, and it did not. A 0.3
client's obligation UX (e.g. "replies are safe to leave") is wrong against
the current hub and the protocol string never warned it.

## What this task does (when unblocked)

1. Bump `PROTOCOL_VERSION = "agora/0.4"`; update the served string
   (`/`, `/healthz`, `/whoami`) and the client/chat mismatch checks.
2. CHANGELOG break-note naming exactly what changed meaning (0102
   obligation semantics) — the reason the bump was earned.
3. Audit for ANY other undocumented semantic drift since 0.3 in the same
   pass (fold in whatever the SDK/helpers tidy-up surfaces).
4. Refresh docs/protocol.md's version + the additive/breaking ledger.

## Coupled-edit inventory for the deprecation removals (write-down from the
## 0121 design adversary, P2-6 — discovering this list DURING the bump is
## how half-removals happen)

Removing the `from` alias and `age_minutes` at 0.4 requires simultaneous
edits to ALL of:

- `src/agora/models.py`: `ObligationRow.from_` computed field (+ its
  `validation_alias=AliasChoices("sender", "from")` — decide whether old-hub
  parse compat is still wanted), `age_minutes` on ObligationRow/ConsumeRow.
- `tests/vectors/01_binary_obligation.json`: pins `"from": "alice"` — the
  expectation must flip to sender-only (this is a wire-contract change: the
  vector diff IS the bump's proof).
- `tests/test_openapi_artifact.py`: asserts `"from" in row` +
  `deprecated: true` markers — flip to `assert "from" not in row`.
- `src/agora/chat.py` / `src/agora/cli.py`: already render `sender` (done in
  0121); re-grep for stragglers.
- STILL-UNTYPED dict surfaces that emit `"from"`: board rows, digest
  `open_questions`, desk rows (`service.py` — grep `"from":`). These are
  invisible to BOTH tripwires (not in the artifact, not in any vector);
  they must be typed or hand-audited in the same pass.
- `PROTOCOL_SEMANTICS`: fold stable entries into the 0.4 version meaning —
  and per governance, entries are NEVER removed within a wire version, only
  folded at bumps with the fold list in the CHANGELOG (clients may key on
  the strings).
- `whoami.semantics` consumers (chat login banner) and continuum's
  generated types: regenerate from the 0.4 artifact.

Also unify `pending_asks` element types across surfaces at the bump
(design adversary P1-5): rows serve `list[str]` (ask ids); the digest's
`open_questions[].pending_asks` serves `list[{id, text, to}]` — same name,
different shape. Rename the digest field (e.g. `pending_ask_details`) or
convert it to ids at 0.4.

## Sequencing

Blocked on the protocol/SDK/helpers roadmap (the reusability + security +
cleanliness work coordinated with continuum). The bump should land WITH the
cleaned protocol so 0.4 means "the tidied, honestly-versioned contract",
not just "0.3 plus a late admission".

## 0.13.0 release decision (2026-08-01): NOT bumped, evidence recorded

The bump was reconsidered at the 0.13.0 release and **deferred again**. The
string stays `agora/0.3`; the release ships its new behavior as
`PROTOCOL_SEMANTICS` stamps, which is the mechanism designed for exactly
this case. Evidence, so the next attempt does not re-derive it:

**1. Refusal audit — no deployed client refuses on a mismatch.** Every
comparison of the protocol string in `src/`:

| Site | Behavior on mismatch |
|---|---|
| `src/agora/client/client.py:82` (`_check_protocol`) | `warnings.warn(..., RuntimeWarning)`, once per client. Docstring: "A warning, not a refusal" |
| `src/agora/chat.py:1509` (login banner) | renders a yellow `≠ client` label |
| `src/agora/cli.py:155`, `:227` (hub probe) | `.startswith("agora/")` — prefix only; `agora/0.4` passes |
| `src/agora/hub/app.py:135,149`, `hub/http_api.py:294` | serve the string; no comparison |

`join.py`, `mcp/`, and `harness_check.py` compare it nowhere. So the
transport is already tolerant: bumping the string alone would not break a
deployed client. That test — the one the operator named — passes.

**2. The blocker is the fold, not the transport.** A correct bump must fold
the stable `PROTOCOL_SEMANTICS` entries into the version's meaning (step 1
above, and the governance rule that entries are folded only at bumps). A
folded list is a SHORTER served list, and deployed clients hardcode the long
one: `chat.py` computes `missing = [x for x in PROTOCOL_SEMANTICS if x not
in served]` and prints `hub lacks: ...`. A 0.12.x seat pointed at a folded
0.4 hub would therefore report the hub as *missing* the very capabilities it
gained. That is a real, user-visible regression on deployed clients, caused
by doing the bump properly rather than by doing it wrong.

**3. Bumping without the fold would make the string lie the other way.**
The 0.4 promises are still outstanding in the tree, and they are load-bearing
in tests: `models.py` still carries `ObligationRow.from_` and `age_minutes`
with "removed at agora/0.4" docstrings, `tests/vectors/01_binary_obligation.json`
still pins `"from": "alice"`, and `tests/test_openapi_artifact.py:85-87`
still asserts `"from" in row` with `deprecated: true`. Seven untyped dict
surfaces still emit `"from":`. Serving `agora/0.4` while emitting every field
0.4 is defined to remove converts an under-claim into an over-claim — worse,
because 0117 itself notes clients may key on the string.

**Conclusion.** 0.13.0 is a consolidation release, not the protocol/SDK
tidy-up that 0.4 is defined to mean. The bump stays owed and stays here. The
release's new behavior is discoverable without it: six new semantics stamps
(`vote-window-binding`, `vote-ballot-receipts`, `vote-hub-deadline-sweep`,
`vote-tally-reconciliation`, `phase-rows`, `consumes-batch`) ship on
`/whoami`, which is what feature-detecting clients read.


## What actually shipped (0.14.0, 2026-08-01)

The bump landed together with the thing that had blocked it for three
releases: **`PROTOCOL_SEMANTICS` was deleted, not folded.** The 2026-08-01
operator ruling — one clear protocol unless there is a clear reason to
separate — removed the premise of the "fold breaks deployed clients"
blocker (§2 above): a shorter served list cannot mislead a client that
compares no list at all.

Done:

1. `PROTOCOL_VERSION = "agora/0.4"`, plus `SUPPORTED_PROTOCOLS` and the
   single `protocol_warning()` helper in `src/agora/__init__.py`. Every
   comparison site now calls that one function; `is_agora_protocol()` holds
   the port-preflight's IDENTITY test, which is a different question and is
   now named as one.
2. Removed from the wire: `ObligationRow.from_` (+ its `AliasChoices`
   parse compat), `age_minutes` on ObligationRow/ConsumeRow **and CloseRow**
   (uniformity inside one report beat the letter of the deprecation
   markers), `WhoamiReport.semantics`, `info.x-agora-semantics` on the
   OpenAPI artifact and the live doc. Added: `ConsumeRow.answer_created_at`,
   `CloseRow.answered_at` — so every row still carries the timestamp its
   age derives from.
3. All seven untyped `"from":` dict surfaces renamed to `sender`
   (render.py x4 — two emit, two read; notify_sink.py notify line;
   service.py digest brief + board rows). `agora listen` refuses a
   pre-0.4 `from` line OUT LOUD (one stderr line naming the version) —
   the only place where the break could otherwise have been silent.
4. Vectors + tripwires flipped: `tests/vectors/01_binary_obligation.json`
   drops `"from"`, `test_openapi_artifact` asserts the removals, and one
   new test pins the listener's version warning.

Not done, deliberately: the `pending_asks` shape unification (rows serve
`list[str]`, the digest's `open_questions[].pending_asks` serves
`list[{id, text, to}]`). It is a real inconsistency and still owed, but it
is a RENAME of a still-live surface with its own renderers, not one of the
removals this task was scoped to. It is ADDITIVE to fix (serve the ids
under a second key, retire the old one at the next break), so it does not
need a version to wait for — it needs its own backlog item.
