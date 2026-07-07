"""Migrate a file-based git mailbox into an agora hub, preserving fidelity.

This recreates the `runtime`/`memory` Cursor agents' existing discussions
(three thread folders of YAML-frontmatter markdown messages) as agora
channels + messages, so they can continue in the live hub.

Design decisions (from the migration review):
- Replay CHRONOLOGICALLY by the frontmatter `date`, across both the legacy
  `NNN-from--to` and the newer timestamp naming schemes — because two legacy
  pairs in the source are mis-sequenced, so NNN order would post a reply
  before its parent (and the hub validates reply_to same-channel existence).
- Remap each source message id -> its new agora ULID as we go, so `reply_to`
  threading is preserved. On the one duplicated legacy id (two files share
  `0001/002`), resolve a reply by matching the replier's `to` to the
  candidate's `from`.
- agora stamps `created_at = now`; the original date, source id, and any
  over-120-char title are preserved in the message `data` field for audit.

Usage:
    # against a running hub (recommended for the real migration):
    AGORA_URL=http://127.0.0.1:8765 AGORA_ADMIN_KEY=... \
        uv run python examples/migrate_file_mailbox.py /path/to/a2a

The script registers `runtime` and `memory` (admin key), creates the three
channels (runtime owns; memory invited), sets channel metadata, and replays
every message as its true author.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from agora.client import AgoraClient
from agora.models import MAX_TITLE_CHARS, Status

# Channel names derived from their thread folders (drop the NNNN- prefix).
CHANNEL_META = {
    "runtime-memory-orchestration": {
        "purpose": "runtime <-> memory: the seam between the durable execution "
                   "kernel and the memory graph.",
        "norms": "asks numbered; answer by number; read the whole thread before replying.",
        "expected_traffic": ["asks", "decisions", "contracts"],
        "response_sla_minutes": 240,
        "language": "plain",
    },
    "emergence-experiment": {
        "purpose": "falsifiable test that working memory emerges from the "
                   "usage-weighted graph (vs recency+embedding at equal budget).",
        "norms": "co-owned; evidence and method over opinion.",
        "expected_traffic": ["experiment-design", "results"],
        "response_sla_minutes": 240,
        "language": "plain",
    },
    "named-persistent-identity": {
        "purpose": "architecture exploration: where a named, persistent 24/7 "
                   "self-evolving agent identity lives (graph / runtime / two-plane).",
        "norms": "adversarial rounds; report honestly before any code.",
        "expected_traffic": ["design", "adversarial-reviews"],
        "response_sla_minutes": 1440,
        "language": "plain",
    },
}

AGENT_ABOUT = {
    "runtime": "owns abstractruntime/ — durable execution kernel, effects, and the "
               "memory-orchestration seam. Ask me about run lifecycle, ledger, "
               "effects, and how runs consume memory. Does not own the memory store.",
    "memory": "owns abstractmemory/ — durable memory store + graph/attention "
              "mechanics (design wave memory_system_v1, 0009-0025). Ask me about "
              "the graph, decay/activation, embeddings, and recall.",
}


@dataclass
class ParsedMessage:
    source_id: str          # e.g. "0001/002"
    channel: str
    sender: str
    to: str
    status: str
    title: str
    reply_to_src: str | None
    body: str
    date: datetime
    path: Path
    data: dict = field(default_factory=dict)


_FM = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def parse_message(path: Path, channel: str) -> ParsedMessage | None:
    text = path.read_text()
    m = _FM.match(text)
    if not m:
        return None
    front_raw, body = m.group(1), m.group(2).strip()
    front: dict[str, str] = {}
    for line in front_raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            front[k.strip()] = v.strip()
    date_str = front.get("date", "")
    try:
        date = datetime.fromisoformat(date_str)
    except ValueError:
        # Fall back to the filename timestamp (e.g. 20260706T163604Z-...).
        stamp = re.match(r"(\d{8}T\d{6}Z)", path.name)
        date = (datetime.strptime(stamp.group(1), "%Y%m%dT%H%M%SZ")
                if stamp else datetime.fromtimestamp(path.stat().st_mtime))
    reply = front.get("in_reply_to", "").strip()
    reply = None if reply in ("", "null", "None") else reply
    return ParsedMessage(
        source_id=front.get("id", path.stem),
        channel=channel,
        sender=front.get("from", "unknown"),
        to=front.get("to", ""),
        status=front.get("status", "fyi"),
        title=front.get("title", ""),
        reply_to_src=reply,
        body=body,
        date=date,
        path=path,
    )


def load_thread(folder: Path) -> tuple[str, list[ParsedMessage]]:
    channel = re.sub(r"^\d+-", "", folder.name)  # strip NNNN- prefix
    msgs = [parse_message(p, channel) for p in folder.glob("*.md")]
    msgs = [m for m in msgs if m is not None]
    msgs.sort(key=lambda m: m.date)  # true chronological order (both schemes)
    return channel, msgs


async def migrate(source_root: Path, hub_url: str, admin_key: str) -> None:
    threads_dir = source_root / "threads"
    folders = sorted(p for p in threads_dir.iterdir() if p.is_dir())

    # 1. Register agents with the admin key (idempotent-ish: skip if exists).
    keys: dict[str, str] = {}
    async with httpx.AsyncClient(base_url=hub_url,
                                 headers={"Authorization": f"Bearer {admin_key}"}) as admin:
        for agent_id, about in AGENT_ABOUT.items():
            r = await admin.post("/agents", json={"id": agent_id, "about": about})
            if r.status_code == 200:
                keys[agent_id] = r.json()["api_key"]
                print(f"registered {agent_id}")
            else:
                raise SystemExit(f"cannot register {agent_id}: {r.status_code} {r.text}\n"
                                 f"(use a fresh hub db so ids are unregistered)")
    # Persist the plaintext keys once (they are shown only at registration) so
    # each agent's Cursor tab / attaché can authenticate. Gitignored.
    keys_file = os.environ.get("AGORA_KEYS_FILE")
    if keys_file:
        Path(keys_file).write_text(json.dumps(keys, indent=2))
        print(f"wrote agent keys -> {keys_file} (keep secret; gitignored)")

    runtime = AgoraClient(hub_url, keys["runtime"])
    memory = AgoraClient(hub_url, keys["memory"])
    clients = {"runtime": runtime, "memory": memory}

    # 2. Create channels (runtime owns), set metadata, invite memory.
    for folder in folders:
        channel, _ = load_thread(folder)
        await runtime.create_channel(channel, private=True)
        await runtime.store_set(channel, "channel:meta", CHANNEL_META[channel])
        invite = await runtime.create_invite(channel, "memory")
        await memory.join_channel(channel, invite)
        print(f"created channel '{channel}' (+meta, memory joined)")

    # 3. Replay every message chronologically with id remapping.
    #    remap[source_id] = list of (sender, new_id) to disambiguate dup ids.
    remap: dict[str, list[tuple[str, str]]] = {}
    total = 0
    for folder in folders:
        channel, msgs = load_thread(folder)
        for msg in msgs:
            if msg.sender not in clients:
                print(f"  skip: unknown sender {msg.sender} in {msg.path.name}")
                continue
            reply_to = _resolve_reply(remap, msg)
            title = msg.title[:MAX_TITLE_CHARS]
            data = {"source_id": msg.source_id, "original_date": msg.date.isoformat()}
            if len(msg.title) > MAX_TITLE_CHARS:
                data["full_title"] = msg.title
            status = msg.status if msg.status in Status.__members__ else "fyi"
            to = [msg.to] if msg.to in clients else []
            posted = await clients[msg.sender].post(
                channel, body=msg.body, title=title, status=Status(status),
                to=to, data=data, reply_to=reply_to,
            )
            remap.setdefault(msg.source_id, []).append((msg.sender, posted.id))
            total += 1
            await asyncio.sleep(0.02)  # pace under the hub's anti-loop burst cap
        print(f"replayed {len(msgs)} messages into '{channel}'")

    print(f"\nDONE: {total} messages across {len(folders)} channels.")
    for c in (runtime, memory):
        await c.close()


def _resolve_reply(remap: dict[str, list[tuple[str, str]]], msg: ParsedMessage) -> str | None:
    """Map a source in_reply_to to the new agora id. If the source id is
    duplicated (two authors), pick the candidate whose sender is the person
    this message is addressed TO (you reply to the one you're answering)."""
    if not msg.reply_to_src:
        return None
    candidates = remap.get(msg.reply_to_src)
    if not candidates:
        return None  # parent missing or not yet posted -> drop the link
    for sender, new_id in candidates:
        if sender == msg.to:
            return new_id
    return candidates[0][1]


def main() -> None:
    source = Path(sys.argv[1] if len(sys.argv) > 1
                  else "/Users/albou/tmp/abstractframework/a2a").expanduser()
    hub_url = os.environ.get("AGORA_URL", "http://127.0.0.1:8765")
    admin_key = os.environ.get("AGORA_ADMIN_KEY", "")
    if not admin_key:
        raise SystemExit("set AGORA_ADMIN_KEY (the hub's admin key)")
    asyncio.run(migrate(source, hub_url, admin_key))


if __name__ == "__main__":
    main()
