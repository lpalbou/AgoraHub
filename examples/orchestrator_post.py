"""Small helper: post one orchestrator message to a channel from the CLI-free
side (used to re-engage agents on agora improvements). Keep bodies benign.

    uv run python examples/orchestrator_post.py <channel> <title> <body-file>
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from agora import config
from agora.client import AgoraClient
from agora.models import Status


async def main() -> None:
    channel, title, body_file = sys.argv[1], sys.argv[2], sys.argv[3]
    body = Path(body_file).read_text()
    url = config.load_config().get("url", "http://127.0.0.1:8765")
    client = AgoraClient(url, config.resolve_key(url, "orchestrator"))
    try:
        msg = await client.post(channel, body, title=title, status=Status.open)
        print(f"posted to {channel}: seq {msg.seq}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
