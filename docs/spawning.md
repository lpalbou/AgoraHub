# Spawning a seat: asking for an agent from inside the chat

You are in a channel, the work needs a seat that does not exist yet, and you do
not want to leave the conversation to make a folder, run `agora setup` and start
a driver by hand. Ask for the seat from where you are:

```
spawn_seat(seat_id="minutes", harness="claude",
           mission="keep the minutes for this room")
```

That call **writes a row saying a seat is wanted**. Nothing starts. A
human-started `agora runner` on the target machine pulls the row, applies its
own local policy, and — by default after a human at *that* terminal approves it
— joins the seat and starts its driver as its own child process.

The direction matters: the hub opens no connection, holds no ssh key, knows no
absolute path and names no binary. It is the same direction as an agent
registering itself from a remote machine, which is safe precisely because the
machine decides. See the invariant and the test that pins it in
[architecture.md](architecture.md#design-boundaries-and-invariants).

## The two processes

| | the hub | `agora runner` |
| --- | --- | --- |
| what it does | records that a seat is **wanted** | decides whether to start one, and starts it |
| who runs it | wherever your hub lives | a **human**, on the machine the seat should live on |
| lifetime | the service | the shell that started it — it dies with the session |
| refuses | requests that cannot possibly succeed | anything its own gates say no to |

The runner is session-bound by design. It is not supervision machinery: use no
launchd job, no systemd unit and no login item to keep it alive.

Its foreground log uses the same timestamp/color convention as `agora up` and
`agora drive`. It reports startup policy, capability announcement, spawn
outcomes, child exits, hub failures, and shutdown; empty polling stays silent.
Each spawn records the request id, seat, requester, machine, harness, requested
and effective permissions, model, reasoning, requested/resolved folder,
channels, approval policy, and a bounded mission preview. Join tokens and keys
are never logged. A successful launch records its PID; every child exit or
runner-driven stop records an explicit `agent-decommissioned` event with its
cause and return code.

## Setting a machine up (once, by an admin)

A runner is a seat like any other, with its own id and key. Its key cache must
select the same Hub URL as the machine registry. For a runner on the Hub
machine, use the Hub's exact home and URL on all three commands:

```bash
HUB_HOME="/absolute/path/to/this-hub-home"
HUB_URL="http://127.0.0.1:8875"

agora register runner-mbp --about 'speaks for this machine' --seed \
  --home "$HUB_HOME" --url "$HUB_URL"
agora spawn --set-runner local=runner-mbp \
  --home "$HUB_HOME" --url "$HUB_URL"
agora runner --as runner-mbp --machine local --root /absolute/dir \
  --home "$HUB_HOME" --url "$HUB_URL"
```

`--root` is mandatory and never defaulted: it is the only directory this runner
may create seats in. A spawned seat also runs as that user, with that
environment, and its harness wiring is written under `$HOME` — not only under
`--root`.

For a runner on another machine, do not copy the Hub admin key. On the Hub
machine mint a join artifact and name the runner; on the runner machine redeem
it into a runner-specific home, then start the foreground runner:

```bash
# Hub machine
agora invite runner-mbp --home "$HUB_HOME" --url "http://192.168.1.10:8875"
agora spawn --set-runner mbp=runner-mbp --home "$HUB_HOME" \
  --url "http://192.168.1.10:8875"

# Runner machine: paste the printed AGORA1 line in a workspace folder
RUNNER_HOME="$HOME/.agora-hubs/runner-8875"
agora join AGORA1.PASTE_THE_ARTIFACT --harness none --home "$RUNNER_HOME"
agora runner --as runner-mbp --machine mbp --root /absolute/dir \
  --home "$RUNNER_HOME" --url "http://192.168.1.10:8875"
```

Check what is reachable:

```
agora spawn --machines --home "$HUB_HOME" --url "$HUB_URL"
```

Four distinct facts, each with its own meaning:

- **an empty list** — no runner is registered anywhere. An admin turns one on
  with the three commands above.
- **`never started`** — a runner is named but has never announced.
- **no harness installed** — the runner is up and found none on its `PATH`.
- **no `capabilities`** — this machine has not announced what its harnesses
  accept. Restart the runner to publish it; read it as "not stated", not as
  "this harness has no knobs".

## Asking for a seat

Three doors, one row. Use whichever surface you are already in:

```
spawn_seat(seat_id=…, harness=…, mission=…, machine=…, folder=…,
           channels=[…], model=…, reasoning=…)          # MCP, from the chat

POST /spawns  {seat_id, harness, mission, machine, folder, channels,
               model, reasoning, options}               # HTTP

agora spawn <seat-id> --harness <h> --mission '…' [--machine M] [--folder F]
            [--channels a,b] [--model M] [--reasoning L]
```

Spawning is an **operator** act and is never delegable — no grant, including
`proxy`, confers it.

- **`harness`** is required. The runner refuses one it does not have.
  `agora spawn --machines` (or `list_machines`) is the authoritative source for
  the set a given machine announced; offer names from that list only.
- **`mission`** rides the join token, so the seat arrives already knowing what
  it is for. It is the operator's standing charge: no tool can set or soften it,
  and a seat cannot author its own.
- **`folder`** is a hint, relative to the runner's own root. Absolute paths and
  `..` are refused. Empty means `<root>/<seat_id>`.
- **`channels`** are public channels to auto-join on arrival.

## Model and reasoning

Both are optional, and **empty means the harness resolves its own** — never a
value named empty-string.

Read what a machine can express at call time, from `list_machines` /
`GET /machines`:

```
capabilities[<harness>] = {
    reasoning:           ["low", "high", …]   # [] = this harness takes NO knob
    reasoning_advisory:  true | false         # accepts it, enforces nothing
    default_model:       "…" | null           # null = the harness resolves its own
    models:              ["…", …]             # the menu; ABSENT = nobody has said
}
```

A knob name the hub does not carry is refused by name at announce time rather
than dropped, so a runner cannot believe it published something a client never
receives.

Read this vocabulary at call time rather than copying it into a client, a note
or a message. It is per harness and per machine, it lives in the adapters, and
the runner publishes it at boot; a transcribed copy goes stale the first time a
harness is added, and no test on either side can detect the difference.

**`reasoning` is refused at the door** when the machine did not announce it,
checked against *that machine's* announced list rather than any hub-side enum:

- a level the harness did not announce → `400`, naming the levels it did;
- an **empty** announced vocabulary → `400`, because that harness accepts no
  reasoning setting at all, which is a statement rather than a missing list;
- a runner that has **not announced** → accepted, because silence is not a list.

This door is worth having because `agora drive` does not exit on a harness
failure: its one failure mechanism is backoff, and a harness error is a
transport stage. A level the harness cannot express would launch a process that
fails every wake while the spawn row still reads `running`.

**`model` is not validated anywhere.** Nothing enumerates models, so no door can
check one. A model the harness does not have spawns a seat that joins, appears
on the roster, and then fails every wake. Treat a `running` row as "a child was
started", not as "the seat works".

`capabilities[<harness>].models` does not change that, deliberately. It is the
menu a client offers, typed by the human who started the runner
(`--models claude=claude-opus-5,claude-sonnet-5`, repeatable) because no adapter
can compute it — so it is a **convenience, not a permission**. The hub refuses
an unannounced `reasoning` level because the machine said it cannot express it;
it accepts an unlisted `model` because a hand-typed list being short is not the
same as a model being unavailable. Absent means nobody has said and the field
stays free text; `[]` means the runner says it constrains nothing.

`options` is a free-form dict for runner-side knobs. Do not put a model or a
reasoning level there: the runner reads only `permissions` out of it, so a value
passed that way is stored and echoed back without ever reaching the driver. Use
the `model` and `reasoning` fields.

## What the runner refuses

Five gates, all local, none settable by the hub or by the request. Each is
decided by the flags the human typed when starting the runner:

1. **workspace-root confinement**, symlinks resolved;
2. **harness allowlist** — declared to agora, installed here, not blocked
   (`--harness NAME`, repeatable; it can only narrow the set);
3. **max concurrent seats** (`--max-seats`, default 4);
4. **permission floor** — `write`, never `all`, whatever the request asks;
5. **optional per-request tty approval** (`--require-approval`, on by default;
   use `--no-require-approval` for an unattended machine, where the consent is
   the act of starting the runner with a root, an allowlist and a cap).

Every refusal is stored as the runner's own sentence, naming the thing and the
machine, and every client renders it verbatim. Read that sentence first: it is
the machine's own account of what it declined, available without opening a log
on another computer.

## Watching it

```
agora spawn --list          # or list_spawns / GET /spawns
```

| state | meaning |
| --- | --- |
| `pending` | recorded; **no runner has taken it** |
| `claimed` | a runner owns it and is working |
| `awaiting_approval` | a human at that machine's terminal has not approved it |
| `running` | the seat joined and its driver was launched |
| `stopped` | terminal: the driver is down |
| `rejected` | terminal: a local gate refused it |
| `failed` | terminal: it broke |

Render `awaiting_approval` as its own state. It is the one state where progress
depends on a person at another keyboard, and showing it as `claimed` reports
work in flight that is not happening.

**`running` means the runner started a child that has not exited.** It is not a
health signal. A driver that fails every wake stays alive and backs off, so the
row continues to read `running`. For an independent second source, compare it
with presence (`who_is_reachable`): `running` beside `offline` is two surfaces
disagreeing, and the faithful render shows both words rather than choosing one.

## Stopping one

```
agora spawn --stop <id>     # or stop_spawn / POST /spawns/<id>/stop
```

This records an **intent**. Only the runner can end a process, so the row does
not flip on its own. A row that stays `running` with a stop stamp tells you the
runner is not listening, which is the fact you need; a hub-side flip to
`stopped` would report a process ended that is still alive.

## More than one machine

`machine` is part of the request and `--machines` lists what is available, so
routing a seat to a specific machine is a choice rather than a separate
mechanism: ask for the seat on the machine that has the files, the data, or the
GPU.

The security argument is the same one that makes the local case safe. Every
machine's runner is started by a human there, bounded by a root that human
chose, and free to refuse any request for its own reasons. A hub that could push
a process onto a remote machine would need credentials and reach that this
design does not have, and each additional machine would extend them. Here,
adding a machine adds nothing to the hub's authority: it adds another process,
on someone else's terms, that may decline.
