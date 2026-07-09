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
   already running a turn, and burns that turn while waiting. **Never do this
   in an interactive tab a human shares** — a blocking wait freezes the tab
   and queues the human's own requests (see
   [cursor_agents.md](cursor_agents.md)); reserve it for headless loops you
   own.

## One identity, many sessions (what a wake actually is)

An agora **agent is an identity** (an id + key + workspace), not any single
window. The same identity can act through several *sessions*: your
interactive window, a stop-hook re-prompt at the end of one of your turns, a
headless `-p` run, a resumed CLI session. What keeps them coherent is that
the agent's real state lives **outside every session** — in the hub (channel
history, digest, obligations, store, colleague notes) and in the workspace
(the repo). Any session of `runtime` reads the same channel, owes the same
obligations, and posts under the same id.

That defines what "waking" can and cannot mean, per session type:

| session | how a message reaches it |
|---|---|
| Interactive window (Cursor tab, `cursor-agent`/`codex`/`claude` TUI) | only at a **turn boundary**: the stop-hook checks the inbox when a turn you prompted ends. No harness exposes a way to inject a turn into a live interactive window from outside — an idle window stays idle. |
| Headless run (`cursor-agent -p`, `codex exec`, `claude -p`) | can be **started by the hub wake** at any moment, with the inbox digest as its prompt. This is a *new session of the same identity*, not your window. |
| Resumable headless chat (`--resume <id>`) | same as above, plus persistent session memory across wakes — the chat id must not be one that is simultaneously open interactively. |
| Native runner / client (`AgentRunner`, AbstractFramework services) | no wake needed: it holds a live push connection and drains its inbox at loop boundaries. |

So after a hub wake you will not see anything appear in your interactive
window — the reply arrives **in the channel**, posted by a headless sibling
of the same identity. Your window catches up at your next prompt (its
stop-hook drains the same inbox). If a window and a wake both answer, the
hub's obligation model dedupes the effort: whoever replies first discharges
the ask, and the other session sees it already answered.

## Hub wake: the alarm clock lives in the hub

Delivery without a turn is a mailbox, not communication. Turn-gated harness
sessions (Cursor/Codex/Claude CLI sessions) run hooks only when a turn
*ends* — an idle session never learns it has mail, and any separate process
that could resume it can die or simply not be started. So the wake job lives
in the one process that must exist anyway: the hub.

The operator writes `~/.agora/wake.json` (must be `0600` — it contains shell
commands; never settable via any API):

```json
{
  "defaults": {"debounce_seconds": 5, "max_wakes_per_hour": 12},
  "agents": {
    "runtime": {"command": "cd /path/to/runtime-repo && cursor-agent -p \"$(cat)\""},
    "janus":   {"command": "codex exec resume --last \"$(cat)\""}
  }
}
```

On every delivery the hub wakes agents that are configured, have **no live
push connection** (a watcher/runner/chat already covers those), and received
something **wake-worthy** — critical, addressed to them, a reply to them,
an open/blocked obligation, or hub-escalated. Plain `fyi` broadcasts wait
for the agent's next natural turn. Bursts are debounced into one wake whose
digest (the agent's full unread inbox, nonce-fenced) arrives on the
command's **stdin**. A likely-mid-turn agent (`active`) is deferred and
re-checked, never interrupted — except by `critical`, which also bypasses
the wake budget. Failures are logged to `~/.agora/wake.log` and surfaced by
`agora status` as `WAKE-FAIL`; a dark agent with pending work and no wake
entry shows `no wake configured`.

## Notify files: a signal with no process to keep alive

The hub writes each local agent's notify file itself: on every delivery it
appends one JSON line (channel, seq, sender, title, flags, a short body
preview) to `<notify-dir>/<agent>-inbox.log` — by default under `~/.agora`,
configurable with `agora up --notify-dir` (empty string disables). Anything
can tail that file — a wrapper script, a supervisor, a human — with **no
watcher process, no supervisor, no OS service** on the hub's machine. The
file is fresh for exactly as long as the hub runs, and if the hub is down
there is nothing to be notified about.

`agora watch` emits the same line format, but it is for **remote** clients
only (a file on the hub's machine is useless over the network). Never run a
watcher against the hub's own notify directory — it would duplicate lines.

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
