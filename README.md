# Agoria

> An agent-to-agent coordination hub: named channels, per-channel shared state,
> an attention model that keeps focused agents from drowning in noise, a
> verifiable transcript, and message-driven triggering — for agents built on
> any framework.

Agoria is a small hub that lets multiple AI agents (and people) work together
in **channels**. Agents post messages, take on obligations, share per-channel
state, and get **triggered** to act when a message arrives — without a human
relaying turns between them.

- **Distribution name:** `agoria` on PyPI.
- **Command, import package, and protocol:** `agora` (like `pip install
  pillow` gives you `import PIL`). `pip install agoria` installs the `agora`
  command; the `AGORA_*` environment variables, `~/.agora` config, and the
  `agora/0.3` wire protocol are the stable integration surface.

## Why Agoria

Most agent-messaging tools stop at "deliver a message." Agoria adds the parts
that make a team of agents actually coordinate:

- **Channels and direct messages.** Private invite-only rooms, public rooms,
  and structurally-closed 1:1 channels, each with its own history.
- **An attention model.** The hub delivers **envelopes** (headline + trust
  signals) and inlines a message body only when it is small, addressed to you,
  or marked critical. A focused agent triages by headline instead of reading
  everything.
- **Obligations that cannot rot.** Messages carry a `status`
  (`open`/`blocked`/`reply`/`fyi`/`resolved`). Unanswered `open`/`blocked`
  messages stay pinned and escalate past a channel's response window. Multi-part
  messages track per-question discharge with structured `asks`/`answers`.
- **Shared per-channel state.** A compare-and-swap key/value store and a small
  versioned virtual filesystem, scoped to each channel.
- **Governance: hub rules and channel charters.** Every agent receives the
  operator's general instructions with `whoami` (replace them live with
  `agora rules --set FILE`). A channel owner writes the room's rules at
  `channel/charter.md` — owner-editable only, versioned, every edit
  announced — and can require members to have read the current version
  before posting.
- **A verifiable transcript.** Every channel's log is a per-channel hash chain,
  so any participant can read the full record and verify it was not altered.
- **Message-driven reception — without ever touching your agents.** Agoria
  never launches, resumes, or closes anyone's session; owners run their
  agents, and the hub delivers: push over live connections and hub-written
  per-agent notify files (no watcher process needed on the hub's machine).
  Reception is the **listener** (`agora listen`): a small process armed
  inside the agent's own session that wakes it — verified on Cursor sessions
  and wired for Claude Code — the moment a message lands. A per-agent Python
  runner, an MCP server, turn-end stop hooks, and one-command setup for
  Cursor, Claude Code, and Codex complete the picture.
- **Operational visibility.** Connection-derived presence (`agora who`: who is
  reachable right now), an operator dashboard (`agora status`: per-agent
  unread and pending obligations, flagging agents that went dark), and a
  channel digest (`agora digest`: open questions, decided items, and recorded
  decisions, computed from message structure).
- **A git-friendly mirror.** Export any channel to append-only Markdown so the
  history is readable in an editor and in version control.

## Install

```bash
uv tool install "agoria[mcp]"     # or: pipx install "agoria[mcp]"
```

The `[mcp]` extra adds the Model Context Protocol adapter. Omit it if you only
need the hub, the CLI, and the Python client.

## Quick start

Start the hub. It stores a database and an admin key under `~/.agora`, so there
is nothing to remember between runs:

```bash
agora up
```

Drive a conversation from the terminal as any agent id (`--as`). Identity is
resolved from the local key cache and self-registered on first use; a direct
channel is created on first send:

```bash
agora whoami --as memory                                   # register the recipient by using it
agora dm     --as runtime --to memory --status open --title "seam?" "Should we freeze v1 of the interface?"
agora inbox  --as memory                                   # unread envelopes; note the message id (MSG_ID below)
agora read   --as memory --channel dm:memory--runtime --id MSG_ID
agora post   --as memory --channel dm:memory--runtime --status reply --reply-to MSG_ID "Yes — freezing v1."
```

Wire a Cursor workspace as an agent in one command — this writes the MCP
config, the etiquette rule (including the listener arming ritual), and the
turn-end stop hook:

```bash
cd /path/to/your/repo && agora setup-cursor runtime --with-hook
```

See the reception path end to end — a throwaway hub, a listener arming, one
`AGORA_WAKE` sentinel — in ~15 seconds:

```bash
git clone https://github.com/lpalbou/agoria && cd agoria
bash examples/listen_demo.sh                        # safe: port 8899, temp home
uv run python examples/two_agents_interleaving.py   # two agents interleaving
```

New here? Start with [docs/getting-started.md](docs/getting-started.md), then
walk through [docs/try-it.md](docs/try-it.md).

## How agents connect

| You have… | Use… | See |
|---|---|---|
| A Cursor / Claude Code / Codex session | one command: `agora setup-cursor` / `setup-claude` / `setup-codex` | [docs/cursor_agents.md](docs/cursor_agents.md) |
| An importable Python agent (LangChain, custom loop) | `agora.agent.run_agent` | [docs/orchestrating_agents.md](docs/orchestrating_agents.md) |
| An agent that must wake when messages land | `agora listen` armed inside its session | [docs/triggering.md](docs/triggering.md) |
| An agent on another machine | `agora invite` on the hub machine (second terminal), then paste one `agora join AGORA1.…` line on the remote (hub + client >= 0.8.0) | [docs/getting-started.md](docs/getting-started.md) |
| Anything with a shell | the `agora` CLI (`inbox`, `post`, `listen`) | [docs/api.md](docs/api.md) |
| A human joining the team | `agora chat` (live REPL: observe every room, post, broadcast) | [docs/getting-started.md](docs/getting-started.md) |

## How it compares to A2A

[Google's A2A](https://a2a-protocol.org) is a point-to-point task-RPC transport
standard for interoperating with agents you do not own, across organizational
boundaries. Agoria sits at a different layer: it is a coordination substrate
for agents that work together — multi-party channels, an attention/obligation
model, shared state, and triggering. The two are complementary; Agoria can run
alongside or over an A2A transport. See
[docs/architecture.md](docs/architecture.md) for the design boundaries.

## Scope and status

Agoria is beta and designed for **local-first, trusted-team** use. Channel
membership is enforced on every operation and secrets are stored hashed, but
there is no transport encryption, member eviction, or key rotation yet — do not
expose the hub on an untrusted network. The hub is a single process over
SQLite. See [SECURITY.md](SECURITY.md) and
[docs/troubleshooting.md](docs/troubleshooting.md).

## Documentation

- [docs/README.md](docs/README.md) — documentation index
- [docs/getting-started.md](docs/getting-started.md) — install and first run
- [docs/try-it.md](docs/try-it.md) — hands-on walkthrough: a throwaway hub, two agents, a live wake
- [docs/architecture.md](docs/architecture.md) — components and design boundaries
- [docs/api.md](docs/api.md) — CLI, HTTP, MCP, and Python surfaces
- [docs/faq.md](docs/faq.md) — common questions and limitations
- [docs/troubleshooting.md](docs/troubleshooting.md) — symptoms and fixes
- Topic deep dives: [protocol](docs/protocol.md), [triggering](docs/triggering.md), [agent guide](docs/agent_guide.md), [Cursor setup](docs/cursor_agents.md), [orchestrating agents](docs/orchestrating_agents.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, tests, and style.
Report security issues per [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
