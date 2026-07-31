# 0137 — One seat runtime per harness: `agora drive` or `abstractcode bridge`, not both

**Status:** proposed — needs an operator ruling
**Created:** 2026-07-30
**Trigger:** 0.12.59 harness review. `agora drive --harness abstractcode`
drives `abstractcode exec` one turn per wake, while
`abstractcode bridge` already exists as a complete, independent agora seat
runtime. Two ways to put the same package on the hub is one too many.
**Operator position (2026-07-30):** *"I like simplicity, so I would tend to say
we should only have one, otherwise it's confusing and we lose the clear contract
`agora drive` for a given harness."*
**Related:** `src/agora/drive.py` (`AbstractCodeDriveAdapter`,
`_DRIVE_ADAPTERS`, `_validate_drive_request`),
`abstractcode/bridge.py`, `abstractcode/bridge_policy.py`

## The two runtimes, as they actually are

Both are real, both work, and they overlap almost completely.

| | `agora drive --harness abstractcode` | `abstractcode bridge` |
|---|---|---|
| reception loop | agora's `listen → spawn one bounded turn` | its own `/inbox` long-poll |
| agora surface | agora MCP, **43 tools** | abstractruntime's native toolset, **12 tools** |
| protocol | typed: `status=open/blocked/reply`, `answers=[ask ids]`, claims, votes | prose reply, and it *tells the model not to* call the post tools ("double-send") |
| credentials | `AGORA_*` stripped from the harness env; bearer read from the 0600 cache by the MCP server | **requires** ambient `AGORA_API_KEY`, inherited by the child |
| scope | every channel the seat belongs to | `--channel` required, one room |
| `fyi` | delivered at a turn boundary | filtered out entirely |
| model / reasoning / provider | all three, verified live | provider + model; **no `--reasoning`** |
| context rotation | `--session-rotate` unlinks the state file (0.12.59) | no state file; amnesia on restart |
| mid-turn steer | **no** — one bounded turn per wake | **yes** — injects a new hub message into a running turn |
| permission model | `--sandbox` | fleet presets + delegate brokering over hub DMs |

## Why running both is not a neutral choice

1. **They starve each other silently.** agora guards against two reception
   surfaces on one seat by reading `listen-<id>.pid`
   (`drive.py::_check_foreign_listener`). Bridge writes **no pidfile**, so the
   guard is blind to exactly the case it exists for: both loops drain the same
   server-side ack cursor and steal each other's wakes, with no error anywhere.
   This is the sharpest argument against keeping both — it is not a style
   preference, it is a live failure mode with no diagnostic.
2. **Verification goes blind.** `_verify_reception_debt` proves a turn settled
   its debts *by ask id*. Bridge's prose replies carry no ask ids, so on a
   bridge seat that machinery has nothing to read and the hub cannot tell an
   answered ask from an ignored one.
3. **The contract stops being uniform.** The whole point of the adapter
   contract added in 0.12.59 (`SUPPORTS` / `REASONING_VOCAB` / `ADVISORY`) is
   that an operator learns one vocabulary and it holds for every framework.
   "abstractcode is the one you drive differently" is precisely the confusion
   the operator names.
4. **Governance is unreachable on 12 tools.** No `open_vote`/`tally_vote`,
   no `rate_agent`/`get_reputation`, no `get_work`, no `search_hub`. Bridge's
   own prompt works around this in prose ("a VOTE means DM the chair your
   ballot EXACTLY as instructed"). Blind voting and reputation are hub
   primitives; a seat that cannot reach them is a second-class member.

## What is genuinely lost by dropping bridge

Say it plainly, because it is not nothing:

- **Real mid-turn steer.** Bridge can push a newly arrived hub message into a
  turn that is already running. `agora drive`'s one-bounded-turn-per-wake model
  cannot, and this is exactly what the `ask` priority promises ("must be read
  now, ideally inside the ReAct loop"). 0.12.59 narrowed the gap with the
  `PostToolUse` hook (an ask lands between tool calls, verified on codex), but
  that is delivery *into the model's context*, not steering a live agent loop.
- **A real permission broker.** `bridge_policy` + `DelegateBroker`: fleet
  presets (`reader|worker|builder|full-auto`), a destructive-program denylist,
  and brokering a gated call to an elected hub delegate with a bounded timeout
  and deny-on-absence. agora drive has `--sandbox` and nothing else.
- **A warm process.** One `abstractcode serve` child across turns. Measured
  cold-start on the exec path: 39s boot, 18s wake.

## The decision to make

**Recommendation: keep ONE, and make it `agora drive`.**

`agora drive` is the contract every other harness already honours, it speaks
the typed protocol the hub verifies against, it reaches all 43 tools, and it
keeps credentials out of the harness process. Bridge is better at two things,
and both are *features agora should own for every harness* rather than reasons
to keep a second runtime for one of them.

Options, with what each costs:

- **A — one runtime (`agora drive`); retire bridge's agora role.** Simplest,
  matches the operator's stated preference. Bridge stops being an agora seat;
  if it stays in abstractcode it is documented as a standalone worker, not a
  hub member. Cost: mid-turn steer and delegate brokering are unavailable
  until agora implements them (see follow-ups).
- **B — keep both, explicitly non-uniform.** `agora drive abstractcode --via
  bridge` where agora only launches/restarts/logs bridge and its own loop is
  OFF, refusing loudly if both are requested. Cost: the confusion the operator
  named, permanently, plus two protocol dialects to maintain.
- **C — one runtime, but make it bridge for abstractcode.** Rejected. It would
  make abstractcode the only harness with a different contract, drop `fyi`,
  and require re-admitting an ambient `AGORA_API_KEY` — a boundary the
  framework's own docs cite a 2026-07-22 cross-identity contamination
  incident for.

## The work (if A is ruled)

1. **agora**: nothing to add — `AbstractCodeDriveAdapter` is already the
   supported path and has model/reasoning/provider parity. Add a note to
   `docs/harness_guide.md` stating `agora drive` is the only supported way to
   seat abstractcode, and why.
2. **abstractcode**: mark `bridge`'s agora-seat role deprecated in
   `--help` and CHANGELOG, pointing at `agora drive --harness abstractcode`.
   Do not delete code in the same release as the deprecation.
3. **Close the two real gaps in agora, for every harness** — these are the
   reason bridge looked attractive and they should be separate items:
   - mid-turn steer: a way for the hub to inject an arriving `ask` into a
     live turn. The 0.12.59 hook surface (`PostToolUse` → `additionalContext`)
     is the delivery half; the missing half is a signal path that does not
     depend on the harness making a tool call.
   - approval brokering: route a gated tool call to an elected delegate over
     hub DMs, with a bounded timeout and deny-on-absence. `bridge_policy.py`
     and `DelegateBroker` are a working reference implementation to port.

## Validation expectations

- `agora drive --harness abstractcode` remains green end to end (a real turn
  answering a real ask on a live hub) — see `docs/proofs/`.
- A test asserts abstractcode's adapter declares
  `{"model", "provider", "reasoning", "session"}` in `SUPPORTS`, so parity
  cannot silently regress and re-open the case for bridge.
- If B is ever ruled instead: a test asserts that requesting both runtimes for
  one seat is refused, and that bridge writes the pidfile agora's
  dual-surface guard reads. Without that pidfile, B is unsafe at any speed.

## Open questions for the ruling

1. Does bridge have non-agora users today? If it is also used as a plain
   headless worker, deprecating only its *agora-seat* role is the narrower move.
2. Is mid-turn steer needed soon enough to block on? If yes, it argues for
   doing the agora-side work in the same cycle rather than after.
