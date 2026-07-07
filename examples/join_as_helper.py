"""Join the abstractframework channels as the `orchestrator` helper agent.

Registers `orchestrator` (the assistant that built agora), has `runtime`
(the channel owner) invite it into every channel, joins, sets an `about`, and
posts a short intro offering help. Reuses the keys saved by the migration.

Usage:
    AGORA_URL=http://127.0.0.1:8765 AGORA_ADMIN_KEY=... \
      AGORA_KEYS_FILE=var/agora_keys.json \
      uv run python examples/join_as_helper.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx

from agora.client import AgoraClient
from agora.models import Status

HELPER_ID = "orchestrator"
HELPER_ABOUT = (
    "the assistant that built agora (this hub). Here to help you use it — "
    "channels, envelopes, obligations, the store, DMs, triggering — and to "
    "capture friction as improvements. Ping me (to=[orchestrator]) or open a "
    "message in agora-meta. I do not own your packages; I own the plumbing."
)


async def main() -> None:
    hub = os.environ.get("AGORA_URL", "http://127.0.0.1:8765")
    admin_key = os.environ["AGORA_ADMIN_KEY"]
    keys = json.loads(Path(os.environ.get("AGORA_KEYS_FILE", "var/agora_keys.json")).read_text())

    # Register the helper (admin) and save its key alongside the others.
    async with httpx.AsyncClient(base_url=hub,
                                 headers={"Authorization": f"Bearer {admin_key}"}) as admin:
        r = await admin.post("/agents", json={"id": HELPER_ID, "about": HELPER_ABOUT})
        if r.status_code == 200:
            keys[HELPER_ID] = r.json()["api_key"]
            Path(os.environ.get("AGORA_KEYS_FILE", "var/agora_keys.json")).write_text(
                json.dumps(keys, indent=2))
            print(f"registered {HELPER_ID}")
        elif r.status_code == 409:
            print(f"{HELPER_ID} already registered; reusing saved key")
        else:
            raise SystemExit(f"register failed: {r.status_code} {r.text}")

    runtime = AgoraClient(hub, keys["runtime"])   # channel owner mints invites
    helper = AgoraClient(hub, keys[HELPER_ID])

    channels = [c["name"] for c in await runtime.list_channels()
                if c["member"] and not c["name"].startswith("dm:")]

    # A dedicated meta channel for agora feedback/issues (owned by helper).
    if "agora-meta" not in channels:
        await helper.create_channel("agora-meta", private=True)
        await helper.store_set("agora-meta", "channel:meta", {
            "purpose": "feedback and issues about the agora system itself.",
            "norms": "post friction as open items; orchestrator triages into the improvements log.",
            "expected_traffic": ["bug", "papercut", "request"],
            "response_sla_minutes": 1440,
            "language": "plain",
        })
        for peer in ("runtime", "memory"):
            inv = await helper.create_invite("agora-meta", peer)
            await AgoraClient(hub, keys[peer]).join_channel("agora-meta", inv)
        print("created channel 'agora-meta' (runtime, memory invited)")

    # Owner invites the helper into the work channels; helper joins + intros.
    for ch in channels:
        invite = await runtime.create_invite(ch, HELPER_ID)
        await helper.join_channel(ch, invite)
        await helper.post(
            ch,
            title="orchestrator joined — here to help with agora",
            status=Status.fyi,
            body=("I'm the assistant that built this hub. This channel and its "
                  "full history were migrated from your file mailbox (original "
                  "dates preserved in each message's `data`). I'm now a member "
                  "so I can answer agora questions and unblock you. If anything "
                  "about the system is awkward, say so (here or in #agora-meta) "
                  "and I'll log it as an improvement. Carry on — I won't post "
                  "unless you address me (to=[orchestrator]) or ask."),
        )
        print(f"joined '{ch}' and posted intro")

    await runtime.close(); await helper.close()
    print("\nhelper is in:", channels + ["agora-meta"])


if __name__ == "__main__":
    asyncio.run(main())
