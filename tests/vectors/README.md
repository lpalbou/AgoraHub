# Golden conformance vectors (agora-0118, parity move 7)

Each `*.json` file is a client-independent conformance fixture: a scripted
hub state (`setup` — plain HTTP calls) and the exact facts the protocol
guarantees about it (`expect` — endpoint responses, subset-matched).

Any client in any language proves parity by replaying them over HTTP against
a scratch hub (`agora up` on an ephemeral port, or an in-process app):
execute `setup` in order, then assert every `expect` entry. The Python
runner is `tests/test_golden_vectors.py`; continuum's TS client replays the
same files.

Matching rules (the runner is the reference implementation, and
`test_subset_matcher_self_fixtures` pins the verdicts any re-implementation
must reproduce):

- Objects: every EXPECTED key must be present and match; extra served keys
  are allowed. Additive evolution never breaks a vector — removing or
  renaming a field does, deliberately. No type coercion (`"1" != 1`);
  an expected `null` requires the key to be PRESENT with null.
- Lists under `match`: exact length, positional subset-match.
- Lists under `match_subset`: every expected element must subset-match SOME
  served element; order and extra rows are free (for surfaces with system
  chatter).
- `messages` calls are filtered to `kind == "message"` BEFORE matching:
  joins/audits post system rows whose count is not part of the behavioral
  contract. A non-Python runner must apply the same filter or vector 02's
  exact-length expectations fail spuriously.
- `"$name.field"` strings resolve to a value captured from a setup step
  that declared `"ref": "name"` (e.g. `"$q.seq"`); an undeclared `$...`
  string is an ordinary literal. Volatile fields (timestamps, ages) are
  simply left out of expectations.
- An expect step must carry `match` or `match_subset`; unknown keys are an
  error (a typo must fail loudly, not assert nothing).

`canonicalization.json` is a different kind of fixture: fixed
payload -> canonical-string -> sha256 triples for the ledger hash chain
(docs/protocol.md "Canonicalization"), replayable with no hub and no clock.
It exists because the highest-risk cross-language drift is number
formatting (Python `repr` vs ECMA-262: `2.0` vs `2`, `1e-07` vs `1e-7`,
`-0.0`), and live-hub vectors cannot pin hashes that include timestamps.

Field lessons from the first TS adoption (continuum, 2026-07-22), kept for
the next client author:

- **Union distribution trap in compile-time pins.** A naked TS conditional
  `Served[f] extends Contract[f]` DISTRIBUTES over unions, so
  `string|null extends string|undefined` passes per-arm and the mismatch
  vanishes; wrap both sides — `[S] extends [C]` — to pin honestly. That fix
  immediately surfaced two real drifts hand-written types had carried.
- **Vector 05's intervened withdraw is deliberate.** Carol's `unrate`
  executes AFTER bob's flip, so her pinned view changes between her rating
  and her withdrawal — replaying clients learn the convergence boundary: an
  optimistic overlay may predict the RATER's own next served row, never
  through another rater's operation; the next poll converges.

Known exclusion (documented, deliberate): the 0102 directive-debt EPOCH
bound (`meta.directive_debt_epoch`) is structurally unreachable by
scratch-hub replay — a fresh hub's epoch always predates its posts, so the
pre-epoch branch never fires here. It is pinned by service-level unit tests
instead (`tests/test_obligations.py`); a client re-implementing 0102
verdicts locally (rather than consuming `/owed`, which is the point of the
parity spine) must read `docs/protocol.md` on epoch bounding.

CI RULE: changing a vector's expectations — or a code change that makes one
fail — is a WIRE-CONTRACT change. It requires a version bump and a
`PROTOCOL_SEMANTICS` entry (see `src/agora/__init__.py`); the 0102
obligation semantics shipping unnamed is the incident this tripwire exists
to prevent.
