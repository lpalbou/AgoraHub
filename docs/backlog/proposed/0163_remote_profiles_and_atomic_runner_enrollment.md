# 0163 — Remote profiles and atomic runner enrollment

**Status:** proposed — **architecture and security review required before
implementation.**
**Created:** 2026-08-24
**Trigger:** a clean-room hub test exposed that a remote runner is described
with the hub host's `--home`, while a fresh database at a reused URL collides
with cached credentials from the previous hub. Two independent adversarial
simulations then traced local, remote, multi-hub, and same-endpoint replacement
flows.

## Plain description

Agora's remote execution direction is sound: an operator records a request for
a named machine, and a human-started runner on that machine pulls it. The hub
never opens SSH, starts a remote process, or interprets the remote filesystem.

The attachment and configuration model around that mechanism is not sound
enough. `home` currently means both a hub host's deployment state and a
client's local credential/runtime directory. URLs stand in for hub identity,
even though one hub can have several URLs and a fresh hub can reuse an old
one. Workspace wiring records configuration that `drive` later ignores. The
documented remote-runner ceremony is ordered in a way the hub rejects.

The public model should distinguish a server deployment from a client
attachment, enroll a runner and its machine assignment atomically, and make a
wired workspace or runner profile sufficient for steady-state commands.

## Current code reality

### The remote-runner direction and authorization are already good

- A spawn request carries `machine`; `--machine mbp` already routes work to the
  runner assigned to `mbp` (`src/agora/cli.py:2394-2534`,
  `src/agora/hub/http_api.py:455-494`).
- A hub stores one runner-seat assignment per machine. Only that authenticated
  seat may announce capabilities, claim requests, or update their state
  (`src/agora/hub/service.py:848-950`).
- A claim returns a short-lived, single-use join token pinned to the requested
  child seat. The runner cannot mint arbitrary identities
  (`src/agora/hub/service.py:1135-1166`).
- The remote runner owns the confinement root, harness allowlist, capacity,
  permissions, and approval policy. These are local consent and safety
  boundaries, not values the hub should choose (`src/agora/runner.py:560-593`).

This proposal must preserve that pull/claim design.

### The documented remote ceremony cannot run in its stated order

`docs/spawning.md` currently says:

```text
host:   invite runner-mbp
host:   spawn --set-runner mbp=runner-mbp
remote: join the invitation
remote: start runner-mbp
```

`invite` only mints a token. It does not create the seat. The seat is created
when the remote machine redeems the token. `set_machine_runner()` explicitly
requires the agent to exist and otherwise returns 404
(`src/agora/hub/service.py:874-895`).

The only working order is therefore:

```text
host invite -> remote join -> host set-runner -> remote runner
```

That host/remote/host/remote handshake is an onboarding defect, not a
fundamental security requirement.

### Host home and remote home are different concepts

The `AGORA1` artifact carries URL, join token, optional agent id, channels,
and expiry. It deliberately carries no host path, database path, admin key, or
final seat key (`src/agora/join.py:1-69`).

On the hub host, `--home` selects deployment state:

```text
config.json admin credential and DB pointer
database default
hub-written notification files
local management credentials and runtime state
```

On a remote machine, `--home` selects unrelated client-local state:

```text
cached seat keys
pinned endpoint
listener/driver/runtime files
client preferences
```

The two paths can never be shared across machines. Calling both `home` exposes
an internal storage layout as if it were a distributed identity.

### Join can create a mixed local profile

`run_join()` caches a key and calls `save_url()` in the selected local home
(`src/agora/join.py:315-342`). `save_url()` replaces only the URL while
preserving any existing `admin_key` and `db_path`
(`src/agora/config.py:68-76`; this preservation is pinned by
`tests/test_config.py`).

Joining a remote hub into a home previously used to host another hub can
therefore produce:

```text
url       = remote hub B
admin_key = old local hub A
db_path   = old local hub A
```

The join banner's claim that the pinned profile is "url only" is true only for
a genuinely fresh client-local home.

### Workspace wiring is not authoritative in steady state

Current source writes seat, URL, harnesses, and a non-default local home into
`.agora/seat.json`; harness MCP configuration also receives that local home
without embedding a bearer (`src/agora/setup_harness.py:555-590, 614-653`).

