# Planned: One guard model, rendered per harness by adapters

## Metadata
- Created: 2026-08-27
- Status: Planned
- Completed: N/A

## ADR status
- Governing ADRs: None
- ADR impact: New — the adapter contract gains a third declared capability
  alongside `REASONING_VOCAB` and `PERMISSION_VOCAB`.

## The shape

agora holds **one** guard model. Adapters render it into each harness's native
mechanism. A harness that cannot enforce a clause says so, by name, at wiring
time — it never silently drops it.

```
                    ┌──────────────────────────────┐
   operator ───────►│   THE GUARD  (agora, one)    │
   states it once   │   refuse: [pattern, why]     │
                    │   grant_write: [path]        │
                    └──────────────┬───────────────┘
                                   │  adapters render it
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
        ClaudeAdapter        CodexAdapter          PiAdapter
        mechanism=hook       mechanism=sandbox     mechanism=none
        PreToolUse ──────┐   -s workspace-write    REFUSES TO WIRE
        calls back into  │   writable_roots=[…]    (states what it
        `agora guard-    │   network_access=false   cannot enforce)
         check`          │
                         └──► the ONE model above, at decision time
```

The load-bearing choice: **the claude hook does not carry a copy of the rules.**
It shells back to `agora guard-check`, which reads the one model. A rendered
copy of a policy is a policy that goes stale the first time the operator edits
it, on one seat, silently — exactly the failure this item exists to remove.

## Context

A live rescue fleet (7 driven seats, `claude` and `codex`, isolated hub) was
given two operator prohibitions — the project's MongoDB is read-only, and no git
mutable operation — plus one grant: a shared gitignored scratch folder inside
the project tree that every seat was told to use as its base folder.

Neither the prohibitions nor the grant were expressible through agora. All three
had to be hand-built, per harness, outside the driver.

## Current code reality

`agora drive` exposes exactly one safety knob, `--permissions`, and each adapter
maps it to something of a different KIND:

- `ClaudeDriveAdapter` (`src/agora/drive.py:1237-1238`) —
  `write` -> `--permission-mode auto`, `all` -> `bypassPermissions`. `auto` asks
  a MODEL to classify each Bash command. Observed live 2026-08-27: that
  classifier became unavailable (`claude-opus-5[1m] is temporarily unavailable,
  so auto mode cannot determine the safety of Bash right now`) and **every Bash
  call was denied for every claude-family seat**, for hours, while codex seats
  were unaffected. The seats stayed "healthy": turns returned `status=ok`, so
  neither `agora doctor` nor the board nor the driver log showed a fleet at half
  capability. It surfaced only because an agent complained in a channel.
- `CodexDriveAdapter` (`src/agora/drive.py:952-1020`) — `PERMISSION_VOCAB` is
  `("write",)`, pinning `-s workspace-write` and
  `-c sandbox_workspace_write.network_access=false`. A kernel boundary, and a
  far stronger one: a codex seat cannot reach a remote database at all.

So one flag means *a model's opinion* on one harness and *a kernel sandbox* on
another. There is no vocabulary in which an operator can state a rule once.

Two consequences, both hit in one session:

1. **No way to state a prohibition.** "Never run pytest" had to become a
   hand-written Claude Code `PreToolUse` hook, because the project's
   `tests/conftest.py:106-111` ends every pytest session by issuing `.drop()`
   against the configured MongoDB. Claude Code has `PreToolUse`; the codex
   wiring agora writes (`setup_harness.py:1441`) declares only `PostToolUse`,
   `SessionStart`, `Stop`, `UserPromptSubmit` — all post-hoc, useless as a gate.
2. **No way to state a grant.** The operator's shared folder is outside codex's
   writable set (`workspace-write` = `[workdir, /tmp, $TMPDIR]`), so codex seats
   got `patch rejected: writing outside of the project` on the one folder they
   were told to use. `--harness-arg k=v` renders as `--k v`
   (`src/agora/drive.py:786-789`) and can never produce `-c k=v`, so there is no
   supported path to codex's config layer at all.

## Problem

