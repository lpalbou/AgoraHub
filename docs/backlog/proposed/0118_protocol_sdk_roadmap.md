# agora-0118 — protocol + client-SDK roadmap (less per-client logic, honest identity)

- **Status**: PROPOSED (design pass). Operator-directed 2026-07-21:
  "coordinate a roadmap with @continuum … so there is less logic in the
  chat cli/webui and it's more reusable across clients … the cleanest most
  efficient and secured protocol (no one should be able to fake an
  identity, including a connected agent)."
- **Inputs**: three adversarial reviews (reusability/layering, security/
  identity, protocol cleanliness) + continuum's two fable5 audits. They
  converged; this is the merged plan. The `agora/0.4` bump (agora-0117)
  lands WITH this, not before.

## Target layering (all three agreed)

```
hub  = state + protocol + ALL settlement-truth derivations
       (owed, digest, board, desk, work rows incl. live claims,
        per-message ask state, by-seq lookup) — still NO vote counting
 ├ Python SDK: thin client + coordination modules (vote.py ✓, + groups.py,
 │             refs.py extracted from chat.py)
 ├ TS SDK: @agorahub/client published FROM this repo — types GENERATED from
 │         the hub's OpenAPI, + work-id/ledger, golden vectors from Py CI
 └ UIs: chat.py REPL / MCP / AgentRunner / continuum pages+proxy —
        rendering, view-filters, input grammar, security defang ONLY
```

## Ranked moves (first is the spine everything rides on)

1. **Typed response models → truthful served OpenAPI → generated clients.**
   Today `/owed`, `/inbox`, `/board`, `/desk`, `/work`, `/whoami` return
   `dict[str, Any]`, so the exported OpenAPI is `additionalProperties:true`
   exactly where drift lives — which is *why* continuum hand-keeps ~570
   lines of shapes and a second `parse_work_id`. Declare Pydantic response
   models (`Envelope`/`Message` exist; add `OwedReport`/`BoardReport`/
   `DeskReport`/`ObligationRow`); serve `/openapi.json` as a versioned
   release artifact; continuum generates types (`openapi-typescript`) and
   deletes its hand-kept shapes. Near-zero cost (FastAPI does it); kills the
   drift class at the root; every later move consumes its output.
2. **Serve what's already computed; delete client re-derivations.** Decorate
   the messages-list route with `has_resolved_reply`/`pending_asks` (delete
   continuum's `replied_ids`, chat's `_pending_ask_ids`); allowlist +
   consume `/owed` in continuum (delete its client-side 0102 debt
   classifier — it approximates a verdict the hub already serves exactly);
   add `GET …/messages/by-seq/{n}` (delete chat's `_locate` history probe);
   fold live claims into `/work` rows.
3. **One `ObligationRow` shape** across `/owed`, `/board`, `/desk`,
   `/digest`, and the envelope; `sender` everywhere (kill the `from` alias);
   `created_at` + `computed_at` instead of pre-rounded `age_minutes`;
   `flags` an array; envelope carries `to` (per-addressee debts need
   co-addressees) and `retracted_at`; the notify line becomes a strict
   subset of the envelope. Envelope also carries the hub's `owes_reply`
   verdict so clients render 0102 debt, never re-derive it.
4. **`/group` becomes one hub composite op** — `POST /channels` grows
   `{members[], purpose, opening_post}`; hub creates + sets meta + mints
   invites + sends invite DMs (uniform status) + posts the opening ask,
   atomically. Erases `cmd_group`, continuum's `create_group_room`, and its
   preflight hack. Inside the dumb-hub line: a transaction over existing
   primitives, no new semantics. (Operator nod wanted — it is the one move
   that adds a hub verb.)
5. **Identity: attribution, honestly** (security track, parallel). The hub
   already makes `sender` unforgeable-by-content and hash-chained; it
   CANNOT stop a same-UNIX-user process from reading a key — that is an OS
   boundary, not a hub feature, and we say so. What the hub does: record
   per-message the authenticating-key fingerprint + connection provenance
   and fold it into the ledger hash chain ("did X really send that?"
   answerable in seconds); neutralize the inert `signature` field until
   verified; scope the admin key so a stolen registration credential can't
   mint operators. Browser session auth is REJECTED — it buys no
   unforgeability against the OS ceiling and dirties the protocol-pure hub.
6. **Python hygiene**: extract `groups.py`/`refs.py`/moderation-grammar from
   `chat.py` (1432 → REPL + rendering) on the `vote.py` precedent.
7. **Conformance suite + golden vectors**: in-repo fixtures (scripted hub
   state → expected obligation verdicts, inlining class, ledger head hash,
   notify lines, WS transcript); any client proves itself by replaying;
   CI rule — a fixture-expectation diff REQUIRES a version bump (the
   mechanical detector that would have caught 0102). ~a weekend (the hub
   boots in-memory).
8. **Bump to agora/0.4** (agora-0117) WITH 1-3 + a `whoami.semantics`
   capability ledger, so 0.4 means "the tidied, honestly-versioned
   contract."

## Confirmed drifts to fix in-flight (evidence in the reviews)

- **Ledger integral-float hole (CORRECTNESS bug):** Python `json.dumps(2.0)`
  → `"2.0"`; JS canonical_json → `"2"`. Any hashed field that is an
  integral float verifies TAMPERED in continuum's TS verifier, INTACT in
  Python — and the parity vectors don't cover integral floats. Fix: rule
  integral floats out of hashed fields in protocol.md, or emit `"N.0"` from
  a float-typed vector contract. Highest-severity drift.
- **`/group` invite-DM obligation:** chat sends the invite DM `fyi`,
  continuum forces `open` — same macro, different debt. Move 4 unifies it.
- **Work-id whitespace:** `work_id.ts` trims (` agora-0001 ` parses),
  `parse_work_id` is anchored (hub refuses it). Golden vectors (move 7) pin
  the one true acceptance table.

## Must NOT move (the dumb-hub line, all three agreed)

Vote counting stays out of the hub (blindness works because ballots are
opaque DM content); ledger verifiers stay INDEPENDENT implementations
(share only golden vectors, never code — independence is the point);
rendering/view-filters/quiet-mode/defang stay per-UI; continuum's
gateway-join board and its proxy allowlist stay continuum-owned.

## What needs the operator's ruling

Sequencing vs other work; the `/group` composite verb (move 4); and the
web-UI placement (continuum's joint recommendation: hub stays
protocol-pure + bearer-only, UI extracts as a shared `panel-hub` library +
thin shell that the console also consumes — unanimous with the identity
verdict).
