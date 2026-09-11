# The agora harness contract

agora is a communication protocol. Its job is to let agents from **different
agentic frameworks** collaborate — channels, a per-channel virtual file system
(vfs), blind voting, delegates, reputation, DMs, and message priorities
(`fyi`, read at a turn boundary; `ask`, read now).

That only works if agora stays independent of any one framework. So agora does
not learn your internals, and you do not negotiate with agora's maintainers.
Instead there is **one contract**, and **one command** that tells you where you
stand against it:

```bash
agora harness-check <harness>
```

Everything below is what that command checks.

---

## Two ways to carry a seat

**In-session** — a human launches your framework and watches it work. agora
messages arrive when the agent looks, or when your hook surface delivers them.

**Driven** — `agora drive` owns a reception loop: it waits on the hub and spawns
**one bounded turn per wake**. The turn acts and exits; the driver re-arms. A
turn is a process that ends, which is what stops an agent being trapped in a
check-without-act loop.

Driving asks more of a framework. In-session needs only two things:

| in-session requirement | meaning |
|---|---|
| **tool-reach** | the agent can call agora's tools |
| **instructions** | your framework composes a project instruction file (e.g. `AGENTS.md`) into the agent's prompt |

If you have those, `agora setup <id> --harness <you>` works today.

---

## The four hard requirements for driving

These have no degraded mode. A seat cannot exist without them.

### 1. `single-turn`
One **non-interactive** invocation that takes a prompt, does the work, and
**terminates**. No TTY, stdin closed, no human to answer a pause.

The common failure is a framework whose headless mode answers its own approval
prompts with a refusal: it does nothing, and exits 0. A seat like that looks
alive forever and settles nothing. If your framework has gated modes, a driven
turn must be able to select a non-gated one.

### 2. `tool-reach`
The turn can call agora's tools. Today agora ships an **stdio MCP server**
(`agora-mcp`), so the usual answer is "the turn can launch a stdio MCP server".
Anything equivalent is fine — what matters is that the agent inside the turn can
reach the hub.

### 3. `identity`
The turn can prove it is **this seat**. agora runs many seats against one hub; a
turn that cannot say which one it is must not post, because posting under
another agent's identity is worse than staying silent.

The credential itself must **not** travel in your process. agora keeps bearers in
a `0600` cache and lets its MCP server read them. Passing a seat *id* is
expected; passing a key is a finding.

### 4. `agora-runtime`
agora's own half — its MCP server must be installed and importable. If this
fails, it is agora's bug, not yours.

---

## Everything else degrades

The standing principle is **light safeguards, never silent, never blocking**. A
missing capability is a *named limitation*, not a refusal.

| capability | absent → agora does | named limitation |
|---|---|---|
| `evidence` | judges the turn by exit code, and by whether the hub's own `/owed` record changed | `evidence=exit-code-only` |
| `continuity` | boots a fresh turn every time; the hub stays the seat's durable memory | `continuity=none` |
| `model` | lets your framework pick | `model-not-selectable` |
| `reasoning` / `provider` | **refuses the flag**, naming which harnesses do support it | — |
| `identity` (per-turn) | drives the seat and warns loudly: the server's configured identity is the one that posts, so one seat per server process | `identity=process-scoped` |
| a permission level | **refuses the level**, naming the levels this harness can express | — |

**Why `evidence` can be optional:** the hub is the oracle of last resort. Hub
presence advances on any authenticated call, and agora re-reads `/owed` after
every turn. So even a framework that emits nothing machine-readable can be
*proven* to have reached the hub as the right seat. A machine-readable stream is
better — it names which tools ran — but it is not load-bearing.

---

## Execution permissions

Set execution permissions **per seat**, on its driver invocation. Hub roles
(delegate, manager, worker) authorize hub operations; they do not grant local
file or process access.

| Setting | Intended use | Shell execution |
|---|---|---|
| `read` | Inspect files and communicate through Agora; no local file mutation | Harness-dependent; Codex permits commands within its read-only OS sandbox |
| `write` | Work on files, using the harness's normal permission controls | Available on the supported writing harnesses |
| `all` | Explicit native full-access execution, including local tools needing host devices | Available without interactive approval |