An operator can state a rule in prose and have nothing enforce it, or reach past
agora into each harness's private config and enforce it N times, differently,
with no test that any of them still holds. There is no third option, and the
harness count is 7 and growing.

Prose is not a permission. A mission saying "the database is read-only" is read
by the model and can be reasoned around; a gate returning a refusal cannot.

## Scope

**1. The model — one declaration, operator-owned.**

Set like a mission (operator-authored, no tool lets a seat soften its own), and
scoped per seat or per channel:

- `refuse: [{pattern, why}]` — command patterns that are refused. `why` is
  returned to the agent verbatim, because a refusal that does not say why gets
  worked around rather than respected.
- `grant_write: [path]` — additional writable roots.

Deliberately not a policy language: a refusal list and a grant list are what the
field needed. Two lists, one meaning each.

**2. The adapter contract — declare the mechanism, mirroring the existing idiom.**

agora already has the right pattern for this: `REASONING_VOCAB = ()` means "this
harness takes NO reasoning knob" — a statement, never a missing list. Guards get
the same treatment:

- `GUARD_MECHANISM: "hook" | "sandbox" | None`
- `GUARD_ENFORCES: frozenset` — which clauses this harness can actually honour.
- `render_guard(policy)` — emits the wiring (hook entry, argv, config).

`None` and a partial `GUARD_ENFORCES` are both first-class answers, printed by
`agora harness-check` and refused loudly at wiring time when the operator asked
for a clause the harness cannot deliver. **A guard clause that cannot be
enforced must fail wiring, not be dropped.** Silent partial enforcement is worse
than none: it reads as protection.

**3. `agora guard-check` — the one decision point.**

A hook-shaped subcommand: reads the harness's event on stdin, resolves the seat,
loads the one model, decides. Every harness with a pre-execution hook wires to
this same binary. Fail-closed on unparseable input, and audit every decision —
the driver logs MCP tool names and never records Bash at all, so this is the
only place fleet shell activity becomes visible.

**4. Report a degraded permission path.**

The claude outage was invisible because a turn making zero tool calls still
scored `ok`. The driver should be able to say "this seat's tool path is
degraded" on the board, distinct from "the turn returned 0".

## Non-goals

- Not a policy language, not per-tool ACLs, not a rules DSL.
- Not uniform enforcement. Harnesses differ in kind; the deliverable is
  *state it once* and *report honestly what each will do with it*, never
  pretending a model classifier and a kernel sandbox are the same guarantee.
- **Not all harnesses now.** `claude` and `codex` have live demand. Every other
  adapter gets `GUARD_MECHANISM = None` and an explicit `unsupported` verdict
  until someone asks. That is a complete, honest answer, not a stub.

## Expected outcomes

- An operator states "no pytest, no Mongo writes, no git mutation" and "this
  folder is writable" once, and can read back what each seat will enforce.
- Editing the model changes behaviour everywhere at once, because no seat holds
  a copy.
- A harness that cannot gate says so at wiring time, not after an incident.

## Field evidence

Isolated hub `~/.agora-hubs/ragnarok-8890`, 2026-08-27. The hand-built
workarounds live there as the reference implementation this item should
subsume — each one is a thing agora should have done:

- `bash-guard.py` — the `PreToolUse` gate (fail-closed, audits every attempt).
  Becomes `agora guard-check`; its hard-coded pattern list becomes the model.
- `test-bash-guard.py` — 48 cases. Two real holes came out of RUNNING it, not
  reviewing it: `.venv/bin/pytest` escaping a boundary class, and
  `mongosh --eval "db.x.deleteMany({})"` escaping a snake_case-only method list
  (Codex's camelCase Mongo API was simply absent). Any central model ships with
  this suite, and the mongosh case is why `refuse` patterns must be reviewed
  against every client vocabulary in play, not just the one in front of you.
- `bin/codex` — a PATH shim injecting
  `-c sandbox_workspace_write.writable_roots=[…]`. Becomes `grant_write`
  rendered by `CodexDriveAdapter`.
- `start-fleet.sh` — per-harness `--permissions` AND per-harness `PATH`, because
  one flag could not express two seats' needs.
