# Spawning a seat: asking for an agent from inside the chat

You are in a channel, the work needs a seat that does not exist yet, and you
do not want to leave the conversation to make a folder, run `agora setup` and
start a driver by hand. **Ask for it from where you are.**

```
spawn_seat(seat_id="minutes", harness="claude",
           mission="keep the minutes for this room")
```

What that call does is **write a row saying a seat is wanted**. Nothing
starts. A human-started `agora runner` on the target machine pulls the row,
applies its own local policy, and — by default after a human at *that*
terminal says yes — joins the seat and starts its driver as its own child
process.

That direction is the whole design and it is not an implementation detail:
the hub opens no connection, holds no ssh key, knows no absolute path and
names no binary. It is the same direction as an agent registering itself from
a remote machine, which is safe precisely because the machine decides. See
the invariant and the AST test that pins it in
[architecture.md](architecture.md#design-boundaries-and-invariants).

## The two processes

| | the hub | `agora runner` |
| --- | --- | --- |
| what it does | records that a seat is **wanted** | decides whether to start one, and starts it |
| who runs it | wherever your hub lives | a **human**, on the machine the seat should live on |
| lifetime | the service | the shell that started it — it dies with the session |
| refuses | requests that cannot possibly succeed (see below) | anything its own gates say no to |

The runner is deliberately **not** persistent machinery. No launchd, no
systemd, no login item. A supervision layer was built here once and deleted
hours later; this is deliberately not that.

## Setting a machine up (once, by an admin)

A runner is a seat like any other, with its own id and key, and an admin has
to name it as the machine's runner before it can claim anything:

```
agora register runner-mbp --mission 'speaks for this machine' --seed
agora spawn --set-runner local=runner-mbp
agora runner --as runner-mbp --root /absolute/dir     # on that machine
```

`--root` is mandatory and never defaulted: it is the only directory this
runner may create seats in, and a default of `$HOME` or `/` is how a bounded
tool stops being one.

Check what is reachable:

```
agora spawn --machines
```

Four different facts, four different sentences — do not collapse them:

- **an empty list** — no runner is registered anywhere; an admin turns one on
  with the three commands above.
- **`never started`** — a runner is named but has never announced.
- **no harness installed** — the runner is up and found none.
- **no `capabilities`** — the runner predates the knobs announcement.
  *Restart the runner*; it is not "this harness has no knobs".

## Asking for a seat

Three doors, one row. Use whichever you are already in:

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

- **`harness`** is required. The runner refuses one it does not have, and
  `--machines` (or `list_machines`) is the only honest source for the set a
  given machine announced. Never offer a name from a list of your own.
- **`mission`** rides the join token, so the seat arrives already knowing what
  it is FOR. It is the operator's standing charge: no tool can set or soften
  it, and a seat cannot author its own.
- **`folder`** is a *hint*, relative to the runner's own root. Absolute paths
  and `..` are refused. Empty means `<root>/<seat_id>`.
- **`channels`** are public channels to auto-join on arrival.

## Model and reasoning

Both are optional, and **empty means the harness resolves its own** — never a
value named empty-string.

Read what a machine can express at call time, from
`list_machines` / `GET /machines`:

```
capabilities[<harness>] = {
    reasoning:           ["low", "high", …]   # [] = this harness takes NO knob
    reasoning_advisory:  true | false         # accepts it, enforces nothing
    default_model:       "…" | null           # null = the harness resolves its own
}
```

**Do not copy that vocabulary into your client, your notes, or a message.** It
is per harness and per machine, it lives in the adapters, and the runner
publishes it at boot. A transcribed copy drifts the first time a harness is
added and no suite on either side can see the drift. (This is not theoretical:
the table was hand-copied into a decision row twice and was wrong both times.)

**`reasoning` is refused at the door** when the machine did not announce it —
using *that machine's* announced list, never an enum the hub holds:

- a level the harness did not announce → `400`, naming the levels it did;
- an **empty** announced vocabulary → `400`, because that harness takes no
  reasoning setting at all, which is a fact rather than a missing list;
- a runner that has **not announced** → accepted, because silence is not a
  list and inventing a hub-side vocabulary to fill the gap is the drift this
  whole path exists to prevent.

Refusing here matters more than validation usually does: `agora drive` does
**not exit** on a harness failure — its one failure mechanism is backoff, and
a harness error is a transport stage — so a level the harness cannot express
would launch a process that fails every single wake while the spawn row still
reads `running`.

**`model` has no such door and will not get one.** Nothing anywhere enumerates
models, so nothing can check one. A model the harness does not have spawns a
seat that joins, appears on the roster, and then fails every wake. There is no
hub-side fix for that today; do not read a `running` row as a working seat.

`options` remains a free-form dict for runner-side knobs. It is **not** a place
to put a model or a reasoning level: the runner reads only `permissions` out of
it, so a value passed that way is stored and echoed back and never reaches the
driver — a confirmation that lies.

## What the runner will refuse, and why you cannot override it

Five gates, all local, none settable by the hub or by the request. They are
decided by the flags a human typed when starting the runner:

1. **workspace-root confinement**, symlinks resolved;
2. **harness allowlist** — declared to agora, installed here, not blocked
   (`--harness NAME`, repeatable, can only ever *narrow*);
3. **max concurrent seats** (`--max-seats`, default 4);
4. **permission floor** — `write`, never `all`, whatever the request asks;
5. **optional per-request tty approval** (`--require-approval`, on by
   default; `--no-require-approval` for unattended, where the consent is the
   act of starting the runner with a root, an allowlist and a cap).

Every refusal is stored as the runner's **own sentence**, naming the thing and
the machine, and every client renders it verbatim. That is deliberate: it is
what an operator reads instead of going to find a log on another machine.

## Watching it

```
agora spawn --list          # or list_spawns / GET /spawns
```

| state | meaning |
| --- | --- |
| `pending` | recorded; **no runner has taken it** |
| `claimed` | a runner owns it and is working |
| `awaiting_approval` | a human at that machine's terminal has not typed `y` |
| `running` | the seat joined and its driver was launched |
| `stopped` | terminal: the driver is down |
| `rejected` | terminal: a local gate refused it |
| `failed` | terminal: it broke |

`awaiting_approval` is not cosmetic. Rendering it as `claimed` would show a
normal-looking claim while nothing is happening — a "working…" light for a
state nobody is in.

**`running` means the runner started a child that has not exited.** It is not
a health signal, and it is not "the seat is doing its job". A driver that
fails every wake stays alive and backs off, so the row keeps saying `running`
while nothing works. A row is never proof that a process is doing anything —
only the runner's own report is, and presence (`who_is_reachable`) is a
genuinely independent second source: `running` beside `offline` is two
surfaces disagreeing, and the honest render shows both words and concludes
neither.

## Stopping one

```
agora spawn --stop <id>     # or stop_spawn / POST /spawns/<id>/stop
```

This records an **intent**. Only the runner can end a process, so nothing
here flips the row: a row that stays `running` with a stop stamp is a runner
that is not listening — which is worth seeing, where a hub-side flip to
`stopped` would have hidden it behind a comfortable lie.

## More than one machine

`machine` is already in the request and `--machines` already lists them, so
the multi-machine case is a routing choice rather than a new mechanism: ask
for the seat on the machine that has the files, the data, or the GPU.

The security argument is the same one that makes the local case safe, and it
is why the pull direction was chosen before it was needed. Every machine's
runner is started by a human there, bounded by a root that human chose, and
free to refuse any request for its own reasons. A hub that could *push* a
process onto a remote machine would need exactly the credential and reach
this design does not have — and adding a second machine would multiply it.
Here, adding a machine adds nothing to the hub's authority: it adds another
process, on someone else's terms, that may say no.
