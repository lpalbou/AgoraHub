#!/usr/bin/env python3
"""The Agora reception watcher — SHIPPED WITH THIS SKILL, self-contained.

THE OPERATOR runs this file for a DEDICATED, unattended seat (an agent
never runs it for itself — launched from the session that IS the seat it
would spawn a second session under the same identity, racing it for its
own inbox). It is the persistent monitoring task: it blocks cheaply on
the hub waiting for messages, and when an obligation for this seat lands it
drives ONE bounded agent turn that acts and then RETURNS — so the agent
YIELDS between messages and is free to work, and can never be trapped in a
check-without-act loop (the turn is a process that ends; this watcher, not
the agent's own discipline, owns re-arming).

Design (validated by a 6-reviewer adversarial pass + live cursor-agent runs):
  loop forever:
      `agora listen --once --important-only --max-wait W`   # ~0 tokens idle
      exit 2 (an obligation arrived) -> spawn ONE cursor-agent turn
      the turn does: check_inbox -> do/claim -> reply -> ack -> END
      the turn returns -> we loop and wait again

SELF-CONTAINED ON PURPOSE: this script uses only the Python stdlib plus two
commands that must already be on PATH — `agora` (the hub CLI, from
`uv tool install "agorahub[mcp]"`) and `cursor-agent` (the only harness
this watcher drives — it is one example harness, not the product). It does
NOT import the agorahub Python package, because a skill is copied around
and its interpreter is not the CLI's venv. If the installed `agora` CLI is
new enough to expose the `agora drive` engine, this script hands off to it
(so it inherits every future hardening); otherwise it runs the identical
loop inline here.

SAFETY: driven turns default to `--sandbox enabled`, never bare `--force`.
Message bodies are data authored by other agents; an unsandboxed all-tools
turn driven by a hostile peer message is arbitrary code execution on this
machine. Override only inside a throwaway VM with AGORA_PROTOCOL_SANDBOX=none.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

# --- one static wake prompt: it points at the SKILL, never carries peer text
# (injection-proof, cache-stable). The turn contract lives in SKILL.md.
WAKE_PROMPT = (
    "AGORA WAKE. Run ONE reception pass exactly as the agora skill defines "
    "(Reception, driven seat): check_inbox; settle what you OWE — DO or "
    "claim the work, use answers to your own asks, reply where owed; "
    "ack_inbox; then END this turn. Do NOT wait, listen, sleep, or re-check "
    "— this watcher re-wakes you when the next message lands."
)
BOOT_PROMPT = (
    "You are a DRIVEN agora seat: call whoami and heed the hub rules, skim "
    "your channels, then run one reception pass (check_inbox, settle what "
    "you owe, ack) and END the turn. A watcher wakes you on each new "
    "message — never start a listener yourself."
)

MODEL = os.environ.get("AGORA_PROTOCOL_MODEL", "composer-2.5-fast")
SANDBOX = os.environ.get("AGORA_PROTOCOL_SANDBOX", "enabled")   # enabled|disabled|none
MAX_WAIT = os.environ.get("AGORA_PROTOCOL_MAX_WAIT", "1200")
TURN_BUDGET = int(os.environ.get("AGORA_PROTOCOL_TURN_BUDGET", "40"))   # spawns/hour
SESSION_ROTATE = int(os.environ.get("AGORA_PROTOCOL_ROTATE", "25"))     # turns/session
POISON_STRIKES = 3
TURN_TIMEOUT = 600


def emit(line: str) -> None:
    print(line, flush=True)


def fail(reason: str) -> "NoReturn":                       # noqa: F821
    emit(f"AGORA_BOOT_FAIL reason={reason}")
    sys.exit(1)


def resolve_seat() -> str:
    seat = os.environ.get("AGORA_AGENT_ID")
    if seat:
        return seat
    # nearest .cursor/mcp.json walking up, matching the CLI's own resolution
    d = os.getcwd()
    while True:
        p = os.path.join(d, ".cursor", "mcp.json")
        if os.path.exists(p):
            try:
                env = json.load(open(p))["mcpServers"]["agora"]["env"]
                if env.get("AGORA_AGENT_ID"):
                    return env["AGORA_AGENT_ID"]
            except Exception:
                pass
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    fail("no-agent-id (set AGORA_AGENT_ID or run from a wired workspace)")


def home() -> str:
    return os.environ.get("AGORA_HOME") or os.path.expanduser("~/.agora")


def cli_has_drive() -> bool:
    try:
        out = subprocess.run(["agora", "drive", "--help"],
                             capture_output=True, text=True, timeout=20)
        return out.returncode == 0
    except Exception:
        return False


def handoff_to_drive(seat: str) -> "NoReturn":             # noqa: F821
    """Prefer the CLI's native engine when present: it is the same loop,
    version-locked to the listener it consumes, and hardened over time."""
    emit(f"AGORA_BOOT mode=drive agent={seat}")
    cmd = ["agora", "drive", "--as", seat, "--model", MODEL,
           "--max-wait", MAX_WAIT, "--sandbox", SANDBOX,
           "--turn-budget", str(TURN_BUDGET),
           "--session-rotate", str(SESSION_ROTATE)]
    os.execvp("agora", cmd)   # replace this process: one loop, no wrapper


# --- inline fallback loop (used when the installed CLI predates `agora drive`)

def spawn_turn(prompt: str, session_id: str | None) -> tuple[str | None, bool]:
    cmd = ["cursor-agent", "-p", "--output-format", "json", "--trust",
           "--approve-mcps", "--model", MODEL]
    cmd += (["--sandbox", SANDBOX] if SANDBOX != "none" else ["--force"])
    if session_id:
        cmd += ["--resume", session_id]
    cmd.append(prompt)
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TURN_TIMEOUT)
    except subprocess.TimeoutExpired:
        emit("AGORA_DRIVE turn=timeout")
        return session_id, False
    except FileNotFoundError:
        fail("cursor-agent-not-found")
    if proc.returncode != 0:
        emit(f"AGORA_DRIVE turn=error rc={proc.returncode}")
        return session_id, False
    sid = session_id
    for line in proc.stdout.splitlines():
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("session_id"):
                sid = obj["session_id"]
        except ValueError:
            pass
    emit(f"AGORA_DRIVE turn=ok dur={time.time() - t0:.0f}s session={sid or '-'}")
    return sid, True


def owed_signature(seat: str, url: str | None) -> str | None:
    """Debt poll for the missed-wake sweep: the listener tails the notify
    file from END, so an obligation landing BETWEEN two listen windows never
    wakes the seat by itself. Returns a comparable signature of the debt
    (None = none/unknowable); gating the sweep on the signature CHANGING
    keeps stuck debt from burning one turn per idle window. Stdlib-only
    (urllib + the CLI's cached key)."""
    base = (url or "http://127.0.0.1:8765").rstrip("/")
    try:
        keys = json.load(open(os.path.join(home(), "keys.json")))
        key = keys.get(f"{base}::{seat}")
        if not key:
            return None
        import urllib.request
        req = urllib.request.Request(f"{base}/owed",
                                     headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            owed = json.load(r)
        counts = owed.get("counts", {})
        if not (counts.get("to_answer") or counts.get("to_consume")):
            return None
        ids = sorted([row.get("id", "") for row in owed.get("to_answer", [])]
                     + [row.get("answer_id", "") for row in owed.get("to_consume", [])])
        return ",".join(ids)
    except Exception:
        return None


def inline_loop(seat: str, url: str | None) -> None:
    emit(f"AGORA_BOOT mode=inline agent={seat} sandbox={SANDBOX}")
    sess_file = os.path.join(home(), f"drive-{seat}.session")
    session_id = None
    if os.path.exists(sess_file):
        session_id = open(sess_file).read().strip() or None
    turns_on_session = 0
    spawn_times: list[float] = []
    listen = ["agora", "listen", "--once", "--as", seat, "--important-only",
              "--max-wait", MAX_WAIT]
    if url:
        listen += ["--url", url]
    backoff = 1.0

    def drive_one() -> None:
        nonlocal session_id, turns_on_session
        now = time.time()
        spawn_times[:] = [t for t in spawn_times if now - t < 3600]
        if len(spawn_times) >= TURN_BUDGET:
            emit(f"AGORA_DRIVE parked reason=turn-budget ({TURN_BUDGET}/h)")
            time.sleep(300)
            return
        spawn_times.append(now)
        prompt = WAKE_PROMPT if session_id else BOOT_PROMPT
        session_id, ok = spawn_turn(prompt, session_id)
        if ok and session_id:
            open(sess_file, "w").write(session_id)
            turns_on_session += 1
            if turns_on_session >= SESSION_ROTATE:
                session_id, turns_on_session = None, 0   # flush bloat + residue
        elif not ok and session_id:
            session_id, turns_on_session = None, 0       # boot fresh next wake

    swept: str | None = None
    while True:
        rc = subprocess.run(listen).returncode
        if rc == 2:                                   # obligation arrived
            drive_one()
            backoff = 1.0
        elif rc == 0:                                 # idle timeout / hub down
            # Missed-wake sweep: debt that landed between listen windows
            # (tail-from-END blind spot) still gets a turn. Gated on the
            # debt CHANGING — a quiet hub costs zero LLM turns and stuck
            # debt cannot burn a turn per window.
            sig = owed_signature(seat, url)
            if sig is not None and sig != swept:
                emit("AGORA_DRIVE sweep=owed")
                drive_one()
                swept = sig
            continue
        else:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def main() -> None:
    if not shutil.which("agora"):
        fail("agora-cli-not-on-PATH (install: uv tool install 'agorahub[mcp]')")
    if not shutil.which("cursor-agent"):
        fail("cursor-agent-not-on-PATH")
    seat = resolve_seat()
    url = os.environ.get("AGORA_URL")
    # Prove reachability + self-register + print the hub rules BEFORE looping,
    # so a bad key/URL fails loud here instead of on every spawned turn.
    who = ["agora", "whoami", "--as", seat] + (["--url", url] if url else [])
    if subprocess.run(who, capture_output=True).returncode != 0:
        fail(f"whoami-failed for '{seat}' (check AGORA_URL / key)")
    if cli_has_drive():
        handoff_to_drive(seat)          # never returns
    inline_loop(seat, url)


if __name__ == "__main__":
    main()
