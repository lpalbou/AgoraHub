"""Live monitor: watch all of an agent's channels and print NEW messages.

Uses the WebSocket push stream (not polling), so it prints each message the
moment it lands. Deduped and reconnecting via the client. Intended for the
`orchestrator` helper to keep an eye on the hub and surface anything that
needs attention (obligations, criticals, agora friction).

    AGORA_MONITOR_AS=orchestrator uv run python examples/monitor_channels.py
"""

from __future__ import annotations

import asyncio
import os

from agora import config
from agora.client import AgoraClient


async def main() -> None:
    agent = os.environ.get("AGORA_MONITOR_AS", "orchestrator")
    url = config.load_config().get("url", "http://127.0.0.1:8765")
    key = config.resolve_key(url, agent)
    client = AgoraClient(url, key)

    channels = [c["name"] for c in await client.list_channels() if c["member"]]
    # connect() without `since` => only messages from now on land in the inbox
    # (no history replay), so every line printed is genuinely new.
    await client.connect(channels)
    await client.set_presence("idle")
    print(f"MONITOR {agent} watching {len(channels)} channels: {channels}", flush=True)

    try:
        while True:
            for e in await client.inbox.wait(timeout=3600):
                flags = []
                if e.critical:
                    flags.append("CRITICAL")
                if e.escalated:
                    flags.append("ESCALATED")
                if e.status.value in ("open", "blocked"):
                    flags.append(e.status.value)
                if e.to_me:
                    flags.append("to-orchestrator")
                tag = f"  [{','.join(flags)}]" if flags else ""
                title = e.title or "(no title)"
                print(f"NEW [{e.channel}#{e.seq}] from={e.sender}: {title}{tag}", flush=True)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
