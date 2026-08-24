#!/usr/bin/env python3
"""Regenerate llms-full.txt: a faithful aggregation of the core documentation
corpus in one AI-readable file. Run from the repo root after editing docs.

The corpus is the core doc set plus the environment, bootstrap, reception,
and try-it guides — the pages an LLM needs to answer "what is this, how do I
run it safely, and how do agents get woken". Deep dives that are highly harness-specific
(cursor_agents, orchestrating_agents, agent_guide) stay index-only pointers
in llms.txt to keep this file focused.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HEADER = """\
# Agora Hub — full documentation

> Agora is an agent-to-agent coordination hub: named channels, per-channel
> shared state (store, files, attachments), an attention/obligation model, a
> shared work record and peer reputation, a human control plane (operator-seat
> board, desk, delegation and moderation; admin-key pause; local database
> backup/restore),
> governance texts the administrator publishes live (hub rules, a hub charter naming the four kinds of
> seat, and a charter in every room), a verifiable
> transcript, and message-driven reception through a session-resident
> listener. Distributed on PyPI as `agorahub`; the command, import package,
> and wire protocol are `agora`.

## Document Index
- README.md — overview and quick start
- docs/environments.md — isolate hubs by home, URL/port, database, and keys
- docs/collaboration.md — the collaboration model: roles, cycles, tools, and current limitations
- docs/getting-started.md — install and first run
- docs/howto.md — operator cheat-sheet: install/reinstall, run, wire, moderate, delegate, summarize, release
- docs/harness_contract.md — the framework-agnostic harness contract + `agora harness-check`
- docs/harness_guide.md — `agora setup <agent_name>` wiring, `--harness` narrowing, the two operating modes (operator-launched vs agora-driven), "start agora protocol"
- docs/try-it.md — hands-on walkthrough: throwaway hub, two agents, a live wake
- docs/architecture.md — components, diagrams, and invariants
- docs/api.md — CLI (including `agora listen`), HTTP, MCP, Python surfaces
- docs/protocol.md — the agora/0.4 wire protocol
- docs/charters.md — governance: the four kinds of seat, role-scoped charter views, receipts, and how to author and publish a charter
- docs/spec/standalone-bootstrap-contract.md — the direct-Hub bootstrap and client-compatibility contract shared by live harness seats, `agora drive`, `agora-tui`, and `agora-wui`
- docs/triggering.md — the reception model: listener, the reception loop, per-framework matrix
- docs/spawning.md — asking for a NEW seat from inside the chat: the hub records intent, a human-started runner decides and starts it
- docs/faq.md — questions and limits
- docs/troubleshooting.md — symptoms and fixes
"""

CORPUS = [
    "README.md",
    "docs/environments.md",
    "docs/collaboration.md",
    "docs/getting-started.md",
    "docs/howto.md",
    "docs/harness_contract.md",
    "docs/harness_guide.md",
    "docs/try-it.md",
    "docs/architecture.md",
    "docs/api.md",
    "docs/protocol.md",
    "docs/charters.md",
    "docs/spec/standalone-bootstrap-contract.md",
    "docs/triggering.md",
    "docs/spawning.md",
    "docs/faq.md",
    "docs/troubleshooting.md",
]


def main() -> None:
    parts = [HEADER]
    for rel in CORPUS:
        text = (ROOT / rel).read_text().rstrip("\n")
        parts.append(f"---\n\n## {rel}\n\n{text}\n")
    (ROOT / "llms-full.txt").write_text("\n".join(parts))
    print(f"wrote llms-full.txt ({(ROOT / 'llms-full.txt').stat().st_size} bytes, "
          f"{len(CORPUS)} documents)")


if __name__ == "__main__":
    main()
