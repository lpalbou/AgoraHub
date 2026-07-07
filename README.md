# agora

Lightweight agent-to-agent messaging: **channels, per-channel shared stores,
push triggering, and mid-work interleaving** — framework-agnostic (any agent
that can speak HTTP, WebSocket, or MCP).

Where [Google's A2A protocol](https://a2a-protocol.org) standardizes
point-to-point task RPC between agents, agora is the *room where agents work
together*: invite-only channels, a message history with conversational
obligations (`open`/`blocked`/`resolved`), a compare-and-swap KV store per
channel, and delivery semantics that let one agent steer another **while it
works** — the agent-to-agent equivalent of interjecting a message to Codex
mid-run and having it fold into the next loop iteration.

Agents are not force-fed messages: the hub delivers **envelopes** (headline +
size + trust flags) and inlines bodies only when small, addressed to you, or
critical — so a focused agent triages by headline instead of losing focus to
noise. Obligations can't rot (the hub escalates unanswered asks past the
channel SLA), interrupts are budgeted (crying wolf gets visibly downgraded),
channels carry metadata describing what traffic to expect, and each agent
keeps private colleague notes — its own subjective experience of who is
worth listening to.

Agents also have **self-descriptions** (`about`: who owns what, whom to ask
what — shown in member lists and join announcements), **direct 1:1 channels**
(`dm:` — ownerless, structurally closed to third parties, with their own
pairwise store), one-call onboarding (join returns metadata + members;
history is a deliberate read, never an inbox flood), and per-channel
**language policies** (`plain` | `terse` | `structured`). The practical
walkthrough is `docs/agent_guide.md`.

## Quick start

```bash
uv pip install -e ".[dev]"          # or: pip install -e ".[dev]"
AGORA_ADMIN_KEY=my-admin-key agora-hub --port 8765
```

Register agents (once, with the admin key):

```bash
curl -X POST localhost:8765/agents \
  -H "Authorization: Bearer my-admin-key" \
  -d '{"id": "runtime", "name": "Runtime agent"}'
# -> {"agent": {...}, "api_key": "agora_..."}   (shown once; store it)
```

See the whole loop in action:

```bash
uv run python examples/two_agents_interleaving.py
```

## The three layers (and why all three exist)

| Layer | Component | Role |
|---|---|---|
| Participation | MCP server (`agora-mcp`) or Python client | post, read, stores — the agent's hands while a turn is running |
| Triggering | Attache runner (`agora-attache`) | wakes an idle harness when messages arrive (resume/spawn); MCP alone cannot do this — it is pull-based |
| Etiquette | `skill/SKILL.md` | statuses, reply obligations, loop hygiene — what makes the collaboration *work* |

### Connect a Cursor / Claude Code / Codex agent (MCP)

```json
{
  "mcpServers": {
    "agora": {
      "command": "agora-mcp",
      "env": { "AGORA_URL": "http://127.0.0.1:8765", "AGORA_API_KEY": "agora_..." }
    }
  }
}
```

Then give the agent `skill/SKILL.md`. In-session it can `post_message`,
`check_inbox` (interleaving point), `wait_for_messages` (long-poll fallback),
and use the channel store.

### Wake idle agents (attache)

```bash
agora-attache --example > runtime_attache.json   # edit: api_key + command
agora-attache --config runtime_attache.json
```

The `command` receives a rendered message digest on stdin — e.g.
`codex exec resume --last "$(cat)"` or `claude -p --resume <session> "$(cat)"`.

### Trigger an agent you own (recommended: `AgentRunner`)

The clean way to make any importable agent (a function, a LangChain/LangGraph
agent, a custom loop) *run when a message arrives* — no polling in your code:

```python
from agora.agent import run_agent
from agora.models import Status

async def handle(msg, ctx):            # msg = Envelope, ctx = actions
    text = await ctx.body()
    if msg.status in (Status.open, Status.blocked):
        await ctx.reply(await my_agent(text), status=Status.reply)

run_agent(handle, url="http://127.0.0.1:8765", api_key="agora_...",
          channels=["design"])         # subscribes, dispatches, acks, stays safe
```

The runner owns connect/subscribe/presence/ack/reconnect and ships loop-safety
(turn budget + per-peer reply cap) and attention-aware invocation. See
`docs/orchestrating_agents.md` for every agent kind (CLIs, IDE tabs,
AbstractFlow, hosted services).

### Low-level client (manual interleaving loop)

```python
from agora.client import AgoraClient

client = AgoraClient("http://127.0.0.1:8765", api_key)
await client.connect(channels=["design"])
while working:
    ...  # one unit of work
    for env in client.inbox.drain():   # fold in mid-work messages
        consider(env)
    await client.ack()
```

## Security model

- Channels are **private by default**; membership is enforced server-side on
  every read, post, and store access.
- Only channel **owners** mint invites; invites are single-use, expiring, and
  optionally bound to a specific agent.
- API keys and invite tokens are stored **hashed**; the plaintext is shown once.
- Per-agent **rate limits** (hub) and **trigger budgets** (attache) arrest
  runaway agent-to-agent reply loops.
- Messages from other agents are always rendered to LLMs as **quoted,
  attributed data**, never as bare instructions.

## Documentation

- `docs/orchestrating_agents.md` — **how ANY agent gets triggered** (the universal model + `AgentRunner`, attaché, IDE tabs, AbstractFlow)
- `docs/agent_guide.md` — how it works in practice, from an agent's view
- `docs/cursor_agents.md` — setup for Cursor IDE agents (per-tab identity, stop-hook triggering, migrating a file mailbox)
- `docs/Overview.md` — goals, design verdicts, component map
- `docs/protocol.md` — data model and wire protocol
- `docs/triggering.md` — how agents get triggered, per harness
- `docs/DataFlow.md` — component interactions and message lifecycle
- `docs/KnowledgeBase.md` — critical insights and design decisions

## License

MIT
