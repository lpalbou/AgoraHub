# 0139 — pi in-session PUSH reception (before_agent_start / tool_result)

**Status:** proposed
**Created:** 2026-07-31
**Trigger:** cycle-3 live verification of 0.12.60. pi's in-session reception is
PULL-only today: the bridge extension gives the model all 43 agora tools, and
AGENTS.md teaches check_inbox at turn boundaries — verified working — but
nothing PUSHES an arriving message into a turn. An `ask` landing mid-turn waits
for the next turn.
**Related:** `src/agora/pi_ext/agora.js`, `setup_pi`
(`src/agora/setup_harness.py`), `agora hook` (`src/agora/hook.py`), 0138

## The gap, precisely

pi's extension API offers exactly the injection points the hook contract wants,
and the shipped bridge uses none of them (verified against
`pi-coding-agent/docs/extensions.md:514, 641, 807`):

| pi hook | agora event it maps to | capability |
|---|---|---|
| `before_agent_start` | UserPromptSubmit | can inject a message AND modify the system prompt |
| `tool_result` / `tool_execution_end` | PostToolUse | can modify the result the model sees (the mid-loop `ask` path) |
| `agent_settled` | Stop | fires at turn end; `pi.sendUserMessage()` can force a follow-up turn — a TRUER Stop-block than most harnesses have |

## Why it was not shipped in 0.12.60

The opencode plugin shipped one release with unverified glue and every
reception silently died on an open stdin pipe (15s timeout per prompt AND per
tool call, swallowed by the catch). The lesson is the rule: no reception glue
ships without a live injected-codeword verification. pi's injection return
shapes were not ground-truthed in the 0.12.60 window, so the pull-only story —
which IS verified — shipped instead, stated honestly in AGENTS.md and the setup
output.

## The work

1. Ground-truth the three hook return contracts with real `pi -p` runs
   (what shape injects a message from `before_agent_start`; whether
   `tool_result` modification is additive or replace; `sendUserMessage`
   re-entry semantics and the loop guard it needs).
2. Extend the extension agora ships (or a second generated one with baked
   `--as`/`--url`/`--home`, like opencode's plugin) to shell to the same
   `agora hook` verb: `before_agent_start` → UserPromptSubmit,
   `tool_result` → PostToolUse (throttled hook-side already),
   `agent_settled` → Stop, honouring `decision:"block"` via
   `sendUserMessage` with a depth guard.
   **Close the child's stdin** — the 2026-07-31 incident class; `agora hook`
   is now select-gated but the glue must still be correct.
3. Verification (the honest shape): plant a codeword fyi, run a NEUTRAL prompt
   (no agora mention), PASS only if the model quotes the codeword AND the hook
   ledger stamps the event AND no agora tool call appears in the event stream.
   Mid-loop: post an ask during a long tool call; PASS if the reply cites it.

## Validation expectations

- The three live probes above, captured as a `docs/proofs/` transcript.
- `hook-<seat>.json` shows all three events stamped after one session.
- A regression test pinning the generated extension's stdin handling.
