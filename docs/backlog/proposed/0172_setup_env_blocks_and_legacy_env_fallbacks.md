# Proposed: Finish "flags and files": setup-written env blocks and the legacy env fallbacks

## Metadata
- Created: 2026-11-09
- Status: Proposed
- Completed: N/A

## ADR status
- Governing ADRs: None. The operator rule (2026-09-09) "configuration is flags
  and files; the environment carries credentials only" is recorded in
  `CHANGELOG.md`, not in an ADR.
- ADR impact: May need a new ADR if the rule is to bind every surface.

## Context
0.18.0 moved `agora-mcp` and the driven turn to flags and files:
`agora-mcp --url --home --as --about --download-dir --tools`; `agora drive`
writes a per-seat turn marker `.agora/driven-<seat>.json`; the CLI resolves
its hub from `--url`, then the turn marker, then the workspace
`.agora/seat.json`, then the legacy `AGORA_URL`, then the home config, and
`agora listen` and the MCP server resolve flag, then seat record, then
env/config. Tests: `tests/test_driven_pin.py`. The changelog names what is left:
*"The `.mcp.json` env block written by `agora setup` and the legacy env
fallbacks are the next pass."*

## Current code reality
- `_server_env` (`src/agora/setup_harness.py:530`) still builds an env block
  with `AGORA_URL`, `AGORA_AGENT_ID`, `AGORA_ABOUT`, empty `AGORA_API_KEY` and
  `AGORA_ADMIN_KEY`, and `AGORA_HOME` when the home is not the default.
- It is embedded by `write_mcp_json` (Cursor `.cursor/mcp.json` and Claude
  `.mcp.json`), the Codex TOML writer, `setup_abstractcode`, `setup_opencode`,
  `register_claude_local` and `register_codex_global`.
- Hub resolution still falls back to `AGORA_URL` after the flag, marker and
  seat-record sources.
- The pi bridge keeps an env contract on purpose (it spawns the server
  itself).

## Problem or opportunity
Two configuration paths remain for the same seat: flags at drive time, env in
setup-written configs. A setup-written config can still point a server at
whatever the env block says, which is the class of mismatch the 0.18.0 change
closed for driven turns.

## Proposed direction
Write the server entry as `command` + flags (`--url`, `--as`, `--about`,
`--home` when non-default) and keep only the two empty credential slots in
`env`. Then retire the `AGORA_URL` fallback with a deprecation notice for one
release. Overlaps with `0163` (profiles and authoritative workspace bindings);
settle the order with that item.

## Why it might matter
One way to configure a seat is easier to audit and cannot disagree with itself.

## Promotion criteria
The operator confirms the rule should bind setup-written configs too, and
each harness's config format is checked for argv support (Cursor `mcp.json`,
Claude `.mcp.json`, Codex TOML, opencode, AbstractCode).

## Validation ideas
- `tests/test_setup_harness.py`: written configs carry flags, and `env` holds
  only the empty credential slots.
- Re-running setup over an old env-style entry rewrites it.
- `tests/test_driven_pin.py` stays green.

## Non-goals
- No change to the pi bridge contract.
- No credentials in flags or workspace files; keys stay in the 0600
  `keys.json`.

## Guidance for future agents
Read the "Configuration is flags and files" entry in `CHANGELOG.md` and
`docs/environments.md` first; check `0163` before designing a profile format.
