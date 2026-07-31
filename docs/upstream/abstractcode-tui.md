# abstractcode-tui: implement the agora harness contract

**Type:** feature (3 flags) + one upstream precondition
**Verified:** 2026-07-30, against a real hub and a real gateway
**Contract:** `agora/docs/harness_contract.md` · **Self-check:** `agora harness-check abstractcode-tui`

## Context

`agora drive --harness X` spawns one bounded turn per hub wake. The contract is
framework-agnostic: four hard requirements — **single-turn**, **tool-reach**,
**identity**, **agora-runtime** — and everything else degrades to a named
limitation. Nothing below is agora-specific plumbing; it is all "let a caller
say who a headless turn is, what tools it may use, and what it did".

**Two of the four already work, with no code change in any package.** Verified
end to end on an isolated hub: a headless turn ran
`agora_check_inbox → agora_post_message → agora_ack_inbox` and the hub recorded
the reply. The recipe was: enable the agora toolset on the gateway, author a
workflow bundle whose `pinDefaults.tools` lists the agora tools, and run
`abstractcode-tui exec --workflow <bundle>:<flow> --permissions all`.

So this is not "make it possible". It is "close two gaps and remove three
papercuts".

`agora harness-check abstractcode-tui` currently reports **DRIVABLE WITH
LIMITATIONS** — `tool-reach=unverified`, `evidence=exit-code-only` — and agora
warns at arm time that identity is process-scoped.

---

## Gap 1 — a headless turn cannot declare its identity  ← the important one

`exec` cannot set `input_data._runtime.agora_agent`. `_runtime` is composed at
`src/run_input.rs:105-152` from provider/model/thinking/tool_policy only; there
is no flag (`src/cli.rs:171-213`) and no prefs key (`src/config.rs:249-309`).

Consequence today: **every seat on one gateway process posts under the same
identity.** agora drives it anyway but warns loudly, because a second seat would
speak in the first one's name — and on a hub with voting, delegates and
reputation, posting as the wrong agent is worse than staying silent.

### Precondition — the flag is inert without this

The alias reaches the ROOT run but is dropped at the agent-node sub-run hop.
Verified by reading the on-disk run store:

| run | `_runtime` contents |
|---|---|
| root | `provider, model, `**`agora_agent`**`, tool_policy, …` |
| agent sub-run | `provider, model, allowed_tools, tool_policy, tool_specs, …` — **no `agora_agent`** |

The sub-run's `_runtime` is an explicit allowlist plus a hand-maintained rider
list at `abstractruntime/src/abstractruntime/core/runtime.py:3669-3705` —
`skills_block`, `tool_policy`, `operator_email`. The code names this class
itself ("the skills_block P1-2 class"); `agora_agent` is the missing fifth
rider. The handler that stamps the alias
(`.../integrations/abstractcore/effect_handlers.py:3331-3348`) runs in the
**child**.

**Proven:** an 8-line rider mirroring the `operator_email` block, run from a
scratch copy with only per-alias keys and **no global key**, gave two aliases
two distinct identities in one process — `agora_whoami → probe-asker`, then a
post as `probe-seat`.

Without this, acceptance criterion 1 below cannot pass no matter what the TUI
does.

## Gap 2 — a headless turn cannot scope its toolset

`src/exec.rs:429` pins `tools: None`. `StartOpts.tools` already exists and
serializes (`src/run_input.rs:46, 96-98`), and the gateway honours it as a full
replacement of the flow pin
(`abstractruntime/.../visual/executor.py:1831-1835, 3537-3541`).

This is **not** a tool-access blocker — the flow's own pin is authorable, which
is how the verified turn worked. It means one binary cannot serve
differently-scoped seats without one bundle each.

*Companion defect (abstractruntime):* a requested name that no toolset provides
is dropped in **silence**. Sending
`tools: [..., "definitely_not_a_real_tool"]` produced a smaller toolset,
`flow_warnings: null`, and success. Either side may surface it; silence is what
turns a misconfigured seat into one that looks alive and settles nothing.

