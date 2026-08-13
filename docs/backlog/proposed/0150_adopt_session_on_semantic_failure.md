# 0150 — A semantically-failed turn must keep its session, or the seat cold-boots forever

**Status:** proposed (root cause located, one-block change)
**Trigger:** rtype fleet run 2026-08-12. Four of five driven seats ran the whole
first phase with no session continuity, re-deriving state every wake and
re-announcing work they had already reported done. Full run notes:
`untracked/notes.md` (P10).

## Diagnosis

In `Driver._reception_turn` (`src/agora/drive.py`):

```python
sid    = self.reception_session_id
prompt = WAKE_PROMPT if sid else BOOT_PROMPT          # prompt chosen by sid
new_sid, ok = self._spawn(prompt, sid)
if not ok:
    if self._last_turn_stage in _SEMANTIC_STAGES:     # "reception" is one
        self._clear_backoff()
        return True                                   # RETURNS HERE
    ...
self.reception_session_id = new_sid                   # never reached
self._write_session(self._reception_session_path, new_sid)
```

The semantic-failure path returns **before** the session id is persisted. A turn
that ran correctly, called its tools and did real work — and was judged only on
CONTENT — throws away the resumable thread it just created.

This compounds with the prompt selector on the third line. No session id means
`BOOT_PROMPT`. A seat whose **first** turn is semantically failed never acquires a
session at all, so every later wake is another cold boot: full whoami + charter +
channel discovery, and no memory of the previous turn.

Measured after ~40 minutes of the run — session files on disk:

```
drive-lead.claude.reception-v2.session        <- the only one
(engine, gameplay, render, shell: none)
```

`lead` ran `boot:ok` then `wake:ok`. The other four ran `boot:error` every time
and never reached a single `wake` turn; `render` logged three consecutive
`kind=boot` turns.

The damage is visible in the channel ledger:

```
#18  shell   resolved  shell: P1 CONTROLS done
#22  shell   reply     shell P1 CONTROLS: claimed and starting now     <- forgot #18
#24  render  resolved  P1 render: DONE
#28  render  reply     render P1 PICTURE: claimed and done             <- forgot #27
```

and in the channel store, against the one-live-claim rule:

```
claim:p1-render / claim:render-p1                        (render, 2 rows)
claim:p1-shell / claim:shell-p1-controls / claim:msg-…   (shell, 3 rows)
```

Each cold boot forgot the row it had already opened and minted a new one. The
invariant was not broken by the models' judgment; it was broken by giving them no
memory of last turn.

An adjacent case was already fixed — the comment at the failure path reads *"Only
a real resume failure invalidates the session. Dropping it on a semantic verdict
threw away the resumable thread and paid a full cold-start BOOT_PROMPT on every
subsequent wake."* That change stopped the driver **discarding an existing**
session on a semantic verdict. It never made it **adopt the new one**, so a seat
that already had a session is protected and a seat whose first turn fails
semantically can never get one.

## The change

Adopt the session on the semantic path, then return:

```python
if self._last_turn_stage in _SEMANTIC_STAGES:
    self._clear_backoff()
    # The turn RAN and left a resumable thread; only its CONTENT was judged.
    # Adopt it: otherwise a seat whose FIRST turn is semantically failed never
    # acquires a session at all and pays a cold BOOT_PROMPT on every wake.
    if new_sid:
        self.reception_session_id = new_sid
        self._write_session(self._reception_session_path, new_sid)
        self._reception_turns_on_session += 1
    return True
```

Apply the same treatment in the work lane (`_work_chunk` / `_work_session_path`),
which has the same shape.

Rotation: the adopted turn should count toward `--session-rotate` like any other,
so a seat failing semantically in a loop still rotates on schedule rather than
riding one session forever.

## Tests

- First turn fails with `stage="reception"`: a session file is written, and the
  next turn uses `WAKE_PROMPT` with `--resume`.
- Transport failure (`stage` in `(None, "harness")`) still clears the session —
  the existing behaviour must not regress.
- Session rotation still fires at `--session-rotate` when the turns in between
  were semantic failures.

## Related

- [0149](0149_per_ask_release_any_sender.md) — manufactures the false failures
  this defect converts into amnesia. Fixing 0149 alone would mask 0150 rather
  than fix it; both should land.
- [0151](0151_turn_lane_classification.md) — same module, also a
  misclassification of a turn that ran fine.
