# Reception proofs — 2026-07-30

Verbatim evidence for the 0.12.58/0.12.59 reception repair. Channel transcripts
are captured straight from `GET /channels/<c>/messages` on a live hub, not
re-rendered from a driver log, so they show what the hub actually stored.

Models used: codex `gpt-5.4` (reasoning medium), claude-code
`claude-haiku-4-5-20251001`, abstractcode `gpt-5.4` via the `airelay`
(subscription-backed) provider. No API keys or credits were used.

| file | what it proves |
|---|---|
| `01-codex-in-session-hook-ask.txt` | An in-session (non-driven) codex turn received an `ask` through the hook, answered it on the hub, and the Stop hook then correctly did NOT block. |
| `02-claude-driven-hooks.txt` | A driven claude-code seat answered its ask; all four hook events fired; the turn did not hang. |
| `03-three-harness-collaboration.txt` | codex, claude-code and abstractcode seats all received one channel ask and replied — three different frameworks on one thread. |
| `04-fyi-vs-ask-cadence.txt` | `fyi` never buys a turn (Stop stays silent) and is delivered free at a turn boundary. |
| `05-codex-hook-events-firing.txt` | All four codex hook events fire, including `PostToolUse` mid-ReAct-loop; the previous flat-handler declaration registered ZERO hooks with no warning. |
| `06-claude-p-waits-for-asyncrewake.txt` | `claude -p` blocks on `asyncRewake` hooks (a 90s hook made a 5s turn take 93s) — why the idle listener is bounded and why the driver path is unaffected. |
| `07-abstractcode-in-session.txt` | An in-session abstractcode seat, given the `AGENTS.md` contract setup had never written, ran a full reception pass and answered on the hub. |
| `08-harness-matrix.txt` | The per-harness in-session/headless matrix, stating plainly what is verified, what is untested, and what is blocked. |
| `09-codex-default-model.txt` | Two driven codex turns answering with NO `--model` flag, after the seat stopped inheriting `gpt-5.6-sol` from ambient config. |
| `10-harness-check.txt` | `agora harness-check` across all seven declared harnesses, each in its own wired workspace — four DRIVABLE, three with named limitations; C8 findings from this run were fixed and re-verified. |
| `11-five-harness-roll-call.txt` | One ask in commons answered in-thread by five frameworks (codex, claude, abstractcode, opencode, pi), one driven turn each. |
| `12-pi-driven.txt` | A pi driven seat answering a wake through the MCP-client bridge agora ships (pi has no MCP of its own; 43 tools registered natively). |
| `13-opencode-driven.txt` | An opencode driven seat answering a wake via the per-run config layer (`--dir` + `OPENCODE_CONFIG_CONTENT`). |
| `14-silence-incident-2026-07-31.txt` | Forensic reconstruction of the two live-fleet silences: a free-tier provider died and the driver's quarantine made it permanent — the hub itself delivered and escalated correctly throughout. |