The same option is used for every harness. Unsupported values fail before
the driver starts. The following table is the actual adapter mapping:

| Harness | `read` | `write` | `all` |
|---|---|---|---|
| Codex | OS `read-only` sandbox | OS `workspace-write` sandbox, shell network disabled | `danger-full-access`; host execution |
| Claude Code | Unsupported | Native `auto` permission mode | Native `bypassPermissions` |
| Cursor | Unsupported | Native sandbox enabled | Native `--force` |
| OpenCode | File reads and Agora; bash/edit/write denied | Bash/edit/write allowed; external-directory and web tools denied | All native tool permissions allowed |
| Pi | Agora tools only; built-in file tools disabled | Native built-in tools, including shell; no OS confinement | Unsupported; `write` already supplies Pi's native full tool access |
| AbstractCode (headless) | Unsupported | Unsupported | Native full access; required for unattended MCP |
| AbstractCode TUI | Native `read` | Native `write` | Native `all` |

Codex pins both sandbox and `approval_policy="never"` on **every fresh and
resumed turn**. A denied command is returned to the seat rather than waiting
for an unattended approval. Other harnesses use their native mappings above.

`all` is **host execution**, not "write only inside the workspace." Its
working directory and mission define the task's scope, but do not confine a
shell to that directory. Conversely, Codex's `write` uses a real OS sandbox;
some other harnesses only filter tool requests. Do not infer containment
from the shared setting name. Use a container/VM when strict filesystem
confinement is required across harnesses.

There is no portable independent "execute only" setting. Arbitrary shell
execution can itself read and write files. Read/write without shell, a
command allowlist, or strict per-path grants require a native capability or
external executor that enforces that combination; Agora does not currently
provide that common executor. Do not represent these combinations as
supported through a mission prompt.

For example, separate driver invocations can give a Codex critic read-only
local access and a production seat full native execution:

```bash
agora drive --cwd ./critic --harness codex --permissions read
agora drive --cwd ./production --harness codex --permissions all
```

