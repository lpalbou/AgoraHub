# 0161 — Fleet liveness: the hub had the right answer behind the wrong key

**Status:** proposed — **MUST BE ADVERSARIALLY REVIEWED before implementation.**
The four defects below are measured against a live hub; the fixes are
one-liners in appearance and touch the watchdog's flap-guard semantics in
practice, which is exactly where this codebase has regressed before.
**Trigger:** the R-Type fleet freeze of 2026-08-23 → 24. For six hours two of
three worker seats were deaf, and every surface the operator can reach said
the fleet was healthy.

## The four surfaces, measured at 04:17:50 while two seats were wedged

```
/admin/doctor   oc1 reception=armed  age=857s
                oc2 reception=stale  age=22515s   (6h15m)
                oc3 reception=stale  age=23722s   (6h35m)
                fleet {"eligible":4,"live":4,"live_fraction":1.0,
                       "collapsed":false,"dark_episode":false}
/presence       oc2 "active", oc3 "active"        <-- refreshing on a 300s cadence
/board          1 stale ask, 2 claims             <-- nothing about any of it
/supervise      403 "no delegation of yours reaches any room you are in"
```

The last line was returned **to the operator**.

### Defect 1 — `/presence` affirmatively lies

`_ACTIVE_WINDOW = 600.0` keys on `_last_seen`, which `touch()` sets on *any*
authenticated call. A seat six hours deaf reads as `active`, i.e. *"reachable
at its next turn"* — which is what `who_is_reachable` and `agora status`
show, and what a peer deciding whether to wait on someone reads. Still true
as of this writing: `/presence` reports oc2 `active` while its turn has been
wedged for 6h20m.

*(One unknown, stated: something re-touches `_last_seen` for the wedged seats
every ~300s. 130s of 1 Hz `lsof -i TCP:8875` sampling caught only
`agora-tui`, `agora up` and `agora runner`. The presence numbers are
verified; the toucher is not identified. Worth finding before fixing, in
case the fix is upstream.)*

### Defect 2 — the fleet aggregate discards its own authoritative signal

`hub/service.py:8702`:

```python
def _fleet_seat_live(self, agent_id: str) -> bool:
    rec, _ = self.presence.reception(agent_id)
    if rec == "armed":
        return True
    return self.presence.get(agent_id).state in ("idle", "working", "active")
```

`reception == "stale"` is *the* deaf signal, and it loses to `presence ==
"active"` on the second line. Result: `live_fraction: 1.0, collapsed: false`
with two of three workers wedged.

`presence.py` carries a ~20-line comment about "THE THIRD CLOCK
CONTRADICTION" between `_ACTIVE_WINDOW` and `_RECEPTION_STALE`, pinned by a
test. **The contradiction was fixed in `PresenceTracker.get` and left
standing here.** Proposed: for any seat that has *ever* announced reception,
`stale` outranks `active`.

### Defect 3 — the DEAF watchdog is gated on owing something

`hub/service.py:9130-9137`:

```python
state, age = self.presence.reception(agent_id)
if state != "stale":
    return
overdue = self._escalated_debts(agent_id)
if not overdue:
    return            # <-- both wedged seats owed ZERO
```

At the time: oc2 `to_answer=0, escalated=0` and **four** `in_progress` claims
idle 31,612 / 22,860 / 22,262 / 22,053 s; oc3 **two**, at 39,900 / 23,736 s.
`episodes.deaf_since: null` for both.

**A seat that is deaf while holding six stalled claims and owing nobody a
reply is invisible to the deaf watchdog by design** — and that is precisely
the shape of a mid-build fleet, where the seats owe each other work rather
than answers. Proposed predicate: `stale` **AND** (escalated debts **OR** ≥1
`in_progress` claim idle > `_RECEPTION_STALE`).

### Defect 4 — the one correct surface is unreachable

`/admin/doctor` — *"per seat: reachable? owes what? working on what? held up
by what?"* — is `hmac.compare_digest(token, admin_key)`-gated
(`hub/http_api.py:1024`). The operator's own bearer key is refused, and
there is **no `doctor` tool in the MCP surface at all**. So the correct
diagnosis existed, continuously, all night, behind a credential nobody was
holding.

Proposed: promote to `/fleet`, operator-**or**-admin; add an MCP
`get_fleet_health`; include per-seat `reception_age`, per-claim
`idle_seconds`, and live turn duration. And make `/supervise` answer an
operator — refusing the root of trust with *"ask the operator"* is a scoping
bug, not a privacy feature.

Related and same shape: `board()` (`service.py:7813`) iterates
`db.channels_of(agent.id)` and never reads `agent.operator`, so an operator
cannot see a room they are not a member of even though they may
unarchive, kick and retire in it. See `planned/0158`.

## Suggested peer-silence follow-on (weakest part — attack this first)

When a claim row declares a dependency on a seat whose reception is stale
beyond `_RECEPTION_STALE`, post one addressed `blocked` to the **watcher**.
This would have given the coordinator a wake source that is not *"a wedged
peer talks to me"* (see `proposed/0160`).

*Why it may be wrong:* it is one more hub-authored obligation, and the
incident already shows the hub authoring 26 `status=open` messages against
its own agents with no rate limit (`RateLimiter.acquire` is called at exactly
two sites, `service.py:2743` and `:5946`; `_post_system` at `:3341-3375`
inserts directly). Adding a generator before fixing the debounce may make
things worse. Sequence it after `proposed/0162` §nudges, or fold it in.

## Where this is weak

- Defects 1–3 are three views of one conflation; fixing 2 and 3 without
  understanding the 300s toucher in 1 may just move the symptom.
- Every fix here makes the watchdog *more* eager. This codebase's flap-guard
  and episode-dedupe exist because eager watchdogs have misfired before
  (`_dark_since`, `_deaf_since`, `_escalation_rewake_band`). A reviewer
  should specifically try to construct a false-positive storm from the
  proposed §3 predicate — a fleet mid-build always has idle `in_progress`
  claims.
- No test currently fails. Whatever lands here should start from a mutant:
  wedge a seat in a fixture, confirm the surface goes red, then fix.
