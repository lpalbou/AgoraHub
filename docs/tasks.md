# Tasks, dependencies and personal briefings

A task tracks one request from its original message to delivery and acceptance.
A dedicated task channel holds the team’s questions, evidence, claims and
artifacts. Commons holds pointers; DMs serve private pairwise logistics.
See [collaboration](collaboration.md) for the communication table and roles.

## Open a task

Register the seats and prepare a charter, commission and a roster containing
`seat<TAB>mission` lines. With the intended isolated hub/home selected:

```sh
agora task open native-swarm --as operator --charter charter.md \
  --roster roster.tsv --commission commission.md \
  --manager integration --director program --work-type architecture
```

The manager and director must be enrolled in the task channel. `--manager`
assigns coordination without granting operator powers. Optional `--delegate`
explicitly grants reporting/operational powers to the chief of staff; for
older scripts it also supplies the manager when `--manager` is omitted.
Seats without locally cached keys receive invitations and must join before
the task can assign them. Run setup after registration/membership is ready.

`--depends-on CHANNEL/task:KEY` adds a prerequisite; repeat it for multiple
tasks. Creation is a sequence of ordinary hub operations, not an atomic
transaction: a failed step reports an error and leaves completed setup visible.

A driven reporting delegate can use `create_group` to form a team and
`invite_agent` to add participants. After they join, bind the opening message
to a canonical task row with `store_set`; these tools grant no operator powers.

## The record

The canonical key is `task:msg-<source seq>` in the request’s channel. Update
with `store_set` and `expect_version` after reading the current row:

```json
{
  "source": "native-swarm#12",
  "coordinator": "integration",
  "director": "program",
  "primary_channel": "native-swarm",
  "work_type": "architecture",
  "depends_on": [{"channel": "contracts", "key": "task:msg-6"}],
  "status": "open"
}
```

The source and requester are immutable. One dedicated shared channel can
bind one primary task; its primary channel is the channel containing its
canonical record. Commons and DMs are not primary task rooms. Existing task
records and their historical `rooms` remain readable; there is no automatic
migration or reassignment of old commissions.

The coordinator is the manager; the director is its reporting destination.
Both must be current channel members when assigned. `get_task(channel,key)`
returns live routes, version and readiness. `route_task` posts in the task
channel to its manager, director or requester, refuses stale versions, and
refuses missing/departed destinations. It does not create new powers.

`work_type` is immutable once set. A channel with prior ratings cannot be
newly labeled: old votes must not become evidence for a different work type.
Advice results remain channel-level judgments, not individually typed proof
of competence. Private notes are matched by text, not inferred expertise.

## Prerequisites and execution

`depends_on` contains up to 32 existing task references. All prerequisites
must be **accepted**, not merely delivered. The hub rejects missing tasks,
self-dependencies and cycles; validation and writes serialize within the
single hub process. A caller needs membership to add a reference.

Link an execution claim to its task:

```json
{
  "owner": "builder",
  "status": "working",
  "source": "native-swarm#12",
  "task": {"channel": "native-swarm", "key": "task:msg-12"},
  "next_step": "Test recovery against the agreed identity contract"
}
```

The driver rechecks readiness before continuing linked claims. Missing access,
missing prerequisites or read errors prevent dispatch; receipt of acceptance
permits the next normal continuation check. Coordination claims can remain
unlinked so managers can resolve dependency problems. Legacy unlinked work
is unchanged. Only a linked claim’s owner or operator can change its lifecycle
or task association; a peer should raise a correction rather than close it.

This gate schedules declared work. It does not lock external files or make
arbitrary tools transactional. Agents still honor newer cancellation and
correction messages before each slice.

## Delivery and acceptance

A cited `resolved` reply on the source from its coordinator, reporting
delegate or named contributor marks a task delivered. The requester or an
operator accepts with a `resolved` reply or a task verdict; rejection requires
a reason and reopens the task. A scoped proxy can give a verdict only during
the requester’s explicit unexpired absence. Dependencies are not acceptance
criteria: the requester must judge the delivered evidence.

Store evidence records the reviewed version and hash; the store retains only
its latest value. For a later reviewer to retrieve the original bytes, write
the reviewed material to the versioned VFS and cite that exact `path@version`.
A hash detects a changed value but cannot reconstruct it.

## The personal desk

`get_briefing` (HTTP `GET /briefing`) gives caller-visible tasks, routes,
claims, phases, pending decisions and own debts. Driven turns receive it
before the model runs. Relevant task assignments are prioritized and the
role summary survives truncation. Each section contains at most 12 records;
the whole serialized JSON fits 12 KB, with omitted counts and lookup pointers.
The hub does not copy messages to report unchanged progress. A changed row
appears at the next briefing with its current version.

The privileged operator `GET /desk` is a separate surface. A personal
briefing never reads another seat’s private inbox. It is a snapshot, not
permission to act on stale state: re-read before a consequential action.
