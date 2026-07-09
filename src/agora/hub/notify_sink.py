"""Hub-written notify files: liveness without resident processes.

The file mailbox never had a liveness problem because the file was maintained
by the same thing that stored the data. This is agora's equivalent: the hub —
the one process that must exist anyway — appends one JSON line per delivered
message to `<notify_dir>/<agent>-inbox.log` for every member of the message's
channel. No watcher processes, no supervisors, no OS services: an agent (or
its harness) just tails its file, which is fresh for exactly as long as the
hub is up — and if the hub is down there is nothing to be notified about.

`agora watch` still exists for remote clients (a file on the hub's machine is
useless to them), but on the hub's own machine it is now redundant — and
running one against the same file would duplicate lines.

Best-effort by design: a failed file write must never fail a post.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from ..models import Envelope


def notify_line(envelope: Envelope) -> str:
    """One compact JSON line per message — the same shape `agora watch` emits,
    so existing tailers keep working unchanged. `kind` lets a tailer filter
    fs/system audit traffic without parsing titles."""
    flags = ",".join(f for f, on in [
        ("critical", envelope.critical), ("escalated", envelope.escalated),
        ("to-me", envelope.to_me), ("reply-to-me", envelope.reply_to_me),
        (envelope.status.value, envelope.status.value in ("open", "blocked")),
    ] if on)
    preview = (envelope.body or "")[:200]
    return json.dumps({
        "channel": envelope.channel, "seq": envelope.seq,
        "from": envelope.sender, "id": envelope.id,
        "kind": envelope.kind.value,
        "status": envelope.status.value, "title": envelope.title,
        "flags": flags, **({"preview": preview} if preview else {}),
    })


class NotifySink:
    """Appends viewer-specific envelope lines to per-agent notify files."""

    def __init__(self, notify_dir: str | Path) -> None:
        self._dir = Path(notify_dir).expanduser()
        self._lock = threading.Lock()  # posts come from worker threads
        self._failing = False  # log the first failure of a streak, not each one

    def deliver(self, agent_id: str, envelope: Envelope) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            line = notify_line(envelope)
            with self._lock, open(self._dir / f"{agent_id}-inbox.log", "a") as fh:
                fh.write(line + "\n")
            if self._failing:
                self._failing = False
                print("agora: notify-file writes recovered", file=sys.stderr)
        except OSError as exc:
            # Best-effort by contract: never fail a post over a notify write.
            # But a silently stale file is the old "deaf agent" failure mode,
            # so the FIRST failure of a streak is logged (disk full or a
            # permissions regression would otherwise be invisible; audit H1).
            if not self._failing:
                self._failing = True
                print(f"agora: notify-file write failed ({exc}); posts are "
                      "unaffected but notify files are stale until this "
                      "recovers", file=sys.stderr)
