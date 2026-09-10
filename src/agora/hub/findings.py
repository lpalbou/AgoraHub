"""Small, opt-in finding-integration grammar.

Generic ``finding:*`` rows predate this contract and remain free-form.  A row
participates only when it carries this discriminator and a task reference.
The service owns authorization, reference resolution, and hub stamps.
"""

from __future__ import annotations

import re
from typing import Any

KIND = "task-finding-v1"
PREFIX = "finding:"
DISPOSITIONS = frozenset({"incorporated", "merged", "superseded", "rejected"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def key_for(task_key: str, finding_id: str) -> str:
    """Canonical typed finding key, or ``""`` when the spelling is invalid."""
    if not task_key.startswith("task:") or not _ID.fullmatch(finding_id):
        return ""
    return f"{PREFIX}{task_key[5:]}:{finding_id}"


def is_typed(key: str, value: Any) -> bool:
    return key.startswith(PREFIX) and isinstance(value, dict) and value.get("kind") == KIND


def task_ref(value: dict[str, Any]) -> tuple[str, str] | None:
    task = value.get("task")
    if not isinstance(task, dict) or set(task) != {"channel", "key"}:
        return None
    channel, key = task.get("channel"), task.get("key")
    if not isinstance(channel, str) or not isinstance(key, str) or not key.startswith("task:"):
        return None
    return channel, key
