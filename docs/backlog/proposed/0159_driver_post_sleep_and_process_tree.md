# 0159 — Driver spawn site: post-sleep staleness and the orphaned process tree

**Status:** proposed — **MUST BE ADVERSARIALLY REVIEWED before implementation.**
The diagnosis below is measured; the remedies are not yet attacked. Both
touch `_spawn_turn`, the single most load-bearing call in the driver.
**Trigger:** the R-Type fleet freeze of 2026-08-23 22:08 → 2026-08-24 04:15
(operator postmortem, 2026-08-24). Two adversary passes; one specifically
tasked with refuting the process-tree claim, which it confirmed with repros.

**Operator ruling that bounds this item (2026-08-24):** *host sleep pausing
the fleet is CORRECT and intended.* A closed lid must pause the agents.
Anything that would make a turn keep running, or be killed, because wall
time passed while the machine slept is **out of scope and explicitly
rejected.** `time.monotonic()` excluding sleep is the desired semantics.

## What happened

`agora drive` bounds a turn with `subprocess.run(..., timeout=…)`, measured on
`time.monotonic()` = `mach_absolute_time()` on macOS, which freezes during
sleep. Correct, per the ruling. But on wake at 03:46:18 neither wedged seat
resumed:

| seat | turn started | host woke | killed by cap | dead time after wake |
|---|---|---|---|---|
| oc3 | 21:38:06 | 03:46:18 | 04:15:09 | **29 min** |
| oc2 | 22:02:55 | 03:46:18 | 04:39:59 | **53 min** |

Both were holding a single `ESTABLISHED` TCP socket to the model provider
opened *before* the sleep, both at ~1% CPU, and both produced real work
within minutes of being respawned. Strongly indicative of a dead connection
nobody detects. **The pause is fine; the resume is broken.**

(An adversary predicted both kill times from the sleep intervals to within
16s — 04:15:26 vs 04:15:09, 04:40:15 vs 04:39:59 — which is what establishes
the clock mechanism beyond argument.)

## Defect 1 — nothing notices that the world moved

On wake the driver carries on with: a dead provider socket, a listener
position hours old, and a hub that has meanwhile accrued hours of SLA and
escalation on `time.time()` while every driver deadline sat frozen on
monotonic. Two clocks, no reconciliation.

**Proposed:** once per main-loop pass, track `time.time() - time.monotonic()`.
That difference is constant while awake and jumps by exactly the sleep
duration on wake.

```
gap = time.time() - time.monotonic()
if gap - last_gap > SLEEP_GAP_S:        # host slept
    log AGORA_DRIVE event=host-slept seconds=N
    abandon the in-flight turn
    re-read /owed and re-arm
```

~6 lines, no new dependency. Note it also catches the case a timeout never
can: a turn that **completes successfully** just after a wake, having decided
what to do from an inbox snapshot taken six hours earlier.

**Open questions for the reviewer.** Is abandoning the turn right, or should
the driver first probe the hub and only abandon on failure — a mid-slice
turn may hold real uncommitted reasoning. What is the right `SLEEP_GAP_S`
(60s proposed) so ordinary NTP slew never trips it. Does the listener need
the same detection independently (`listen.py` deadlines are all monotonic:
`:948,949,974,980,1005,1022,1035,1038`).

## Defect 2 — the kill orphans the whole descendant tree

`grep start_new_session|preexec_fn|killpg|setsid src/agora/drive.py` → **zero
hits at the spawn site.** On timeout, `subprocess.run` kills the direct child
only. Verified with a repro plus `ps`:

```
[cap 3s] elapsed=3.00s   direct child 75765 -> dead
                         GRANDCHILD   75766 -> ALIVE, ppid=1
```

A 20-descendant variant left 20 processes reparented to PID 1. Each keeps
running **in the seat's cwd, with the seat's credentials**, while the loop
immediately spawns the next turn (`drive.py:3778-3780`) — so one seat can
hold two live turns writing one workspace. That is strictly worse than an
overrun: an overrun shows up in `dur_s`; a live orphan shows up nowhere.

The live incident had exactly this shape available: each wedged `opencode`
held an `agora-mcp` grandchild.

**Proposed:** `start_new_session=True` at the spawn, and kill the process
group. `src/agora/runner.py:271/392/400` **already does exactly this pair**
for the drivers it spawns — this is one module adopting its sibling's
solution, not a new design.

**Open questions for the reviewer.** Changing signal delivery affects every
adapter (`cursor-agent`, `claude`, `opencode`, `pi`, `AbstractCode`) — which
of them rely on sharing the parent's process group, e.g. for terminal
signals? Does `SIGTERM`-then-`SIGKILL` (runner's ladder) beat a bare
`SIGKILL` here, given a harness may need to flush a session file? Does any
adapter's session-id parsing depend on output written during shutdown?

## Defect 3 — capture volume can overrun the cap (small, real, unbudgeted)

Measured on CPython 3.12.7: the `_communicate` read loop honours the cap
exactly, but `b''.join()` inside `TimeoutExpired` and the `.decode()` at
`drive.py:3075-3079` run *after* it.

| child spew rate | captured at cap | overrun |
|---|---|---|
| 1 MB/s | 2.9 MB | +0.01s |
| 10 MB/s | 29.9 MB | +0.02s |
| unbounded | **7.9 GB** | **+14.8s, ~16 GB resident** |

Host RAM is 18 GB. A runaway harness can OOM the driver. Spooling capture to
a file bounds this; a process-group kill does not. **Low priority** — no
observed incident — but it is the one thing the old
"give the child a file instead of a pipe" remedy would actually have fixed,
and it should be recorded so the reasoning is not lost again.

## Related correction already applied

The `KNOWN, MEASURED LIMIT` comment at `drive.py:3060` claimed the cap was
defeated by a grandchild holding the pipes. On POSIX the re-`communicate()`
that could block is `if _mswindows`; measured with 20 descendants holding the
pipes at a 3s cap, overrun **+0.01s**. The comment has been rewritten in
place with the measurements above. **`src/agora/runner.py:370` still cites
the old, wrong diagnosis** as its justification for `start_new_session` — the
conclusion is right, the cited evidence is the artifact. That file was being
edited by another seat at the time and was deliberately left alone; correct
the citation when convenient.