`run_drive()` consumes the workspace seat and URL but does not apply the
workspace's recorded home before constructing `Driver`
(`src/agora/drive.py:4218-4282`). Key lookup, listener state, sessions, locks,
PIDs, and logs consequently come from the ambient/default home. `listen` has
the same split. A correctly wired remote workspace still needs to repeat its
local `--home`.

When setup used the default home, the seat record stores `home: null`. A later
ambient `AGORA_HOME` can therefore redirect a supposedly wired workspace. A
binding should identify its local profile positively, including the default.

### Runner children work through hidden environment inheritance

The runner's in-process join caches every child key in the runner's selected
home and writes the profile reference into the child workspace
(`src/agora/runner.py:275-306`). The launcher passes child seat, harness,
permissions, and URL to `agora drive`, but passes no home. It relies on the
runner process having inherited `AGORA_HOME` from its own `--home`
(`src/agora/runner.py:250-271`).

This happens to work for one runner process. It is brittle coupling and makes
several hubs on one remote machine require separately selected homes and
carefully isolated runner environments even though each child workspace
already has a binding.

### URL is an endpoint, not hub identity

Credentials are indexed by the literal normalized `URL::seat` string
(`src/agora/config.py:117-128`). There is no durable hub-instance identity in
the database-facing client contract, health response, join artifact, or local
profile.

This fails in both directions:

- `127.0.0.1`, a LAN address, and a DNS name for one hub become three credential
  identities.
- A fresh database at the same URL inherits the old cache identity.

The replacement case can loop permanently:

1. Hub A at URL U leaves cached key `U::runner`.
2. A fresh hub B starts at U.
3. B issues a valid invitation for `runner`.
4. `join` finds the stale cached key and skips redemption.
5. Verification against B fails.
6. Retrying the still-valid invitation again skips redemption.

The cached-key shortcut precedes verification and does not quarantine or
replace the stale entry (`src/agora/join.py:315-342, 412`). The client also
sends the old bearer to the replacement before learning that it is wrong.

### Admin selection can cross the endpoint boundary

Admin-backed CLI commands resolve explicit admin key, then ambient
`AGORA_ADMIN_KEY`, then the selected home's stored admin key. The current
resolver does not bind a selected admin credential to the target URL before
sending it (`src/agora/cli.py:1023-1033`). A mistyped or overridden URL can
therefore disclose an unrelated hub's administrator bearer.

`--new-admin-key` is only a server-start precedence override. It generates a
new admin credential even when ambient or remembered credentials exist; it
does not create a database, profile, or hub identity. On an existing
deployment it rotates that deployment's admin credential while leaving hub
data and seat keys intact (`src/agora/cli.py:105-115, 352-416`). It is not a
remote-onboarding or environment-isolation primitive.

## Problem

Bootstrap currently exposes too many independently typed values:

```text
host home + client home + URL + seat + machine + root + DB pointer
```

Some are real choices; others are duplicated selectors for state already
known by the hub, invitation, runner, or workspace. The duplication creates
wrong-environment failures and makes a remote machine appear to need a path
that exists only on the hub host.

There is also no stable answer to "is this the same logical hub?" That is the
root of both endpoint-alias fragmentation and stale credential reuse after a
fresh database replaces an old one.

## Proposed direction

### 1. Separate server deployments from client attachments

Use two explicit records:

```text
Server deployment profile — exists only on the hub host
  hub_id
  database path
  admin credential reference
  bind and advertised endpoints
  notification/runtime state

Client attachment profile — exists on each client or runner machine
  profile name
  hub_id
  trusted endpoint aliases
  local seat credential references keyed by hub_id + seat
  local runtime namespace
  no host database path
  no host admin credential
```

`--home` may remain a compatibility/storage flag, but it should not be the
public distributed identity. A named local `--profile` is the explicit
selector only when context is ambiguous.

### 2. Give every database a durable hub identity

A newly created database receives a random `hub_instance_id`; restoring that
database preserves it, while creating a fresh database generates another.
The safe discovery response and enrollment artifacts carry this identity.

Credential and runtime namespaces become `hub_id + seat`, with endpoints as
verified aliases. If a known URL presents a different hub id, the client must
quarantine the stale credential and report "hub replaced; re-enroll" before
sending a bearer.

