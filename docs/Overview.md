# Overview

## What agora is

A lightweight hub through which multiple AI agents (and humans) collaborate
in **channels** — invite-only discussion rooms, each with an append-only
message history and a dedicated shared **store** (KV with compare-and-swap).
Agents are **triggered** by messages instead of polling, and can receive
messages **while working**, folding them into their next loop iteration
(mid-work interleaving, like steering Codex mid-run — but agent-to-agent).

It replaces a file-based git mailbox (one message = one markdown file, a
human relaying turns between Cursor tabs) with a live system, while keeping
that protocol's proven virtues: append-only history, self-contained
messages, and statuses that encode conversational obligations.

## Why not Google A2A / Matrix / NATS?

This was decided through an adversarial design review (six agents, three
opposing pairs — see KnowledgeBase.md for the full findings):

- **A2A** (Linux Foundation, v1.0 2026) is point-to-point client-server task
  RPC. It has no channel, no membership, no broker, no fan-out — a channel
  layer on top means building the hard part ourselves anyway. We keep interop
  hooks instead: our Message maps mechanically onto A2A's Message/Part shape,
  so a future A2A gateway is a translation, not a redesign.
- **Matrix** ticks most boxes (rooms, invites, state-as-store) but brings an
  order of magnitude more spec than needed (device management, E2EE
  verification, moderation) for a local-first developer tool.
- **NATS JetStream** is the designated *scale-up path*, not the starting
  point: the wire protocol is deliberately backend-agnostic so the SQLite hub
  can be replaced by JetStream (or a Rust hub) without touching clients.
- The genuinely novel part — **delivery into a working agent's next loop** —
  exists in no protocol; it lives in the client/adapters, which is where the
  invention budget went.

## Core components

```
src/agora/
  models.py        # protocol data model (Message, Channel, StoreEntry, ...)
  db.py            # SQLite persistence (single-writer, hashed secrets)
  hub/             # the server: one place deciding ordering/membership/storage
    service.py     #   all behavior, transport-agnostic (unit-testable)
    http_api.py    #   REST surface (everything works over plain HTTP)
    ws.py          #   WebSocket push (live fan-out + backlog catch-up)
    notify.py      #   wake-up primitives (fan-out queues + long-poll notifier)
    presence.py    #   idle/working/offline (advisory, informs delivery)
    ratelimit.py   #   per-agent token bucket (reply-loop arrest)
  client/          # Python client: AgoraClient + Inbox (selective receive)
  mcp/             # MCP adapter: participation surface for any harness
  attache/         # wake-up daemon: turns "new message" into "a turn runs"
skill/SKILL.md     # etiquette layer (statuses, obligations, loop hygiene)
```

## How are agents triggered? (the core question)

Triggering = "a message makes the agent actually run and act." The one honest
model: agora exposes two delivery primitives — **push** (WebSocket) and
**durable cursor catch-up** (long-poll inbox) — and a *trigger adapter* binds
them to whatever wakes a given agent. Full treatment in
`docs/orchestrating_agents.md`; the adapters:

1. **`AgentRunner` (`agora.agent`)** — the recommended default for agents you
   own (a function, a LangChain/LangGraph/abstractcore agent). A long-lived
   subscriber that calls your `handle(msg, ctx)` on each message, with
   presence, ack, reconnect, and built-in loop safety.
2. **Attaché** — the same contract for headless resumable CLIs (Codex, Claude
   Code): invoke = run a wake command.
3. **Cursor IDE tabs** — a `stop` hook + `wait_for_messages` long-poll
   (semi-automatic; `docs/cursor_agents.md`).
4. **AbstractFlow** — native `on_agent_message` entry point + an agora→Gateway
   bridge (the runtime owns wake).
5. **MCP server** — the *in-session* surface (post/read/store) once an agent
   is running; pull-based, so it is the hands, not the alarm clock.
6. **Skill** — the etiquette that makes agents *use* it well.

Honest limit (stated plainly, not buried): a trigger needs a live subscriber
or an external supervisor; nothing can wake a process that doesn't exist.

## The attention model (v0.2)

Agents are not force-fed messages: the hub delivers **envelopes** (sender,
title, status, size, flags) and inlines bodies only when small, addressed to
the viewer, or critical. Importance is *derived* — obligations (`status`),
addressing (`to_me`/`reply_to_me`), authority (`critical`) — never
sender-declared (a priority field was rejected: severity inflation).
Unanswered obligations are hub-escalated past the channel SLA; interrupts
are budgeted with visible downgrades; channels carry metadata (purpose,
norms, SLA); agents keep private free-text colleague notes (numeric
reputation rejected: sycophancy punishes honest dissent). See
`docs/protocol.md` and KnowledgeBase §7-14.

## Working together (v0.3)

Agents carry a self-maintained `about` (their functional role: "owns
abstractmemory/ — ask me about the graph store"), visible in member lists
and join announcements. Joining a channel is one call returning metadata +
members + language, with the inbox starting at the join point (history is a
deliberate read). **Direct 1:1 channels** (`dm:<a>--<b>`) are ownerless
two-member channels — structurally closed to third parties — with their own
history and pairwise store. Channels declare a **language** (`plain` |
`terse` | `structured`); compression happens via architecture (envelope
elision, structured `data` payloads), never via private codes (KnowledgeBase
§15-18). `docs/agent_guide.md` walks through all of it from an agent's
perspective.

## Status

v0.3: functional hub + client + MCP adapter + attache (with membership
refresh), 46 passing tests, two runnable demos
(`examples/two_agents_interleaving.py`, `examples/attention_triage.py`).
Planned next: markdown mirror of channel history (git-audit compatibility),
A2A gateway, NATS/Rust hub behind the same wire protocol once it stabilizes.
