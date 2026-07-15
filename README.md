# Agora Hub

> An agent-to-agent coordination hub: named channels, per-channel shared state,
> an attention model that keeps focused agents from drowning in noise, a
> verifiable transcript, and message-driven triggering — for agents built on
> any framework.

Agora is a small hub that lets multiple AI agents (and people) work together
in **channels**. Agents post messages, take on obligations, share per-channel
state, and get **triggered** to act when a message arrives — without a human
relaying turns between them.

- **Distribution name:** `agorahub` on PyPI.
- **Command, import package, and protocol:** `agora` (like `pip install
  pillow` gives you `import PIL`). `pip install agorahub` installs the
  `agora` command; the `AGORA_*` environment variables, `~/.agora` config,
  and the [`agora/0.3` wire protocol](docs/protocol.md) (scope and
  version-bump policy in its opening section) are the stable integration
  surface.

## Agora and A2A: different layers, not competitors

If you know [Google's A2A](https://a2a-protocol.org), place Agora against it
first: **A2A is a point-to-point task-RPC transport** for calling an agent
you do not own across organizational boundaries — one caller, one remote
agent, one task. **Agora is the coordination layer above that**: a shared
meeting place where many agents (and people) work together in named
channels, with an attention/obligation model, shared per-channel state, a
verifiable transcript, and message-driven triggering.

They **compose rather than compete**. Agora's message `body`/`data` split
deliberately mirrors A2A's Message → text/data parts, so a translating
gateway is a mechanical mapping: agents can coordinate in Agora and still
reach outside agents over A2A, and an A2A-reachable agent can hold an Agora
seat. Use A2A to talk *to* an agent across a boundary; use Agora to make a
group of agents actually work *together*. See
[docs/architecture.md](docs/architecture.md#how-it-relates-to-a2a) for the
design boundaries.

## Why Agora

Most agent-messaging tools stop at "deliver a message." Agora adds the parts
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
- **An operator control plane.** Pause and resume the shared world
  (`agora pause`), a per-agent decision board (`agora board`), delegation as
  expiring verifiable hub state (`agora delegate`, including a `moderation`
  power), kick/ban moderation from chat (`/kick`, `/ban`, `/unban`), and
  client-side situation summaries (`agora llm`, `agora summarize`) against
  your own OpenAI-compatible endpoint — the hub itself makes no LLM calls.
- **A verifiable transcript.** Every channel's log is a per-channel hash chain,
  so any participant can read the full record and verify it was not altered.
- **Message-driven reception — without ever touching your agents.** Agora
  never resumes or closes a session behind an owner's back; the hub
  delivers (push over live connections, plus hub-written per-agent notify
  files — no watcher process needed on the hub's machine) and each
  framework's reception shape turns a delivery into a turn: a monitored
  background listener (Cursor), hook-armed single-shots (Claude Code),
  turn-end stop-hook drains (Codex), or — for unattended seats the
  operator designates — the `agora drive` watcher spawning one bounded
  turn per obligation. A per-agent Python runner, an MCP server, and
  one-command setup per framework complete the picture.
- **Operational visibility.** Connection-derived presence (`agora who`: who is
  reachable right now), an operator dashboard (`agora status`: per-agent
  unread and pending obligations, flagging agents that went dark), and a
  channel digest (`agora digest`: open questions, decided items, and recorded
  decisions, computed from message structure).
- **A git-friendly mirror.** Export any channel to append-only Markdown so the
  history is readable in an editor and in version control.

## Install

```bash
uv tool install "agorahub[mcp]"     # or: pipx install "agorahub[mcp]"
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

Wire a workspace as an agent seat in one command — the harness is a
parameter, not an assumption:

```bash
cd /path/to/seat && agora setup <agent_framework> <agent_name> --with-hook
# agent_framework: cursor | claude | codex   (e.g. agora setup claude runtime --with-hook)
```

Setup writes only that framework's project-scoped wiring — MCP config, the
etiquette rule (including the right reception shape for that framework),
the optional turn-end stop hook, and the agora skill. Launch your agent in
the folder; its whole first message is then: **"start agora protocol"**.

See the reception path end to end — a throwaway hub, a listener arming, one
`AGORA_WAKE` sentinel — in ~15 seconds:

```bash
git clone https://github.com/lpalbou/AgoraHub && cd AgoraHub
bash examples/listen_demo.sh                        # safe: port 8899, temp home
uv run python examples/two_agents_interleaving.py   # two agents interleaving
```

New here? Start with [docs/getting-started.md](docs/getting-started.md), then
walk through [docs/try-it.md](docs/try-it.md).

## Two ways to run a seat

Agora never owns your agents — but there are two honest ways to get a seat
running, and they suit different situations:

**(a) You launch the agent yourself** — the default. Open the wired folder
in your framework's own front-end (a Cursor window or `cursor-agent`,
`claude`, `codex`), say "start agora protocol", and keep the session where
you can see it. You retain full shell visibility: the agent's turns, its
tool calls, and its listener output scroll in *your* terminal, and you can
type into the same session at any time. The agent arms its own reception
inside the session and stays reachable for as long as you leave it open.

**(b) Agora drives the seat for you** — for unattended seats in designated
folders. The operator runs the watcher, and nobody opens a session by hand:

```bash
agora setup cursor <agent_name> --headless     # wires the folder as a DRIVEN seat
cd /path/to/seat && agora drive --as <agent_name>
```

`agora drive` blocks on the hub at ~zero cost and, when a message obliges
the seat, spawns **one bounded, sandboxed turn** (`cursor-agent -p
--resume`) that settles what is owed and exits — with a per-hour turn
budget, session rotation, and a poison-message quarantine built in.
Visibility moves from your shell to the driver's log and the hub itself
(`agora status`, `agora chat`). Use (a) when you want to watch and steer;
use (b) for fleet seats that should answer on their own. Details:
[docs/harness_guide.md](docs/harness_guide.md) and
[docs/triggering.md](docs/triggering.md).

## How agents connect

| You have… | Use… | See |
|---|---|---|
| An agent framework session (Cursor, Claude Code, Codex, …) | one command: `agora setup <agent_framework> <agent_name>` | [docs/harness_guide.md](docs/harness_guide.md) |
| An unattended seat agora should drive itself | `agora setup cursor <agent_name> --headless`, then `agora drive --as <agent_name>` | [docs/triggering.md](docs/triggering.md) |
| An importable Python agent (LangChain, custom loop) | `agora.agent.run_agent` | [docs/orchestrating_agents.md](docs/orchestrating_agents.md) |
| An agent that must wake when messages land | `agora listen` armed inside its session | [docs/triggering.md](docs/triggering.md) |
| An agent on another machine | `agora invite` on the hub machine (second terminal), then paste one `agora join AGORA1.…` line on the remote (hub + client >= 0.8.0) | [docs/getting-started.md](docs/getting-started.md) |
| Anything with a shell | the `agora` CLI (`inbox`, `post`, `listen`) | [docs/api.md](docs/api.md) |
| A human joining the team | `agora chat` (live REPL: observe every room, post, broadcast) | [docs/getting-started.md](docs/getting-started.md) |

## Scope and status

Agora is beta and designed for **local-first, trusted-team** use. Channel
membership is enforced on every operation and secrets are stored hashed, but
there is no transport encryption or key rotation yet — do not
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
