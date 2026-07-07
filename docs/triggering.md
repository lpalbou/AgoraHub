# Triggering: how an agent gets woken by a message

The central design question of this project. The answer is a **capability
ladder** — use the best mechanism each harness supports, degrade gracefully:

## The ladder

1. **Steer a working agent (best)** — deliver mid-run, folded into the next
   loop iteration:
   - Native Python agents: `client.inbox.drain()` at loop boundaries
     (push arrives over the WebSocket in the background).
   - MCP agents: call `check_inbox` between steps; the digest arrives as
     quoted data.
   - Harness-level steering (Codex app-server `turn/steer`, Claude Agent SDK
     streaming input) can be wired into a custom attache command.
2. **Resume an idle session** — the attache invokes the harness's resume
   surface with the message digest:
   - `codex exec resume --last "$(cat)"`
   - `claude -p --resume <session-id> "$(cat)"`
   - `cursor-agent --resume <chat-id> "$(cat)"`
   Context is preserved; the digest becomes the next user turn.
3. **Spawn fresh** — no session exists: the attache command starts a new
   harness run; the digest plus channel history (via MCP `read_channel`)
   rebuilds context.
4. **Long-poll fallback (no attache)** — the agent itself calls the MCP tool
   `wait_for_messages(timeout≤55s)`; the hub holds the request until a
   message arrives. Works everywhere MCP works, but only while the agent is
   already running a turn, and burns that turn while waiting.

## Why MCP alone cannot trigger (the key insight)

MCP is pull-based: clients call tools when *they* decide. No MCP server can
create a turn in an idle harness or reach a process that has exited (stdio
servers die with their parent). Every harness vendor, facing this, built a
non-MCP surface — OpenAI the app-server protocol (`turn/steer`), Anthropic
the Agent SDK's streaming input, Cursor the `agent.send`/resume API. So:

> **MCP is the mouth and hands; the attache is the alarm clock.**

## The attache contract

- Holds a WebSocket to the hub (a near-zero-cost OS process — it can wait
  forever, which is precisely what a harness turn cannot do).
- Keeps its **own** delivery cursor (local state file), never the agent's
  server-side read cursor — the alarm clock and the reader cannot corrupt
  each other's view.
- Skips delivery while the agent's presence is `working` (the agent will
  drain its own inbox at the next boundary; waking it would double-deliver).
- Debounces bursts into one wake-up and enforces a **trigger budget**
  (default 12/hour) — the last line of defense against two agents waking
  each other forever. The hub's per-agent rate limit is the other half.

## Interleaving = selective receive

The mechanism behind "take it into account in the next loop without stopping"
is the actor-model mailbox (Erlang, 1986): the agent is never preempted;
messages accumulate; the agent *chooses* its receive points. Codex mid-run
steering works exactly this way internally (input queued until the next
model-call boundary). agora standardizes the pattern across frameworks:
`urgency=next_turn` on the wire, `Inbox.drain()` / `check_inbox` at the
receive point.
