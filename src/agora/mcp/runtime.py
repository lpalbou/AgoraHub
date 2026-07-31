"""Runtime contract shared by setup, the MCP entry point, and drivers.

The project config describes an interactive harness session.  A dedicated
driver additionally needs a deterministic, non-secret binding that works in
an unattended process without weakening the harness's project-trust model.
This module owns that small boundary: locate the installed ``agora-mcp``
entry point, probe the exact server API it exposes, and describe one seat's
MCP environment without carrying its bearer key.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import __version__ as AGORA_VERSION


MCP_SELF_CHECK_FLAG = "--self-check"
MCP_SELF_CHECK_COMPONENT = "agora-mcp"
SUPPORTED_MCP_SDK = ">=1.28.1,<2"

# Keep runtime probing independent from harness-spawn injection.  Tests and
# embedders legitimately replace ``agora.drive.subprocess.run``; because
# ``subprocess`` is a shared module object, calling its mutable attribute here
# would accidentally turn an MCP self-check into a fake harness turn.
_run_subprocess = subprocess.run


def supports_mcp_sdk(version: str) -> bool:
    """Whether *version* satisfies the API contract implemented by Agora."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    return bool(
        match
        and (tuple(map(int, match.groups())) >= (1, 28, 1) and int(match.group(1)) < 2)
    )


@dataclass(frozen=True)
class MCPRuntimeProbe:
    ok: bool
    command: str
    agora_version: str | None = None
    sdk_version: str | None = None
    reason: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class MCPBinding:
    """Non-secret configuration for one driven seat's MCP subprocess."""

    command: str
    agent_id: str
    url: str
    home: Path
    about: str = ""
    download_dir: str | None = None

    def environment(self) -> dict[str, str]:
        # The MCP server reads the bearer key from this home's 0600 key cache.
        # Never put it in Codex argv or the model subprocess environment.
        env = {
            # Explicit empties override legacy trusted project config that may
            # still contain a bearer. The server treats an empty value as
            # absent and reads the seat key from the 0600 cache instead.
            "AGORA_API_KEY": "",
            "AGORA_ADMIN_KEY": "",
            "AGORA_URL": self.url.rstrip("/"),
            "AGORA_AGENT_ID": self.agent_id,
            "AGORA_HOME": str(self.home.resolve()),
            "AGORA_ABOUT": self.about,
        }
        if self.download_dir:
            env["AGORA_DOWNLOAD_DIR"] = str(
                Path(self.download_dir).expanduser().resolve()
            )
        return env


def resolve_mcp_command() -> str:
    """Return a compatible MCP entry point, preferring the running Agora.

    An ``agora`` console script and its sibling ``agora-mcp`` are one installed
    unit, so that sibling is authoritative.  Source runners such as ``pytest``
    and ``python -m`` are different: their executable directory and PATH can
    contain several Agora installations.  In that case, select the first
    executable candidate that passes the real MCP self-check instead of
    failing on an unrelated stale installation that happens to appear first.
    """

    argv0 = Path(sys.argv[0]).expanduser()
    if not argv0.is_absolute() and argv0.parent == Path("."):
        located = shutil.which(str(argv0))
        argv0 = Path(located) if located else argv0
    resolved_argv0 = argv0.resolve()
    console_sibling = resolved_argv0.parent / "agora-mcp"
    if (
        resolved_argv0.name in {"agora", "agora-mcp"}
        and console_sibling.is_file()
        and os.access(console_sibling, os.X_OK)
    ):
        return str(console_sibling.resolve())

    candidates: list[str] = []

    def add(path: Path) -> None:
        if not path.is_file() or not os.access(path, os.X_OK):
            return
        value = str(path.resolve())
        if value not in candidates:
            candidates.append(value)

    # An unrelated runner can still share the intended environment (for
    # example .venv/bin/pytest), but unlike the Agora-console case above its
    # sibling must prove compatibility before it is selected.
    add(console_sibling)

    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if folder:
            add(Path(folder).expanduser() / "agora-mcp")

    # ``python -m`` may run with a deliberately narrow PATH. Its interpreter
    # sibling is therefore the final candidate, subject to the same probe.
    interpreter_sibling = Path(sys.executable).resolve().parent / "agora-mcp"
    add(interpreter_sibling)

    for candidate in candidates:
        # Self-check is local and side-effect-free; a candidate that cannot
        # answer promptly is not suitable for an unattended MCP launch.
        probe = probe_mcp_runtime(candidate, timeout=5.0)
        if probe.ok:
            return probe.command
    if candidates:
        # Preserve the highest-precedence failure for the caller's actionable
        # preflight diagnostic when no compatible runtime exists anywhere.
        return candidates[0]
    return "agora-mcp"


def probe_mcp_runtime(command: str, *, timeout: float = 15.0) -> MCPRuntimeProbe:
    """Execute the real entry point's side-effect-free runtime self-check."""

    resolved = command
    if not Path(command).is_file():
        resolved = shutil.which(command) or ""
    if not resolved:
        return MCPRuntimeProbe(
            ok=False,
            command=command,
            reason="command-not-found",
            detail=f"{command!r} is not executable on PATH",
        )
    resolved = str(Path(resolved).resolve())
    try:
        done = _run_subprocess(
            [resolved, MCP_SELF_CHECK_FLAG],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            # A self-check needs executable/runtime state only. Candidate
            # binaries discovered while repairing a split PATH must never see
            # a seat bearer, admin key, identity, or hub address.
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("AGORA_")
            },
        )
    except subprocess.TimeoutExpired:
        return MCPRuntimeProbe(
            ok=False,
            command=resolved,
            reason="self-check-timeout",
            detail=f"runtime self-check exceeded {timeout:g}s",
        )
    except OSError as exc:
        return MCPRuntimeProbe(
            ok=False,
            command=resolved,
            reason="spawn-failed",
            detail=str(exc),
        )

    payload = None
    for line in (done.stdout or "").splitlines():
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(candidate, dict)
            and candidate.get("component") == MCP_SELF_CHECK_COMPONENT
        ):
            payload = candidate
            break
    if done.returncode == 0 and payload and payload.get("status") == "ok":
        reported_agora = str(payload.get("agora") or "")
        if reported_agora != AGORA_VERSION:
            return MCPRuntimeProbe(
                ok=False,
                command=resolved,
                agora_version=reported_agora or None,
                sdk_version=str(payload.get("mcp_sdk") or "") or None,
                reason="runtime-version-mismatch",
                detail=(
                    f"agora-mcp reports Agora {reported_agora or 'unknown'}, "
                    f"but the caller is Agora {AGORA_VERSION}"
                ),
            )
        version = payload.get("mcp_sdk")
        return MCPRuntimeProbe(
            ok=True,
            command=resolved,
            agora_version=reported_agora,
            sdk_version=str(version) if version else None,
        )

    raw_detail = (done.stderr or done.stdout or "").strip()
    detail = (
        raw_detail.splitlines()[-1]
        if raw_detail
        else ("entry point did not return the Agora MCP self-check marker")
    )
    reason = "runtime-incompatible" if done.returncode else "invalid-self-check"
    return MCPRuntimeProbe(
        ok=False,
        command=resolved,
        reason=reason,
        detail=detail[:500],
    )


def format_probe_failure(probe: MCPRuntimeProbe, *, action: str) -> str:
    """Human-readable, paste-safe failure block with no credential values."""

    return (
        "stage: mcp-runtime\n"
        f"  command: {probe.command}\n"
        f"  reason: {probe.reason or 'unknown'}\n"
        f"  detail: {probe.detail or 'no diagnostic was returned'}\n"
        f"  action: {action}"
    )
