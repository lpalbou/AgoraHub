# 0151 — Classify a turn's lane explicitly, not by matching a prompt prefix

**Status:** proposed (root cause located; a correct fix is a small signature change)
**Trigger:** rtype fleet run 2026-08-12. Every work chunk the delegate seat ran
was graded as a reception boot and failed for not calling `check_inbox`. Full run
notes: `untracked/notes.md` (P12).

## Diagnosis

`Driver._work_chunk` (`src/agora/drive.py`) builds the prompt, then prepends a
supervision preamble for a delegate seat:

```python
sid    = self.work_session_id
prompt = WORK_PROMPT if sid else WORK_BOOT_PROMPT   # both start "AGORA WORK CHUNK"
if self._is_delegate_seat():
    prompt = SUPERVISE_PROMPT + prompt              # PREPEND
```

`SUPERVISE_PROMPT` begins *"You are a user's delegate."*, so after the prepend the
prompt no longer starts with `"AGORA WORK CHUNK"`. Four separate classifications
in the module are prefix matches on that literal:

| site | expression | effect when it misfires |
|---|---|---|
| `_prompt_kind` | `startswith("AGORA WORK CHUNK") → "work"`, else `"boot"` | chunk labelled `boot` |
| `ClaudeDriveAdapter.assess_turn` | `if kind in {"boot","wake"} and "check_inbox" not in successful` | **chunk FAILED** for skipping a reception pass |
| `_verify_reception_debt` | `if kind not in {"boot","wake"}: return` | chunk debt-verified as a reception turn |
| session lane selection | `lane = "work" if prompt.startswith(...) else "reception"` | rotation flushes the wrong lane |

A work chunk has no reason to call `check_inbox` — that is what makes it a work
chunk. So the delegate's chunk is judged against the reception contract and fails:

```
AGORA_DRIVE state=chunk agent=lead reason=continuable-work row=rtype/claim:rtype-delivery@6
AGORA_DRIVE event=turn_end status=error agent=lead kind=boot stage=mcp-use
            reason=incomplete-reception-pass rc=0
            detail="missing successful Agora MCP call(s): check_inbox"
```

Two lines about one turn, and the driver's own two classifications of it
disagree. Across 29 turns the delegate's flight recorder contains **no `kind=work`
entry at all** while `state=chunk` fired twice: the work lane is invisible for the
seat that uses it most.

`stage=mcp-use` is not in `_SEMANTIC_STAGES`, so this takes the transport path —
wake held, backoff, and the session dropped when the stage is `harness`. Live,
`lead` was pushed back to a cold `kind=boot` turn after holding a stable session
for 17 turns.

Only delegate seats are affected, because only they get the prepend — i.e. the
seat coordinating the fleet.

## The change

The caller always knows which lane it is spawning. Stop recovering a behavioural
classification from a string another feature is free to prepend to.

- Thread an explicit `kind` (`"boot" | "wake" | "work"`) from `_reception_turn`
  and `_work_chunk` through `_spawn_turn` into `assess_turn`,
  `_verify_reception_debt`, the flight-recorder rows, and the lane selection.
- Keep `_prompt_kind` only as a fallback for callers that genuinely have nothing
  but a prompt, and add a test asserting the two agree for every prompt constant.

A one-line stopgap (`"AGORA WORK CHUNK" in prompt` at all four sites) restores
correct behaviour today, but leaves the same trap for the next preamble.

## Tests

- A delegate seat's work chunk reports `kind=work`, is not asked for
  `check_inbox`, and does not run reception debt verification.
- A non-delegate work chunk is unchanged.
- Every prompt constant round-trips: the lane the caller declares equals the lane
  the classifier would infer.
- Regression: assert no `status=error reason=incomplete-reception-pass` on a turn
  whose lane is `work`.

## Related

- [0150](0150_adopt_session_on_semantic_failure.md) — the session loss this
  defect triggers through the transport path.
- The adapter contract's own principle (`DriveAdapter` docstring) is that
  differences between harnesses should be DATA an operator can see rather than
  scattered conditionals. The same argument applies to a turn's lane.
