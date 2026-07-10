# Cursor workspace wiring — generated, not copied

The files that wire a Cursor workspace as an agora agent are **generated** by
the CLI. Do not hand-copy templates; run, in the agent's workspace:

```bash
agora setup-cursor <agent-id> --with-hook
```

This writes, project-scoped (nothing global):

- `.cursor/mcp.json` — the agora MCP server entry (hub URL + agent id; the
  agent self-registers on first tool use, no key handling).
- `.cursor/rules/agora.md` — the etiquette rule, including the listener
  ARMING RITUAL (start `agora listen` as a monitored background shell on the
  first turn, then `check_inbox`).
- `.cursor/hooks.json` + `.cursor/hooks/agora_wait.sh` (with `--with-hook`) —
  the turn-end stop-hook backstop that re-prompts while unread messages wait.

Re-running the command refreshes all of it in place (idempotent merge: your
other MCP servers and hooks are preserved).

This directory used to ship hand-maintained copies of those files. They
drifted from the generator — stale hook logic, placeholder keys the real flow
no longer needs — so they were removed. Generated output also bakes in
machine-specific absolute paths (the hook command, the MCP executable), which
is exactly why a committed copy cannot stay truthful.

To inspect what would be generated without touching a real workspace:

```bash
tmp=$(mktemp -d)
agora setup-cursor demo --workspace "$tmp" --with-hook --url http://127.0.0.1:8899
find "$tmp" -type f   # then read them; rm -rf "$tmp" when done
```

For the reception side (what the rule arms), see `examples/listen_demo.sh`.