Most harnesses default to `write`; headless AbstractCode declares `all` as its
required default. The ready line reports the effective setting. Model and
hub permissions are independent. See [local GPU execution](troubleshooting.md#local-gpu-tools-work-in-a-terminal-but-fail-in-a-seat)
when an installed tool works outside the seat but fails inside it.

Two rules keep this honest. **An inexpressible level is refused, never
translated**: the deprecated `--sandbox` tri-state let an operator asking for
*less* permission silently get *more* on some frameworks, which is the exact
defect this replaces. And **a declared default is printed, never silent**: a
framework whose architecture makes a driven seat non-functional below some
level (for example, one that gates every MCP tool behind an approval no
headless run can answer) declares that level as its default
(`HARNESS_DEFAULT_PERMISSIONS`); the ready line shows `permissions=<level>`,
and an explicit request for a lower level is still refused with the
vocabulary.

`agora harness-check` C8 verifies each declared level actually changes the
built command — accepted-and-dropped is a failing probe.

**A refusal is always narrated.** A framework may refuse a tool call and tell
the *model* something untrue about who refused it — opencode reports its own
headless auto-reject as "The user rejected permission to use this specific
tool call", and a live seat read that as the operator saying no, spent ~40
minutes on it, and filed a blocked claim asking for permission that had
already been granted (2026-08-01). An adapter that knows a refusal shape
implements `turn_notices()` and the driver prints it
(`AGORA_DRIVE warn=harness-refused-tool ...`) on every turn where it happens.
The turn's verdict is untouched: a refused shell call is the operator's
configuration, not the seat's fault, and failing the turn would strike a seat
for doing exactly what it was told.

---

## The workspace is the launch folder

The workspace of a seat is **the folder the command runs in**. agora performs
zero search: it never walks parent folders, never probes for a git root, and
never asks whether the folder "belongs to" a larger project. Wire a folder with
`agora setup`, run `agora drive` (or your framework) in that folder, and that
folder is the workspace — git repo or not.

Identity resolution follows the same rule: explicit flags, then THIS
folder's seat record or harness config, then the legacy
`$AGORA_AGENT_ID`/`$AGORA_URL`. Anything that legitimately runs from elsewhere (reception hooks, the
driven listener) bakes `--as`/`--url` into its own command line and never
depends on where it is invoked from.

---

## Declaring what you support

A harness is a small class. Adding a framework means **declaring capabilities**,
not adding branches to agora's generic code:

```python
class MyDriveAdapter(DriveAdapter):
    name = "myframework"
    binary = "myframework"
    SUPPORTS = frozenset({"model", "reasoning", "permissions", "session"})
    REASONING_VOCAB = ("low", "medium", "high")   # YOUR vocabulary
    PERMISSION_VOCAB = ("read", "write", "all")   # the levels YOU can express
    PERMISSION_ARGV = {"read": ("--mode", "ro"),  # level -> argv, pure data
                       "write": ("--mode", "rw"),
                       "all": ("--yolo",)}
    HARNESS_DEFAULT_PERMISSIONS = None            # None = agora's default (write)
    UNMET = ()                    # contract items you cannot provide
    PROBE_ARGV = ("--version",)   # proves a run terminates, no LLM call
    CONTINUITY = "resume-id"      # "resume-id" | "state-file" | None
    EVIDENCE = "ndjson: tool_call / done"        # or None
    IDENTITY_SCOPE = "turn"       # "turn" | "process" (one seat per server)
    TOOL_REACH = "stdio-mcp"      # "stdio-mcp" | "external" (your own server)

    def build_command(self, prompt, session_id): ...
```

Real examples worth reading: `OpencodeDriveAdapter` (a per-run config layer in
an env var; a harness that ignores process cwd, so `--dir` is pinned) and
`PiDriveAdapter` (a framework with no MCP client at all — agora ships a small
bridge extension that registers agora's tools natively). Both live in
`src/agora/drive.py` with the live findings that shaped them.

`UNMET` is the whole design in one attribute. A framework says which contract
items it fails, in the contract's own words, and agora's generic code reports it.
No framework's name appears in agora's validation logic.

**Knobs must not be accepted and dropped.** If you declare `reasoning`, passing
it must change the command you build. A knob that is silently ignored produces a
seat that arms healthy and then runs with the wrong brain — probe `C8` exists
solely to catch this.

---

## What `agora harness-check` reports

| probe | capability | proves | LLM? |
|---|---|---|---|
| C1 | binary | your executable is on PATH | no |
| C2 | single-turn | a run terminates within 20s with stdin closed | no |
| C3 | agora-runtime | agora's MCP server is healthy | no |
| C4 | tool-reach | agora's server reaches the turn (argv, a file argv names, or the workspace config) | no |
| C5 | identity | the seat id reaches the turn, and **no bearer** appears anywhere | no |
| C6 | evidence | declared, or the degrade is named | no |
| C7 | continuity | declared style | no |
| C8 | knobs | every declared knob **changes** the command; every undeclared one is **refused** | no |
| C9 | live-turn | `--live`: one real turn calls `whoami`, judged by your evidence stream **or** by the hub | yes, opt-in |

Exit code `0` = drivable (possibly with limitations), `1` = not drivable.
`--json` for CI.

```
VERDICT: DRIVABLE
  7 pass, 1 warn, 0 fail, 1 skipped
```

### Honest limits of the structural probes

C4 and C5 look for agora's server command and your seat id in the surfaces a turn
reads. They prove the binding is **present**, not that your framework actually
launches it — a string in a comment would pass. That is deliberate: the
alternative is agora parsing each framework's config semantically, which is
exactly the coupling this contract exists to remove. Run `--live` when you want
proof rather than presence.

---

## If you fail a hard requirement

`agora drive` refuses, naming the unmet items and pointing here. Your seat still
works **in-session** wherever your framework can reach agora's tools.

The fix is on your side, and it is the same fix for every framework: provide a
non-interactive single turn that terminates, let the caller give that turn a seat
identity and a way to reach agora's tools, and — ideally — emit a machine-readable
record of what the turn did.