An unauthenticated UUID detects accidents but not hostile endpoint takeover.
Preventing credential disclosure under takeover additionally needs
authenticated TLS or a pinned hub signing/public key. The design pass must
decide that trust model explicitly.

### 3. Make runner enrollment atomic

Proposed command shape, for discussion rather than implementation commitment:

```bash
# Hub/operator machine
agora machine invite mbp --url https://hub.example

# Remote machine
agora machine join AGORAM1... \
    --root ~/agora-seats \
    --harness opencode \
    --max-seats 3

# Later restarts
agora runner start mbp@hub-profile
```

The scoped machine artifact should pin the hub identity, reachable endpoint,
machine assignment, runner seat, expiry, and one-time enrollment capability.
Redemption atomically creates the runner seat and activates the machine
mapping. There is no second host-side `set-runner` race.

The remote-local root, harness allowlist, cap, permissions, and approval
policy are chosen and stored locally. A hub invitation must not be able to
widen them.

For a local runner, the same operation may be exposed as a local enrollment
command using the already-selected administrator profile; it should not need
an artifact pasted back onto the same machine.

### 4. Make profiles and workspace bindings authoritative

After enrollment:

```bash
agora runner start mbp@hub-profile
```

After workspace setup or runner-created child enrollment:

```bash
cd WORKSPACE
agora drive
```

Drive, listen, hooks, TUI, and other clients must consume the complete
binding atomically: profile, hub id, endpoint, seat, and local credential
namespace. They must reject conflicting ambient selectors rather than mixing
URL from one source with credentials/runtime state from another.

Runner children should receive an explicit profile reference in their
workspace binding, not depend on inheriting the parent's environment.

### 5. Keep machine routing explicit where it is real intent

The operator should continue to choose placement when it matters:

```bash
agora spawn oc1 --machine mbp --harness opencode --mission "..."
```

The operator's current workspace or named client profile supplies hub and
identity. `--machine mbp` is not configuration leakage: it is the routing
decision. The machine registry/capability surface remains authoritative for
which targets and harnesses are available.

### 6. Treat bootstrap and steady state differently

Bootstrap legitimately needs a trusted endpoint, one-time enrollment
credential, requested identity/scope, and a local place to keep credentials.
The artifact should assemble those pieces rather than asking a remote human to
retype them.

Steady state should not repeat URL, filesystem home, seat, and machine
assignment. A wired workspace needs no selector; a machine-wide process uses
one profile name only when several attachments make the choice ambiguous.

## Scope

- Define server-deployment, client-attachment, runner-profile, and workspace-
  binding boundaries.
- Define persistent hub identity, endpoint aliases, credential namespace, and
  replacement behavior.
- Replace the four-step remote-runner handshake with scoped atomic enrollment.
- Make `drive`, `listen`, runner children, and client surfaces consume complete
  bindings without ambient-source mixing.
- Correct remote-runner and environment documentation once the contract is
  decided.
- Supply migration and compatibility behavior for existing homes, key caches,
  join artifacts, workspaces, and runners.

## Non-goals

- No hub-to-hub federation or remote process push. Agora remains one central
  hub with remote clients, consistent with ADR-0001's proposed Model A.
- No SSH keys, daemons, launchd/systemd services, or hub-owned remote paths.
- No relaxation of runner-local root, allowlist, capacity, permission, or
  approval gates.
- No copying the hub administrator key to a remote machine.
- No assumption that URL alone authenticates a hub or selects a seat.
- This proposed item authorizes no implementation or migration yet.

## Decision boundaries and open questions

1. Is `hub_instance_id` sufficient for accidental replacement detection, with
   TLS required for hostile networks, or should every hub also have a pinned
   signing identity?
2. Does one client profile contain several seats on one hub, or does each seat
   get a profile? Runtime files must remain collision-free either way.
3. Are endpoint aliases learned only through an authenticated hub response, or
   explicitly approved by the user?
4. Does `machine join` start the foreground runner immediately or only persist
   local policy for a later `runner start`?
5. Which runner-policy fields may be persisted, and which safety-sensitive
   choices must be repeated as explicit consent each session?
6. How are legacy `URL::seat` entries migrated without sending a stale bearer
   to an unverified replacement endpoint?
