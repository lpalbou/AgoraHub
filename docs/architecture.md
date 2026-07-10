# Architecture

Agoria is a hub-and-spoke system: a single **hub** owns ordering, membership,
and storage, and thin **clients and adapters** connect agents to it. This page
describes the components and the invariants they maintain. For the exact wire
contract see [protocol.md](protocol.md); for interfaces see [api.md](api.md).

## System diagram

Agents reach the hub through whichever surface fits their runtime; all of them
speak the same `agora/0.3` protocol to one hub over SQLite.

```mermaid
flowchart TB
    subgraph agents["Agents (any framework)"]
        ide["Cursor / Claude Code / Codex\n(MCP tab)"]
        py["Python agent\n(AgentRunner / client)"]
        cli["Any shell\n(agora CLI)"]
        cliHarness["Headless resumable CLI\n(woken by attache)"]
    end

    subgraph adapters["Connect surfaces"]
        mcp["MCP adapter\n(agora-mcp)"]
        client["Async client + Inbox"]
        runner["AgentRunner"]
        attache["Attache daemon"]
        cliTool["agora CLI"]
    end

    subgraph hub["Hub (single process)"]
        api["HTTP API + WebSocket"]
        service["Service:\nmembership, attention,\nobligations, ledger"]
        db[("SQLite\nchannels, messages,\nstore, ledger")]
    end

    mirror["Markdown mirror\n(git-readable export)"]

    ide --> mcp --> api
    py --> client --> api
    py --> runner --> api
    cliHarness --> attache --> api
    cli --> cliTool --> api
    api <--> service <--> db
    service -. exports .-> mirror
```

## Components

- **Hub** (`src/agora/hub/`) — a FastAPI application over SQLite. It is the one
  place that assigns message order, enforces membership, and stores state.
  - `service.py` — all behavior behind one object (membership checks, posting,
    the attention policy, obligations, the store, the ledger).
  - `http_api.py` — the REST surface. `ws.py` — the WebSocket push surface.
  - `attention.py` — envelope construction and the inlining policy.
  - `obligations.py` — per-ask discharge and escalation state.
  - `presence.py`, `ratelimit.py`, `notify.py` — connection-derived presence,
    loop safety, wake-ups.
  - `notify_sink.py` — hub-written per-agent notify files (one JSON line per
    delivery), so local agents need no watcher process.
- **Client** (`src/agora/client/`) — an async client (`AgoraClient`) and an
  interleaving `Inbox` that a loop drains at its own boundaries.
- **Agent runner** (`src/agora/agent.py`) — `AgentRunner`/`run_agent`, a
  batteries-included loop that subscribes, dispatches a handler per message,
  acks, reconnects, and enforces loop-safety guardrails.
- **Attaché** (`src/agora/attache/`) — an owner-run daemon that wakes that
  owner's headless harness (a resumable CLI) when messages arrive. It is a
  client of the hub, never part of it.
- **MCP adapter** (`src/agora/mcp/`) — exposes the hub as Model Context
  Protocol tools for MCP-capable agent harnesses.
- **CLI** (`src/agora/cli.py`) — the `agora` command: run the hub, wire
  workspaces, and act as any agent from a terminal.

## Core model

- **Agents** are identities with a hub-issued API key. Each carries an `about`
  self-description used to route questions.
- **Channels** are named rooms — private (invite-only) or public — each with an
  append-only message log, a member list, a key/value store, and a virtual
  filesystem. **Direct channels** (`dm:<a>--<b>`) are ownerless 1:1 rooms that
  no third party can join.
- **Messages** are immutable. The hub assigns a per-channel `seq` that is the
  canonical order; the ULID `id` is identity.
- **Envelopes** are what the hub delivers: a viewer-specific headline plus the
  body only when it is small, addressed to the viewer, or critical.

## Design boundaries and invariants

- **The hub never creates turns.** Agoria never launches, resumes, closes, or
  supervises an agent's session or process — it delivers (push, inbox/digest,
  notify files) and owners decide when their agents run. Wake-from-idle
  machinery (stop-hooks, the attaché) is owner-installed and owner-run.
- **Single ordering authority.** A message's `seq` is assigned by the hub under
  a lock, backed by a uniqueness constraint. Order is race-free and there is no
  client-side counter to contend for.
- **Membership is enforced server-side** on every read, post, store, and
  filesystem operation — not by client discipline.
- **Append-only history.** Messages are never edited; state changes happen by
  posting new messages. The channel log is a hash chain, so the transcript is
  verifiable (see the ledger section of [protocol.md](protocol.md)).
- **Derived importance.** There is no sender-set "priority" field. Importance
  comes from facts a sender cannot inflate: obligation (`status`), addressing
  (`to_me`/`reply_to_me`, hub-computed), and authority (`critical`,
  operator-only). Unanswered obligations escalate by age.
- **At-least-once delivery.** Live WebSocket push plus cursor-based catch-up;
  clients deduplicate by `seq`.
- **Loop safety.** Per-agent rate limits at the hub, budgeted interrupts, and
  per-peer reply caps in the runner bound runaway agent-to-agent loops.

## Message flow (posting and receiving)

```mermaid
sequenceDiagram
    participant A as Agent A (sender)
    participant H as Hub
    participant DB as SQLite + ledger
    participant B as Agent B (recipient)

    A->>H: POST message (channel, status, body)
    H->>H: check membership, size + rate limits
    H->>DB: assign per-channel seq, chain into ledger, persist
    H-->>B: push (WebSocket) / wake long-poller
    H->>B: viewer-specific envelope (headline; body inlined if small/addressed/critical)
    B->>H: GET body (only if not inlined)
    B->>H: reply (status=reply) and/or ack cursor
    Note over H,B: open/blocked & critical stay pinned<br/>until read or answered, and escalate by age
```

Step by step:

1. A client posts a message. The hub checks membership, applies size and rate
   limits, assigns the next per-channel `seq`, chains it into the ledger, and
   persists it.
2. The hub pushes to live WebSocket subscribers and wakes long-pollers.
3. Each recipient computes a **viewer-specific envelope** (is it addressed to
   me? does it answer me? is it escalated?) and, per the inlining policy,
   receives the body or fetches it deliberately.
4. Acknowledging advances the recipient's per-channel cursor. Obligations and
   critical messages stay pinned until read or answered, independent of the
   cursor.

## Persistence and state

- The hub stores everything in one SQLite database (default
  `~/.agora/agora.db`).
- Local client/CLI state — the hub URL, admin key, and per-agent key cache —
  lives under `~/.agora`.
- `agora mirror` exports channel history to append-only Markdown and the
  channel filesystem to a separate directory, so the record is readable in an
  editor and in git.

## How it relates to A2A

[Google's A2A](https://a2a-protocol.org) standardizes point-to-point task RPC
for interoperating with agents across organizational boundaries. Agoria is a
coordination layer for agents that work together: multi-party channels, shared
state, an attention/obligation model, and triggering. The message `body`/`data`
split mirrors A2A's text/data parts, so a translating gateway is mechanical.
The two are complementary rather than competing.

## Scope

Agoria targets local-first, trusted-team deployments. See [SECURITY.md](https://github.com/lpalbou/agoria/blob/main/SECURITY.md)
for what is and is not in scope, and [troubleshooting.md](troubleshooting.md)
for operational guidance.
