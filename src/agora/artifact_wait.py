"""Owner-declared, all-of artifact thresholds; no content acceptance implied."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


MAX_ARTIFACT_WAITS = 64


def declaration_signature(value: dict) -> str:
    # Progress prose/CAS versions are deliberately absent: a peer comment or
    # unrelated owner update must not re-arm the same completed reconsideration.
    # Public claim reads already project valid legacy source references to
    # source_message_id. Unlinked source prose is peer-editable context, so
    # hashing it would let a peer re-arm the owner's consumed declaration.
    identity = [value.get("owner"), value.get("source_message_id"),
                value.get("waiting_for_artifacts")]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


class ArtifactResumeReceipts:
    """One last completed declaration per claim, private to this driver seat.

    Written after a successful work turn: a process crash before that write
    can replay a reconsideration. This is not exactly-once task execution.
    """
    def __init__(self, home: Path, agent: str, hub: str, warn):
        scope = hashlib.sha256(json.dumps([hub.rstrip("/"), agent]).encode()).hexdigest()[:24]
        self.path = home / f"artifact-resumes-{scope}.json"
        self.warn = warn
        self.warned = False
        try:
            self.done = json.loads(self.path.read_text())
            if not isinstance(self.done, dict) or any(not isinstance(v, str) for v in self.done.values()):
                raise ValueError("invalid artifact resume receipts")
        except FileNotFoundError:
            self.done = {}
        except (OSError, ValueError):
            # Preserve reachability after corrupted local state. The warning
            # exposes that duplicate suppression across this restart is lost.
            self.done = {}
            self._warning()

    def _warning(self):
        if not self.warned:
            self.warned = True
            self.warn(f"AGORA_DRIVE warn=artifact-resume-receipts-unavailable path={self.path}")

    def seen(self, channel: str, key: str, signature: str) -> bool:
        return self.done.get(json.dumps([channel, key])) == signature

    def record(self, channel: str, key: str, signature: str):
        self.done[json.dumps([channel, key])] = signature
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".artifact-resume-", dir=self.path.parent)
            with os.fdopen(fd, "w") as stream:
                json.dump(self.done, stream, sort_keys=True)
            os.replace(temporary, self.path)
        except OSError:
            self._warning()
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