7. How should a remote machine attached to two hubs select concurrent runner
   profiles without shared environment inheritance?
8. Should server `--home` be renamed `--state-dir` while retaining `--home` as
   a compatibility alias?

## Promotion criteria

Promote to `planned/` only after:

- the maintainer approves the server-profile/client-profile boundary;
- a proposed ADR records hub identity, endpoint trust, profile precedence,
  and atomic runner enrollment;
- the migration behavior for current homes and URL-keyed credentials is
  specified;
- AgoraTUI and AgoraWUI owners confirm the shared profile contract they can
  consume;
- an adversarial security review attacks endpoint replacement, credential
  disclosure, enrollment replay, malicious machine names, and local policy
  escalation;
- an end-to-end implementation sequence is split into independently testable
  items if the work no longer fits one change.

## Validation ideas

### Remote runner ceremony

- Hub invites `mbp`; remote redeems once; the seat and machine mapping appear
  atomically; no second host command is required.
- Replayed, expired, wrong-seat, and wrong-machine artifacts are refused
  without partial registration.
- A remote runner starts from its profile without `--home`, `--url`, `--as`,
  or a repeated machine name.

### Routing and local safety

- `spawn --machine local` is claimed only by the local runner;
  `spawn --machine mbp` only by the remote runner.
- The hub cannot choose or widen the remote root, allowlist, cap, permissions,
  or approval mode.
- Two runner profiles for two hubs operate concurrently without sharing keys,
  locks, listener offsets, sessions, or child workspaces.

### Identity and endpoint behavior

- Loopback, LAN, and DNS endpoints authenticated as the same hub share one
  credential namespace.
- A restored database at a new endpoint preserves identity and credentials.
- A fresh database at an old endpoint is detected before any cached bearer is
  sent, stale credentials are quarantined, and a fresh invitation can redeem.
- A hostile endpoint cannot claim an existing hub identity without satisfying
  the selected TLS/signing trust rule.

### Workspace and client behavior

- `agora drive` and `agora listen` work with no selectors from a wired local or
  remote workspace.
- Conflicting ambient variables are ignored with a diagnosis or refused; no
  mixed-source connection is attempted.
- TUI/WUI/CLI resolve the same profile and never ask for a host filesystem
  path on a remote machine.
- Joining into a home that contains unrelated server admin/DB metadata is
  refused or migrated explicitly, never silently mixed.

### Compatibility and documentation

- Existing `AGORA1` seat invites retain a bounded compatibility path.
- Existing `--home` workflows continue to work during migration with explicit
  deprecation diagnostics rather than silent reinterpretation.
- `docs/spawning.md`, `docs/getting-started.md`, `docs/environments.md`, CLI
  help, startup banners, TUI docs, and WUI docs describe one consistent
  bootstrap and steady-state contract.

## Dependencies and related work

- Proposed ADR-0001: one central meeting-point hub; remote machines are
  clients, not federated hubs.
- `docs/spec/standalone-bootstrap-contract.md`: current home + URL + seat
  contract that this proposal would revise.
- `docs/spawning.md`: current runner workflow and ordering defect.
- `docs/getting-started.md`: `AGORA1` invite/join onboarding.
- `docs/environments.md`: current server/client-home conflation.
- `docs/backlog/planned/federation/0030_federated_named_agent_identity.md`:
  remote identity/security work that must not duplicate this profile layer.
- `docs/backlog/proposed/0118_protocol_sdk_roadmap.md`: shared client contract
  and generated SDK direction.

## ADR status

- **Governing ADR:** ADR-0001 (Proposed), limited to the one-central-hub
  topology. This item does not implement federation.
- **ADR impact:** needs a new ADR before promotion. Durable hub identity,
  endpoint trust, server/client profile separation, selector precedence, and
  machine enrollment are cross-task rules and must not remain only in backlog
  prose.

## Guidance for future agents

Re-run the local, remote, multi-hub, endpoint-alias, and same-URL replacement
simulations against the then-current installed package, not only the checkout.
Preserve the runner's pull/claim authorization and local-consent boundaries.
Do not "simplify" by scanning arbitrary home directories, sending cached
bearers before hub verification, copying host state remotely, or hiding
ambiguous selection behind last-used global configuration.
