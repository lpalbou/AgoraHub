"""RETIRED: the attache is no longer part of the protocol surface.

Its default delivery commands were session resumes (`codex exec resume`,
`claude -p --resume`, `cursor-agent --resume`), which the protocol now forbids
outright: the agent IS the running session the owner started, and nothing may
spawn or resume sessions on its behalf (constraint C1). The reception
primitive that replaced it is `agora listen` — a listener the session itself
supervises, whose AGORA_WAKE sentinels wake the session through the harness's
own wake surface (Cursor monitored shells, Claude asyncRewake). See
`agora listen --help`.

The Attache class body remains for now (importable, unsupported); the
`agora-attache` entry point only prints the deprecation below.
"""

from __future__ import annotations

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
            # "working" = declared mid-turn. "active" = recent authenticated
            # calls with no push connection — for MCP/REST-only harnesses that
            # IS mid-turn, so treat it the same (review F2): the harness picks
            # these up itself at its next check_inbox boundary; waking would
            # double-deliver. Criticals are the one exception: forced wake.
            # (Honest limit: the attache's own WebSocket makes the hub report
            # connection-derived "idle", which can mask a busy agent.)
            if presence in ("working", "active"):
                print(f"attache: agent {presence}, leaving {len(envelopes)} envelope(s) in inbox")
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
    """Deprecation stub: the attache's job (turn "message arrived" into "the
    agent runs a turn") now belongs to `agora listen`, which wakes the EXISTING
    session instead of resuming/spawning one (forbidden)."""
    print("agora-attache is retired: its delivery commands resumed harness "
          "sessions, which the protocol forbids. Arm `agora listen` inside the "
          "agent's own session instead (see `agora listen --help`).",
          file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
