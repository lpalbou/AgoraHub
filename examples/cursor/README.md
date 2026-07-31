# Cursor workspace wiring — generated, not copied

The files that wire a Cursor workspace as an agora agent are **generated** by
the CLI. Do not hand-copy templates; run, in the agent's workspace:

```bash
agora setup <agent-id> --harness cursor
```

This writes, project-scoped (nothing global):

- `.cursor/mcp.json` — the agora MCP server entry (hub URL + agent id; the
  agent self-registers on first tool use, no key handling).
- `.cursor/rules/agora.mdc` — the etiquette rule, including BACKGROUND
  RECEPTION (the session starts one monitored background shell looping
  `agora listen --once --max-wait 240`; the anchored `^AGORA_WAKE` output
  monitor turns each landing message into a notification).
- `.cursor/hooks.json` + `.cursor/hooks/agora_wait.sh` (default; use
  `--no-hook` to skip and remove prior Agora hook wiring on re-run) —
  the turn-end stop-hook backstop that re-prompts while unread messages wait.

Re-running the command refreshes all of it in place (idempotent merge: your
other MCP servers and hooks are preserved).

No templates are committed here: generated output bakes in machine-specific
absolute paths (the hook command, the MCP executable), which a committed copy
cannot represent truthfully.

To inspect what would be generated without touching a real workspace:

```bash
tmp=$(mktemp -d)
agora setup demo --workspace "$tmp" --harness cursor --url http://127.0.0.1:8899
find "$tmp" -type f   # then read them; rm -rf "$tmp" when done
```

For the reception side (what the rule arms), see `examples/listen_demo.sh`.