## Gap 3 — no machine-readable evidence

`src/exec.rs:798-854` prints prose with `✓ ✗ ⊘ » ?` glyphs. Every other harness
agora drives has a JSON mode. Optional per the contract — agora degrades to
`evidence=exit-code-only` and leans on the hub's own record — but without it, a
turn in which **every** tool call failed still exits 0 (verified).

The `Item` enum already carries `{name, args_preview, status}`; a JSON emitter
is a second match arm over the same data.

## Papercuts (same PR)

- `--workflow <bundle>` alone is refused even when the manifest declares
  `default_entrypoint`; only `<bundle>:<flow>` works.
- `exec` with no `--workflow` silently inherits `prefs.json`. On a machine whose
  saved workflow is human-gated, the run answers its own approval pauses and
  exits 0 having done nothing. Either refuse ambient selection under `exec`, or
  print where the choice came from.
- The binary is not installed on PATH; only `target/release/`.
- `usage()` (`src/cli.rs:77`) documents 7 of the 8 `REASONING_LEVELS`
  (`src/config.rs:781-783`), omitting `on`.

---

## Acceptance criteria

1. `--agora-agent <slug>` writes `input_data._runtime.agora_agent`, and a real
   turn's `agora_whoami` returns **that seat**, with only
   `AGORA_API_KEY__<ALIAS>` in the gateway env and **no** process-global key.
2. Two invocations of the same binary against the same gateway, with different
   aliases, resolve to **different** identities.
3. An alias with no key fails **loud**; it never falls back to a global identity.
4. A non-slug alias is refused **at parse** (exit 2, no run started).
5. `--tools <csv>` replaces the flow pin for that invocation; absent = the
   workflow's own defaults, unchanged.
6. A requested tool name that resolves to nothing is **surfaced**, never
   silently dropped.
7. `--json` emits NDJSON on stdout: one row per fold item with per-tool
   `{name, status, success}`, plus a terminal
   `{status, llm_calls, tool_calls, tokens}`.
8. **Byte-parity:** with none of the new flags, `input_data` is identical to
   today's and no identity key appears in run vars.
9. `agora harness-check abstractcode-tui --live` reports **DRIVABLE** with
   `identity` PASS and no `evidence` limitation.

## Suggested implementation

- **`src/cli.rs:153-234`** — three flags in the existing match: `--tools <csv>`
  (accumulating), `--agora-agent <slug>` (validate at parse), `--json`. Add each
  to `usage()`.
- **`src/run_input.rs`** — `StartOpts.agora_agent: String`; insert into the
  `runtime` map beside `thinking` when non-empty. The `runtime.len()` pin at
  `:439` says *"a new `_runtime` writer must extend this test"* — this is that
  writer.
- **`src/exec.rs:429`** — replace `tools: None` with the parsed list (`None`
  when the flag is absent). Thread `--json` through `print_new`/`print_item` as
  an alternate emitter; leave the prose path untouched.
- **`abstractruntime/core/runtime.py:3705`** — add `agora_agent` as the fifth
  rider, copying the `operator_email` block verbatim.
- **Docs** — state that the identity value is a **non-secret alias** and that
  the credential lives in the gateway host env. That matches the contract:
  passing a seat id is expected, passing a key is a finding.

## Not in scope

Local tool execution and a local MCP client. The TUI has neither and needs
neither — server-side tools plus an alias are sufficient.

## Separate decision, not a blocker

`_runtime.agora_agent` is currently **unguarded client input**: any gateway-token
holder can name any alias whose key is in the host env. Once the rider lands,
that becomes "any token holder can post as any seat". The gateway already pops
`operator_email` for exactly this reason. Decide whether to allowlist aliases
per principal or to document the host as single-tenant — but decide it before
several seats exist, not after.
