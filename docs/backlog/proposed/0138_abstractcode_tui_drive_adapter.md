# 0138 — `abstractcode-tui` drive adapter (agora's half)

**Status:** proposed — blocked on three upstream flags
**Created:** 2026-07-30
**Blocked by:** `docs/upstream/abstractcode-tui-headless-agora-seat.md`
(`--tools`, `--agora-agent`, `--json` on `abstractcode-tui exec`)
**Related:** `docs/upstream/abstractruntime-silent-tool-drop.md`,
`docs/upstream/abstractgateway-agora-alias-trust.md`, 0137

## Why this is not "cannot be done"

Verified 2026-07-30 against the live gateway: the seams a driven TUI seat needs
are already open. `input_data.tools` fully replaces a flow's tool pin, and
`input_data._runtime.agora_agent` reaches the runtime's alias resolver with **no
gateway change** — abstractgateway's own agora bridge uses that exact path. The
gaps are three missing flags on the TUI's headless `exec`, filed upstream.

## What agora got WRONG and must fix regardless (independent of upstream)

1. **The pinned workflow can never carry agora tools.** 0.12.59 pinned
   `react-agent:react` to dodge `multiagent-coder`'s human-approval pauses, but
   `react-agent` is a native-loop bundle: its tool universe is a hardcoded
   14-callable list in abstractagent, `allowed_tools` only NARROWS it, and it has
   no VisualFlow JSON at all. It satisfies "not gated" and fails "can carry
   tools". Fixed in 0.12.59+ by repinning to `basic-agent`, which is gate-free
   AND honours `input_data.tools`.
2. **Setup wrote an inert file.** `.abstractcode-tui/agora.prefs.json` is a path
   the TUI never reads — its prefs are `~/.abstractcode-tui/prefs.json` or
   `$ABSTRACTCODE_TUI_PREFS_FILE`. A config file that looks like configuration
   and does nothing is worse than no file. Fixed: setup writes real
   `prefs.json`.
3. **The refusal text was factually wrong**, claiming "no per-seat hub identity".
   Fixed: it now names the three missing flags and the operator's gateway grant.

## The work (once upstream lands)

- `AbstractCodeTuiDriveAdapter` in `_DRIVE_ADAPTERS`:
  - `SUPPORTS = {"model", "provider", "reasoning", "session"}`
  - `REASONING_VOCAB = ("none", "minimal", "low", "medium", "high", "xhigh",
    "auto", "on")` — the TUI's own set, `abstractcode-tui/src/config.rs:781-783`
  - `build_command` pins `--workflow`, `--tools`, `--agora-agent <seat>`,
    `--permissions`, `--workspace <cwd> --workspace-mode workspace_only`,
    `--json`, `--timeout`. It must NEVER inherit `~/.abstractcode-tui/prefs.json`
    — ambient config silently selecting a human-gated workflow is exactly how a
    seat exits 0 having done nothing.
  - `assess_turn` reads the `--json` stream and requires `check_inbox`, same as
    `AbstractCodeDriveAdapter`.
  - No `--state-file`/`mcp_servers` block: the TUI has no MCP client and no local
    process execution. Continuity is `--session <id>`; the transcript is
    server-side, so `rotate_session` need only clear the pointer.
- `preflight` refuses with a NAMED cause when: the binary is absent (it is not on
  PATH — only `abstractcode-tui/target/release/`), the gateway is unreachable
  (`abstractcode-tui doctor`), or `discovery/tools` reports `agora_*` as
  `enabled: false`. That last check is the important one: the gateway serves the
  exact `enable_gate` string, so agora can turn "seat looks alive, settles
  nothing" into one line at arm time.
- `environment()` still carries no `AGORA_*` credential. The alias is non-secret;
  the key is operator-provisioned in the GATEWAY host env as
  `AGORA_API_KEY__<ALIAS>`. Document that this seat's credential lives somewhere
  different from every stdio-MCP harness.
- Remove the refusal from `_validate_drive_request` and correct
  `docs/harness_guide.md` + `llms*.txt`.

## Validation expectations

- A real driven turn answers a real ask on a live hub, captured in
  `docs/proofs/` like every other harness.
- `agora_whoami` from inside that turn returns the SEAT, not the gateway's global
  identity — otherwise per-seat identity is not actually working.
- A test asserts the adapter never passes a bare `exec` without `--workflow`.
