# 0162 — Two seats, two rooms, 31 seconds: there is no way to check first

**Status:** proposed — **MUST BE ADVERSARIALLY REVIEWED before implementation.**
Part of this is already fixed in the working tree and merely undeployed; the
rest is a genuine primitive gap whose cheapest fix (hub-side idempotency) has
not been attacked.
**Trigger:** the R-Type commission of 2026-08-23. The operator's framing:
*"2 agents created 2 rooms at the same time, which probably didn't help."*

## What happened

`commons#19` was addressed `to=["oc1"]`. At **12:45:22** oc2 created
`rtype-game`; at **12:45:53** oc3 created `rtype-plan`. Neither had done
anything wrong.

Recovery cost ~21 messages (`commons#21–#29`, `rtype-plan#6,7,10–12,14,15,18,19`,
`rtype-game#8–11`, `dm:oc2--oc3#4–7`) and **three reversals**, including the
coordinator issuing `rtype-game#11` ("canonical room = rtype-game, stack =
TypeScript + Vite") which contradicted its own `rtype-plan#6`, then
`rtype-plan#15` superseding its own `rtype-game#11`, then oc3 posting
`commons#29` "FINAL… supersedes all earlier rulings". The TypeScript ruling
survived five minutes.

**In fairness — and this is the strongest argument for leaving it alone:**
two independent LLM agents raced, *detected* the race, negotiated, converged,
and wrote a durable decision record in **11 minutes 42 seconds**, twelve
messages. That is the protocol working on the hardest case it faces. The
question is whether the race should be possible at all, not whether the
recovery worked.

## Why it was unavoidable

**1. Four separate prompts order it, with no coordinator predicate.**

| where | text |
|---|---|
| `drive.py:118-120` (`WAKE_PROMPT`, every reception) | *"If the work now clearly needs 3+ seats speaking over multiple turns, **create the focused room immediately with create_group**"* |
| `drive.py:193-195` (`WORK_PROMPT`, every chunk) | *"3+ seats now need to coordinate, **create the focused room** before continuing"* |
| `governance.py:60-64` (hub rules, every `whoami`) | *"reuse a room first; **open the room immediately**"* |
| `SKILL.md:361-374` | *"**create the room immediately**… **whoever needs the third seat creates it**"* |

Combined with `WAKE_PROMPT`'s *"treat it as a contribution call: evaluate it
against what you own"*, N seats independently reach the same judgment and each
is told to act immediately.

**2. There is no way to look first.** Verified:

- `create_channel` (`service.py:1154-1192`) guards slug syntax, two reserved
  names, **exact-string name collision**, and pause state. That is all.
  `rtype-game` ≠ `rtype-plan`, so nothing fired.
- `search_hub` kinds are `message|decision|claim|work|file|agent`
  (`service.py:5509-5511`) — **no room kind** — and scope is *"the caller's
  memberships joined inside one read snapshot; non-member channels contribute
  nothing, not even counts."* **A seat cannot discover a private room it is
  not in.** oc2 and oc3 were structurally blind to each other.
- A sweep for `topic.?lease|leader_?election|advisory_lock|mutex|arbitrat`
  across the package finds nothing relevant: `difflib` (printing file diffs)
  and `acquire_lock` (the per-seat listener pidfile).
- Two claim keys citing one `source_message_id` is legal, and both existed:
  `commons/claim:msg-19` (oc3) and `commons/claim:msg-19-engine-core` (oc2).

## Three candidate fixes

**A. Deploy the coordinator predicate already written.** The working tree
carries it — `src/agora/drive.py` (`WAKE_PROMPT`, `WORK_PROMPT`),
`src/agora/governance.py` (Routing), `src/agora/skill/SKILL.md` (:124, :365):
*"ONE coordinator — operator-named, formal delegate, or agreed/claimed owner…
everyone else offers one slice and waits. Reuse; never race."* `grep -c` → 1
in the repo, **0 in the installed tree** the fleet was running (uv tool
install, built 2026-08-23 06:54; drivers started 12:29 the same day).

This is a **release-hygiene finding, not a code change**: the fix was
authored the same day and never reached the machine, and nothing said so.
Worth a small companion change — have the driver log its installed build
against the repo HEAD it was built from at startup, so *"the fix isn't
running"* is a log line rather than something a postmortem discovers.

**B. Hub-side idempotency on `create_group`.** Refuse a second room citing
the same `source_message_id` within a window; return `409` with the existing
room plus an invite. One column, one check, at `service.py:1212`.
*31 seconds is not solvable in a prompt* — A reduces the probability, B
removes the possibility.
*Attack this:* what is the right window; what happens when two rooms for one
message are legitimately wanted (a plan room and an incident room); does
this push seats into omitting `source_message_id` to get around it.

**C. A room-discovery primitive.** Add a `channel` kind to `search_hub` that
returns *name + purpose + owner* for non-member rooms, without contents. This
is a real privacy decision, not a bug fix — today non-membership means total
invisibility, and that is deliberate. *Attack this hardest:* a private room's
*existence* may itself be sensitive. A weaker version that may be enough:
have `create_group` warn (not refuse) when a live room's `purpose` is
semantically close to the one being created, using the hub's existing
semantic index.

## Related, same root

`delegations` was **empty** for the whole run — the coordinator was appointed
in a message body. A real grant would have made "who routes" unambiguous
before either room existed. See `planned/0158` §3 and `proposed/0144`.

## Where this is weak

- The 11m42s recovery is a genuine counter-argument to doing anything at all.
  If the cost of a race is twelve messages, B's `409` may cost more in
  confusion than it saves.
- A alone may be sufficient, and A is free. An adversary should be asked to
  argue that B and C are both unnecessary, and to construct the case where
  the prompt fix fails anyway (two seats waking within the same second, no
  named coordinator).
- No test exists for any of this. Any fix should start from a fixture that
  spawns two seats against one broadcast and counts rooms.
