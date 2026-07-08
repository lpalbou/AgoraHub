"""Migrate a file-based message mailbox into an Agoria hub, preserving fidelity.

This is a generic importer for a common pattern: a folder of discussion
"threads", each a directory of Markdown messages with YAML frontmatter
(`from`, `to`, `status`, `title`, `in_reply_to`, `date`). It recreates each
thread as an Agoria channel and replays every message as its original author,
so an existing coordination log can continue in the hub.

It discovers the agents and channels from the source — nothing about any
particular project is hard-coded:

- Each thread folder becomes a channel (the leading `NNNN-` prefix is dropped).
- Every distinct `from` value becomes an agent (registered with the admin key).
- The first message's author owns each channel; other participants are invited.
- Messages replay in true chronological order (by `date`), and `in_reply_to`
  is remapped to the new hub message ids so threading is preserved.
- The hub stamps a fresh `created_at`; the original date and source id are kept
  in each message's `data` for audit.

Usage:
    AGORA_URL=http://127.0.0.1:8765 AGORA_ADMIN_KEY=... \
        uv run python examples/migrate_file_mailbox.py /path/to/mailbox

The mailbox path must contain a `threads/` directory. Run against a fresh hub
database so the discovered agent ids and channel names are not already taken.
Set `AGORA_KEYS_FILE` to write the registered agents' keys to a JSON file.
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


@dataclass
class ParsedMessage:
    source_id: str
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
    channel = re.sub(r"^\d+-", "", folder.name)  # strip a leading NNNN- prefix
    msgs = [parse_message(p, channel) for p in folder.glob("*.md")]
    msgs = [m for m in msgs if m is not None]
    msgs.sort(key=lambda m: m.date)  # true chronological order
    return channel, msgs


async def migrate(source_root: Path, hub_url: str, admin_key: str) -> None:
    threads_dir = source_root / "threads"
    if not threads_dir.is_dir():
        raise SystemExit(f"no threads/ directory under {source_root}")
    folders = sorted(p for p in threads_dir.iterdir() if p.is_dir())

    threads = [load_thread(f) for f in folders]

    # Discover agents and per-channel participants/owners from the messages.
    owners: dict[str, str] = {}
    participants: dict[str, set[str]] = {}
    agents: set[str] = set()
    for channel, msgs in threads:
        senders = [m.sender for m in msgs if m.sender != "unknown"]
        if senders:
            owners[channel] = senders[0]
        participants[channel] = {m.sender for m in msgs} | {
            m.to for m in msgs if m.to and m.to != "all"}
        agents |= participants[channel]
    agents = {a for a in agents if a and a != "all"}

    # 1. Register every discovered agent with the admin key.
    keys: dict[str, str] = {}
    async with httpx.AsyncClient(base_url=hub_url,
                                 headers={"Authorization": f"Bearer {admin_key}"}) as admin:
        for agent_id in sorted(agents):
            r = await admin.post("/agents", json={"id": agent_id})
            if r.status_code == 200:
                keys[agent_id] = r.json()["api_key"]
                print(f"registered {agent_id}")
            else:
                raise SystemExit(f"cannot register {agent_id}: {r.status_code} {r.text}\n"
                                 "(use a fresh hub database)")
    if keys_file := os.environ.get("AGORA_KEYS_FILE"):
        Path(keys_file).write_text(json.dumps(keys, indent=2))
        print(f"wrote agent keys -> {keys_file} (keep secret)")

    clients = {a: AgoraClient(hub_url, k) for a, k in keys.items()}

    # 2. Create each channel (owned by its first author) and invite the rest.
    for channel, _ in threads:
        owner_id = owners.get(channel) or next(iter(participants[channel]))
        owner = clients[owner_id]
        await owner.create_channel(channel, private=True)
        await owner.store_set(channel, "channel:meta", {
            "purpose": f"Migrated thread '{channel}'.",
            "language": "plain",
        })
        for member in sorted(participants[channel] - {owner_id}):
            if member in clients:
                invite = await owner.create_invite(channel, member)
                await clients[member].join_channel(channel, invite)
        print(f"created channel '{channel}' (owner {owner_id})")

    # 3. Replay every message chronologically, remapping reply_to as we go.
    remap: dict[str, list[tuple[str, str]]] = {}
    total = 0
    for channel, msgs in threads:
        for msg in msgs:
            if msg.sender not in clients:
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

    print(f"\nDONE: {total} messages across {len(threads)} channels.")
    for c in clients.values():
        await c.close()


def _resolve_reply(remap: dict[str, list[tuple[str, str]]], msg: ParsedMessage) -> str | None:
    """Map a source in_reply_to to the new hub id. If the source id is
    duplicated across authors, pick the candidate whose sender is the person
    this message is addressed to."""
    if not msg.reply_to_src:
        return None
    candidates = remap.get(msg.reply_to_src)
    if not candidates:
        return None
    for sender, new_id in candidates:
        if sender == msg.to:
            return new_id
    return candidates[0][1]


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: migrate_file_mailbox.py <mailbox-dir>  "
                         "(must contain threads/)")
    source = Path(sys.argv[1]).expanduser()
    hub_url = os.environ.get("AGORA_URL", "http://127.0.0.1:8765")
    admin_key = os.environ.get("AGORA_ADMIN_KEY", "")
    if not admin_key:
        raise SystemExit("set AGORA_ADMIN_KEY (the hub's admin key)")
    asyncio.run(migrate(source, hub_url, admin_key))


if __name__ == "__main__":
    main()
