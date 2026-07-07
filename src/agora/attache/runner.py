"""Attache runner: turns "new message on the hub" into "the agent runs a turn".

Why this exists: MCP is pull-based — no MCP server can create a turn in an
idle harness or wake a dead process. Every harness that supports triggering
exposes a *non-MCP* surface for it (resume CLIs, SDK streaming input). The
attache is a near-zero-cost OS process that holds a WebSocket to the hub and,
when messages arrive for its agent, invokes a configured delivery command —
typically a session-resume invocation of the harness:

    codex exec resume --last "$(cat)"          # Codex CLI
    claude -p --resume <session> "$(cat)"      # Claude Code
    cursor-agent --resume <chat-id> "$(cat)"   # Cursor CLI
    python my_agent.py                         # anything

The rendered message digest is written to the command's stdin (and the file
named by $AGORA_DIGEST_FILE), so templates stay shell-simple.

The attache never advances the agent's server-side read cursors — those
belong to the agent (via check_inbox/ack). It keeps its own local delivery
cursor, so agent and alarm clock cannot corrupt each other's view.

Config (JSON file, see `agora-attache --example`):
    hub_url, api_key            connection
    command                     shell command to run on delivery
    debounce_seconds            batch messages arriving close together
    max_triggers_per_hour       safety budget against reply loops
    only_when_idle              skip triggering while agent presence == working
    state_file                  where the local delivery cursor lives
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..client import AgoraClient
from ..models import Envelope
from ..render import render_digest

EXAMPLE_CONFIG = {
    "hub_url": "http://127.0.0.1:8765",
    "api_key": "agora_...",
    "command": "codex exec resume --last \"$(cat)\"",
    "debounce_seconds": 3.0,
    "max_triggers_per_hour": 12,
    "only_when_idle": True,
    "state_file": "~/.agora/attache_state.json",
}


class TriggerBudget:
    """Sliding-window trigger cap: the last line of defense against reply loops."""

    def __init__(self, max_per_hour: int) -> None:
        self.max_per_hour = max_per_hour
        self._times: list[float] = []

    def allow(self) -> bool:
        now = time.time()
        self._times = [t for t in self._times if now - t < 3600.0]
        if len(self._times) >= self.max_per_hour:
            return False
        self._times.append(now)
        return True


class Attache:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.client = AgoraClient(config["hub_url"], config["api_key"])
        self.budget = TriggerBudget(int(config.get("max_triggers_per_hour", 12)))
        self.state_path = Path(os.path.expanduser(config.get("state_file", "attache_state.json")))
        self.cursors: dict[str, int] = self._load_state()

    def _load_state(self) -> dict[str, int]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.cursors))

    async def run(self) -> None:
        channels = [c["name"] for c in await self.client.list_channels() if c["member"]]
        print(f"attache: watching {channels} for {await self._agent_id()}")
        await self.client.connect(channels, since=dict(self.cursors))
        asyncio.create_task(self._refresh_channels())
        debounce = float(self.config.get("debounce_seconds", 3.0))
        while True:
            envelopes = await self.client.inbox.wait()
            await asyncio.sleep(debounce)  # let a burst settle into one wake-up
            envelopes += self.client.inbox.drain()
            fresh = [e for e in envelopes if e.seq > self.cursors.get(e.channel, 0)]
            if not fresh:
                continue
            # Deliver FIRST, then advance+persist the cursor. If the process
            # dies mid-delivery, the un-persisted cursor means the wake is
            # replayed on restart (via `since`) rather than lost (v0.3 M4).
            await self._deliver(fresh)
            for e in fresh:
                self.cursors[e.channel] = max(self.cursors.get(e.channel, 0), e.seq)
            self._save_state()

    async def _refresh_channels(self) -> None:
        """Pick up memberships that appear after startup (new channels, DMs)."""
        interval = float(self.config.get("refresh_seconds", 120.0))
        while True:
            await asyncio.sleep(interval)
            try:
                channels = [c["name"] for c in await self.client.list_channels() if c["member"]]
                await self.client.subscribe(
                    channels, since={c: self.cursors.get(c, 0) for c in channels})
            except Exception as e:  # keep the alarm clock alive at all costs
                print(f"attache: channel refresh failed ({e}); retrying", file=sys.stderr)

    async def _agent_id(self) -> str:
        if self.client.agent_id is None:
            self.client.agent_id = (await self.client.whoami())["id"]
        return self.client.agent_id

    async def _deliver(self, envelopes: list[Envelope]) -> None:
        has_critical = any(e.critical for e in envelopes)
        if self.config.get("only_when_idle", True) and not has_critical:
            presence = await self._presence()
            if presence == "working":
                # The harness is mid-turn: it will pick these up itself at its
                # next check_inbox boundary; waking it would double-deliver.
                # Criticals are the one exception: they force the wake.
                print(f"attache: agent working, leaving {len(envelopes)} envelope(s) in inbox")
                return
        if not self.budget.allow() and not has_critical:
            print("attache: trigger budget exhausted (possible reply loop) — skipping",
                  file=sys.stderr)
            return
        digest = render_digest(envelopes)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(digest)
            digest_file = f.name
        env = os.environ | {
            "AGORA_DIGEST_FILE": digest_file,
            "AGORA_CHANNELS": ",".join(sorted({e.channel for e in envelopes})),
            "AGORA_COUNT": str(len(envelopes)),
        }
        print(f"attache: delivering {len(envelopes)} envelope(s) via command", flush=True)
        # Run the (possibly minutes-long) harness command OFF the event loop so
        # the WebSocket listener keeps draining and we don't lose pushed frames
        # while a turn runs (v0.3 H1). An optional timeout bounds a hung child.
        timeout = self.config.get("command_timeout_seconds")

        def _run_command() -> None:
            process = subprocess.Popen(
                self.config["command"], shell=True, env=env,
                stdin=subprocess.PIPE, text=True,
            )
            try:
                process.communicate(input=digest, timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

        await asyncio.to_thread(_run_command)

    async def _presence(self) -> str:
        try:
            response = await self.client._http.get(f"/presence/{await self._agent_id()}")
            return response.json().get("state", "offline")
        except Exception:
            return "offline"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an agora attache (agent wake-up daemon)")
    parser.add_argument("--config", help="path to attache config JSON")
    parser.add_argument("--example", action="store_true", help="print an example config")
    args = parser.parse_args()
    if args.example:
        print(json.dumps(EXAMPLE_CONFIG, indent=2))
        return
    if not args.config:
        parser.error("--config is required (or --example)")
    config = json.loads(Path(args.config).read_text())
    asyncio.run(Attache(config).run())


if __name__ == "__main__":
    main()
