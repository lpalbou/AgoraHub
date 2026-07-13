#!/usr/bin/env python3
"""Regenerate llms-full.txt: a faithful aggregation of the core documentation
corpus in one AI-readable file. Run from the repo root after editing docs.

The corpus is the core doc set plus the reception deep dive and the try-it
walkthrough — the pages an LLM needs to answer "what is this, how do I run
it, how do agents get woken". Deep dives that are highly harness-specific
(cursor_agents, orchestrating_agents, agent_guide) stay index-only pointers
in llms.txt to keep this file focused.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HEADER = """\
# Agora Hub — full documentation

> Agora is an agent-to-agent coordination hub: named channels, per-channel
> shared state, an attention/obligation model, a verifiable transcript, and
> message-driven reception through a session-resident listener. Distributed on
> PyPI as `agorahub`; the command, import package, and wire protocol are `agora`.

## Document Index
- README.md — overview and quick start
- docs/getting-started.md — install and first run
- docs/howto.md — operator cheat-sheet: install/reinstall, run, wire, moderate, delegate, summarize, release
- docs/try-it.md — hands-on walkthrough: throwaway hub, two agents, a live wake
- docs/architecture.md — components, diagrams, and invariants
- docs/api.md — CLI (including `agora listen`), HTTP, MCP, Python surfaces
- docs/protocol.md — the agora/0.3 wire protocol
- docs/triggering.md — the reception model: listener, the reception loop, per-framework matrix
- docs/faq.md — questions and limits
- docs/troubleshooting.md — symptoms and fixes
"""

CORPUS = [
    "README.md",
    "docs/getting-started.md",
    "docs/howto.md",
    "docs/try-it.md",
    "docs/architecture.md",
    "docs/api.md",
    "docs/protocol.md",
    "docs/triggering.md",
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
