"""`agora harness-check` — the conformance test a framework runs on ITSELF.

Agora is a communication protocol. It must not know a framework's internals,
and its operator must not have to negotiate with each vendor to get a seat
working. So the relationship is inverted: agora publishes ONE contract, and any
framework runs ONE command to learn, in the contract's own terms, whether it
can carry an agora seat — and exactly what a seat loses where it cannot.

Every probe is STRUCTURAL: no LLM call, no tokens, no hub writes. The single
exception is the opt-in `--live` turn. A failing probe never aborts the rest:
the report IS the deliverable, and a partial harness must be able to read its
own scorecard in one run.

Three capabilities are HARD — no seat can exist without them: single-turn,
tool-reach, identity. Every other capability degrades to a NAMED limitation:
light safeguards, never silent, never blocking.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
HARD = ("single-turn", "tool-reach", "identity", "agora-runtime")
#: `binary` is not a CONTRACT item — it is an install fact — but nothing can run
#: without it, so a missing executable must block the verdict just the same.
BLOCKING = HARD + ("binary",)

#: Static, peer-text-free, side-effect-free. The one tool it names is the one
#: tool every agora seat must be able to call.
LIVE_PROMPT = (
    "AGORA HARNESS CHECK. Call the Agora MCP tool `whoami` exactly once, then "
    "reply with the agent id it returned and END. Do not write files, do not "
    "send messages, do not call any other tool."
)


@dataclass
class Probe:
    id: str
    capability: str
    status: str
    detail: str
    limitation: str | None = None


@dataclass
class Report:
    harness: str
    probes: list[Probe] = field(default_factory=list)

    def add(self, *a, **kw) -> None:
        self.probes.append(Probe(*a, **kw))

    @property
    def blocked(self) -> list[Probe]:
        return [p for p in self.probes
                if p.status == FAIL and p.capability in BLOCKING]

    @property
    def limitations(self) -> list[str]:
        return [p.limitation for p in self.probes if p.limitation]

    def render(self) -> str:
        out = [f"agora harness-check — {self.harness}", ""]
        for p in self.probes:
            out.append(f"  {p.id} {p.capability:<14} {p.status:<4} {p.detail}")
            if p.limitation:
                out.append(f"       -> agora degrades: {p.limitation}")
        out.append("")
        if self.blocked:
            out.append("VERDICT: NOT DRIVABLE — unmet: "
                       + ", ".join(p.capability for p in self.blocked))
            out.append("  This seat still works IN-SESSION wherever its "
                       "framework can reach agora's tools.")
        elif self.limitations:
            out.append("VERDICT: DRIVABLE WITH LIMITATIONS — "
                       + "; ".join(self.limitations))
        else:
            out.append("VERDICT: DRIVABLE")
        counts = {s: sum(1 for p in self.probes if p.status == s)
                  for s in (PASS, WARN, FAIL, SKIP)}
        out.append(f"  {counts[PASS]} pass, {counts[WARN]} warn, "
                   f"{counts[FAIL]} fail, {counts[SKIP]} skipped")
        return "\n".join(out)

    def to_json(self) -> str:
        return json.dumps({"harness": self.harness,
                           "drivable": not self.blocked,
                           "limitations": self.limitations,
                           "probes": [p.__dict__ for p in self.probes]},
                          indent=2)

    def exit_code(self) -> int:
        return 1 if self.blocked else 0


def _referenced_files(argv: list[str]) -> str:
    """Text of every existing file this command NAMES.

    A harness may carry its agora binding in argv (`-c mcp_servers...`,
    `--mcp-config <json>`) or in a sidecar the argv points at
    (`--state-file`). Reading both, generically, lets ONE probe cover every
    style without agora knowing which style a vendor chose.
    """
    blob = []
    for token in argv:
        if "\n" in token or len(token) > 4096:
            continue
        try:
            path = Path(token)
            for candidate in (path, path.with_suffix(".config.json")):
                if candidate.is_file() and candidate.stat().st_size < 1_000_000:
                    blob.append(candidate.read_text(errors="replace"))
        except (OSError, ValueError):
            continue
    return "\n".join(blob)


def _surface(adapter, workspace: Path, harness: str) -> str:
    """Everything a turn could carry a binding in: argv, files argv names, the
    per-seat env, and the workspace config the framework reads on its own."""
    from .setup_harness import agora_config_text, workspace_harness_env

    argv = adapter.build_command(LIVE_PROMPT, None)
    return "\n".join([*argv, _referenced_files(argv),
                      json.dumps(adapter.environment() or {}),
                      json.dumps(workspace_harness_env(workspace, harness) or {}),
                      # Cursor names no binding in argv at all: it reads the
                      # file agora wrote. Both styles must count.
                      agora_config_text(workspace, harness)])


def run_check(harness: str, *, workspace: Path, agent_id: str, url: str,
              live: bool = False) -> Report:
    from . import config as _config
    from .drive import _AGORA_KEY_RE, _DRIVE_ADAPTERS, _make_adapter
    from .mcp.runtime import (MCPBinding, format_probe_failure,
                              probe_mcp_runtime, resolve_mcp_command)

    report = Report(harness)
    cls = _DRIVE_ADAPTERS.get(harness)
    if cls is None:
        report.add("C0", "declared", FAIL,
                   f"no harness named '{harness}' is declared to agora")
        return report
    unmet = frozenset(cls.UNMET)
    binding = MCPBinding(command=resolve_mcp_command(), agent_id=agent_id,
                         url=url, home=_config.home())

    # C1 binary — cheapest, and everything after it depends on it.
    found = shutil.which(cls.binary) if cls.binary else None
    report.add("C1", "binary", PASS if found else FAIL,
               found or f"`{cls.binary}` is not on PATH")

    # C2 single-turn — proves a turn TERMINATES with no human and no tty.
    if "single-turn" in unmet:
        report.add("C2", "single-turn", FAIL,
                   "declared unmet: no non-interactive single-turn invocation")
    elif not found:
        report.add("C2", "single-turn", SKIP, "binary absent")
    else:
        t0 = time.time()
        try:
            done = subprocess.run([found, *cls.PROBE_ARGV], capture_output=True,
                                  text=True, timeout=20, stdin=subprocess.DEVNULL)
            report.add("C2", "single-turn", PASS,
                       f"terminates with stdin closed (rc={done.returncode} in "
                       f"{time.time() - t0:.1f}s)")
        except subprocess.TimeoutExpired:
            report.add("C2", "single-turn", FAIL,
                       "did not terminate within 20s with stdin closed — a "
                       "driven turn would wedge the seat")
        except OSError as exc:
            report.add("C2", "single-turn", FAIL, str(exc))

    # C3 agora-runtime — agora's OWN half of the contract, probed the same way.
    probe = probe_mcp_runtime(binding.command)
    report.add("C3", "agora-runtime", PASS if probe.ok else FAIL,
               f"agora-mcp {probe.agora_version}, mcp-sdk {probe.sdk_version}"
               if probe.ok else
               format_probe_failure(probe, action="reinstall agora"
                                    ).replace("\n", " "))

    # C4/C5/C8 all read one built command.
    adapter = None
    if unmet & {"single-turn", "tool-reach"}:
        for pid, cap in (("C4", "tool-reach"), ("C5", "identity")):
            report.add(pid, cap, FAIL, "declared unmet — no command can be built")
        report.add("C8", "knobs", SKIP, "no command to inspect")
    else:
        # permissions=None lets _make_adapter apply the harness's own default
        # chain — hardcoding "write" here crashed the whole report for any
        # harness whose vocabulary excludes it.
        adapter = _make_adapter(harness, model=None, provider=None,
                                cwd=workspace, mcp=binding)
        text = _surface(adapter, workspace, harness)
        if cls.TOOL_REACH == "external":
            # This harness reaches agora's tools through a server it already
            # runs, so there is no binding in the command surface to find.
            # Saying FAIL here would be agora inventing a verdict about
            # somebody else's configuration; saying PASS would be claiming a
            # check it did not perform.
            report.add("C4", "tool-reach", WARN,
                       "provided by the framework's own server, not by an "
                       "agora-launched MCP server — not statically checkable",
                       limitation="tool-reach=unverified (run --live, or check "
                                  "the framework's own tool listing)")
        else:
            report.add("C4", "tool-reach", PASS if binding.command in text else FAIL,
                       "agora's MCP server reaches the turn"
                       if binding.command in text else
                       "no agora MCP server in argv, in any config this command "
                       "names, or in this workspace's harness config — run "
                       "`agora setup <id>` here, or give the framework a way to "
                       "launch a stdio MCP server per turn")
        if _AGORA_KEY_RE.search(text):
            report.add("C5", "identity", FAIL,
                       "a bearer value appears in the command surface; agora "
                       "credentials belong only in the 0600 key cache")
        elif cls.IDENTITY_SCOPE == "process":
            # The harness declares that a TURN cannot say which seat it is —
            # the server's configured identity posts. That is a degraded mode
            # agora warns about at arm time, not a missing hard capability.
            report.add("C5", "identity", WARN,
                       "identity is PROCESS-scoped: whatever identity the "
                       "framework's server is configured with is the one that "
                       "posts. Safe only while that server serves this seat "
                       "alone",
                       limitation="identity=process-scoped (one seat per "
                                  "server process)")
        else:
            report.add("C5", "identity", PASS if agent_id in text else FAIL,
                       f"seat id `{agent_id}` reaches the turn; no bearer in "
                       "argv, env, or config" if agent_id in text else
                       f"the turn carries no way to say it is `{agent_id}`; a "
                       "turn that cannot prove its seat must not post")
        report.probes.append(_knob_probe(harness, cls, binding, workspace))

    # C6 evidence — DECLARED, never sniffed.
    if cls.EVIDENCE and "evidence" not in unmet:
        report.add("C6", "evidence", PASS if live else WARN,
                   f"{cls.EVIDENCE}" + (" (verified by the live turn)" if live
                                        else " (pass --live to verify)"))
    else:
        report.add("C6", "evidence", WARN, "no machine-readable turn stream",
                   limitation="evidence=exit-code-only (turn success is judged "
                              "by exit code plus the hub's own /owed record)")

    # C7 continuity — DECLARED, never sniffed.
    if cls.CONTINUITY:
        report.add("C7", "continuity", PASS, cls.CONTINUITY)
    else:
        report.add("C7", "continuity", WARN, "none declared",
                   limitation="continuity=none (every turn boots fresh; the "
                              "hub stays the seat's durable memory)")

    # C9 live turn — opt-in, one real turn, judged by the hub.
    if not live:
        report.add("C9", "live-turn", SKIP, "pass --live to run one real turn")
    elif adapter is None or report.blocked:
        report.add("C9", "live-turn", SKIP,
                   "structural requirements unmet; a live turn cannot pass")
    else:
        report.probes.append(_live_probe(adapter, agent_id, url, workspace))
    return report


def _knob_probe(harness: str, cls, binding, workspace: Path) -> Probe:
    """Every DECLARED knob must change the built command; every undeclared one
    must be REFUSED. A knob accepted and silently dropped is the failure that
    makes a seat look healthy while the wrong brain answers the hub."""
    from .drive import _make_adapter, _validate_drive_request

    probes = {"model": "probe-model-x", "provider": "probe-provider-x",
              "reasoning": (cls.REASONING_VOCAB or ("low",))[-1]}
    field_of = {"model": "model", "provider": "provider",
                "reasoning": "reasoning_effort", "permissions": "permissions"}
    # permissions joined the contract in 0.12.60: probe with a NON-default
    # vocabulary member so the diff below can see it reach the turn. A
    # single-level vocabulary has nothing to diff — the default IS the level —
    # so the knob is exercised only where a second level exists.
    default_level = cls.HARNESS_DEFAULT_PERMISSIONS or "write"
    non_default = [lv for lv in cls.PERMISSION_VOCAB if lv != default_level]
    if "permissions" in cls.SUPPORTS and non_default:
        probes["permissions"] = non_default[-1]
    reached, dropped, refused = [], [], []
    base = _surface(_make_adapter(harness, model=None, provider=None,
                                  reasoning_effort=None,
                                  cwd=workspace, mcp=binding),
                    workspace, harness)
    for knob, value in probes.items():
        kwargs = {"model": None, "provider": None, "reasoning_effort": None}
        kwargs[field_of[knob]] = value
        if knob not in cls.SUPPORTS:
            try:
                _validate_drive_request(harness, **kwargs)
                dropped.append(f"{knob}(undeclared, not refused)")
            except SystemExit:
                refused.append(knob)
            continue
        try:
            text = _surface(_make_adapter(harness, cwd=workspace, mcp=binding,
                                          **kwargs),
                            workspace, harness)
        except SystemExit:
            refused.append(f"{knob}(refused at build)")
            continue
        changed = text != base
        hit = value in text or (knob == "permissions" and changed)
        (reached if hit and changed else dropped).append(knob)
    parts = [label + ": " + ",".join(names)
             for label, names in (("reach the turn", reached),
                                  ("correctly refused", refused),
                                  ("ACCEPTED AND DROPPED", dropped)) if names]
    return Probe("C8", "knobs", FAIL if dropped else PASS,
                 "; ".join(parts) or "no knobs declared",
                 limitation=("knobs: " + ",".join(dropped)
                             + " accepted and dropped — the seat arms healthy "
                             "and answers with the wrong configuration")
                 if dropped else None)


def _live_probe(adapter, agent_id: str, url: str, workspace: Path) -> Probe:
    """One real turn, judged by the HUB — the oracle every framework shares.

    Hub presence advances on any authenticated call, so this proves the turn
    reached agora AS THIS SEAT even when the framework emits no evidence at
    all. That is precisely why evidence can be optional without the driver
    going blind."""
    from . import config as _config
    from .drive import _harness_environment

    def seen() -> float:
        """Last authenticated hub activity for this seat. Same cached-key,
        short-timeout, never-raises shape as listen._owed_snapshot."""
        try:
            import httpx
            key = _config.get_cached_key(url, agent_id)
            if not key:
                return -1.0
            resp = httpx.get(f"{url.rstrip('/')}/presence/{agent_id}",
                             headers={"Authorization": f"Bearer {key}"},
                             timeout=5.0)
            return float(resp.json().get("updated_at") or 0.0)
        except Exception:
            return -1.0

    before = seen()
    env = {**_harness_environment(), **(adapter.environment() or {})}
    try:
        done = subprocess.run(adapter.build_command(LIVE_PROMPT, None),
                              capture_output=True, text=True, timeout=300,
                              cwd=str(workspace), stdin=subprocess.DEVNULL,
                              env=env)
    except subprocess.TimeoutExpired:
        return Probe("C9", "live-turn", FAIL, "turn did not finish in 300s")
    evidence = adapter.assess_turn(done.stdout or "", done.stderr or "",
                                   done.returncode, "check")
    if evidence.ok and "whoami" in evidence.tools:
        return Probe("C9", "live-turn", PASS,
                     "one real turn called whoami "
                     f"(tools: {','.join(evidence.tools)})")
    if seen() > before >= 0:
        return Probe("C9", "live-turn", PASS,
                     "hub presence advanced during the turn — it authenticated "
                     "as this seat (no evidence stream names the tools)")
    return Probe("C9", "live-turn", FAIL,
                 f"stage={evidence.stage or 'unknown'} "
                 f"reason={evidence.reason or 'unknown'} "
                 f"{evidence.detail or ''}".strip())
