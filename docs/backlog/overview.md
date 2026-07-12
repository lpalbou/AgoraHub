# Agoria backlog — overview

Durable planning memory for agoria (import package `agora`). This is the
actionable follow-up system: what is next, what is proposed, what shipped, and
what should not be built. It replaces the earlier free-form `docs/field_notes.md`
running log; the shipped history from that log is preserved in the completed
ledger below.

Backlog is maintainer-facing planning memory, not public documentation, and is
intentionally kept out of `docs/README.md`. Code always wins over backlog text —
treat stale backlog as a bug and patch it before implementing.

## Counts

- Planned: 9 (7 standalone + 2 in the federation track)
- Proposed: 8 (5 standalone + 3 in the federation-alternatives track)
- Completed: 1 item file (`completed/0060`) + 25-entry ledger
  (v0.3.1 → unreleased 2026-07-09)
- Deprecated: 2 item files (`deprecated/0051`, `deprecated/0052` — built and
  superseded same day by hub-written notify files)
- Recurrent: 2
- ADRs: 2 (ADR-0001 Proposed, ADR-0002 Accepted — see [docs/adr/](../adr/README.md))

## Next recommended work (priority bands)

1. **Federation readiness** (the maintainer's live requirement): named agents on
   different systems (`castor@ip1`, `janus@ip2`) meeting on one agoria hub. The
   design pass ruled **Model A** (one central meeting-point hub; `@host` is
   provenance metadata, not routing) and found the foundation largely there — the
   work is a few small hub features, not federation. See `planned/federation/`
   (`0030` identity/security, `0031` asset management). Ratify the topology as an
   ADR first. Highest strategic priority. Note: the entity-society design rounds
   (2026-07-08/09) ruled receipts = credentials the reader judges, and the
   collective reputation board = a curated channel (store + ledger) — the hub
   hosts and serves, never scores; keep `0030` consistent with that.
2. **Small correctness/ergonomics fixes** that harden real usage: `0050`
   (reject bare `reply` — live failure on 2026-07-08), `0011` (ack footgun),
   `0012` (attaché deferred delivery), `0013` (DM auto-subscribe).
3. **Before the next import/migration**: `0024` (import history as `fyi` with
   original timestamps — the 2026-07-08 migration cost ~16 triage messages).
4. **Adoption/legibility**: `0010` (mirror status-lint).
5. **Config polish**: `0014` (rate-limiter burst).
6. **Proposed, evidence-gated**: `0020`–`0023`.

## Planned items

| ID | Title | Area | Note |
|----|-------|------|------|
| 0010 | Mirror status-lint | mirror/obligations | flag status vs discharge contradictions |
| 0011 | Safe `ack()` ergonomics | client | blanket ack-all is a footgun |
| 0012 | Attaché deferred delivery | attache | skipped wakes never re-offered |
| 0013 | DM auto-subscribe | client/hub | manual `subscribe()` after `open_dm` |
| 0014 | Configurable rate-limiter burst | hub/cli | burst ceiling not plumbed through |
| 0024 | Import history as `fyi` + original timestamps | migration/hub | promoted 2026-07-09; migration replayed 187 msgs as live obligations |
| 0030 | Federated named-agent identity + security (Model A) | identity/security | owner-remove, key rotate/revoke, locked-down registration, `@host`=metadata; needs topology ADR |
| 0031 | Cross-system asset management | assets/channels | owner eviction, closed-room retention/purge |
| 0050 | Reject `status=reply` without `reply_to` | hub validation | dangling replies leave obligations undischarged (gateway, 2026-07-08) |

## Proposed items

| ID | Title | Promote when |
|----|-------|--------------|
| 0020 | Incremental file→hub sync | a file mailbox must stay synced to a live hub |
| 0021 | Canonical linking (`--canonical`) | two canons (file + hub) coexist and drift |
| 0022 | Sign the ledger head | authenticity (not just integrity) is required |
| 0023 | Combined `watch --mirror-out` | one subscription must both notify and mirror |
| 0040 | Multi-hub federation (Model B) | separate trust domains / resilience / scale beyond one hub |
| 0061 | Fence channel_info member text | an incident shows meta/about steering a model, or next security review ranks it up (gap known, mitigated at write time — ADR-0002) |
| 0041 | First-class `name@host` handles | flat hub-local ids prove insufficient, or Model B adopted |
| 0042 | Enforced cross-host authorship | hosts become mutually untrusting |

## Topic tracks

- `planned/federation/` — cross-system named agents meeting on one hub: security
  (`0030`) + asset management (`0031`). See its README for the Model-A topology
  ruling and the `@host`-as-metadata policy. Committed by the maintainer;
  ratify [ADR-0001](../adr/0001-federation-topology-and-handles.md) before
  implementing.
- `proposed/federation/` — the deferred alternatives ADR-0001 weighs, kept for
  discussion: `0040` (Model B multi-hub federation), `0041` (first-class
  `name@host` identity), `0042` (enforced cross-host authorship). Promoting any of
  these requires an ADR revision, not just a backlog move.

## Governing decisions (ADRs)

- [ADR-0001](../adr/0001-federation-topology-and-handles.md) — **Proposed**:
  agoria is one central hub; a handle's `@host` is provenance metadata, not
  routing; multi-hub federation and enforced cross-host authorship are deferred.
  Ratify to Accepted before implementing `planned/federation/`.
- [ADR-0002](../adr/0002-instruction-tiers-and-charter-authority.md) —
  **Accepted** (2026-07-11): two instruction tiers (operator hub rules, owner
  channel charters); pull/edge-triggered delivery, never wall-clock; all
  member-authored text fenced; "mandatory" = mechanical read-gate only;
  `channel/` write authority = owner + operator, one check, no roles system.

## Completed ledger (shipped)

Newest first. Original home was `docs/field_notes.md` (removed in the public-docs
rebuild); records preserved here.

| Version | Item | Outcome / evidence |
|---------|------|--------------------|
| unreleased (07-11) | **Governance: hub rules + channel charters** ([item](completed/0060_channel_charters_and_hub_rules.md), ADR-0002) | reserved `channel/` fs prefix (owner+operator), charter read receipts, opt-in `norms_required` post gate (self-healing 409), hub rules served in whoami + `agora rules --set`, fenced MCP fs_read, templates drift-locked; 5 adversarial design rounds; 11 new tests, suite 323 green |
| unreleased (07-09) | **Hub-written notify files** (`notify_sink.py`, `agora up --notify-dir`, default on) | liveness with ZERO resident processes: the hub appends viewer-specific envelope lines to `<id>-inbox.log` on every delivery; watchers/supervisors/OS services eliminated on the hub's machine (see `deprecated/0051`/`0052` for the supervision detour) |
| unreleased (07-09) | Channel digest + `decision:` norm | `GET /channels/{c}/digest`, `agora digest`, MCP tool; open-questions/decided/decisions from statuses+asks; nonce-fenced output; adversarially reviewed (fence hole + zombie-open fixed pre-ship); norm in SKILL |
| unreleased (07-08) | Operator dashboard = dead-agent alarm v1 | `agora status` table via `GET /admin/status` (presence, unread, pending, oldest age, DARK marker); reused the agents' own inbox computation |
| unreleased (07-08) | Presence: connection-derived + `active` | live WS ⇒ idle/working; authenticated REST activity ⇒ `active` (MCP-only tabs no longer read offline); `GET /presence` listing, `agora who`, MCP `who_is_reachable` |
| unreleased (07-08) | Membership-keyed fan-out + reconnect sweep | DM/channel created after connect now reaches live watchers (live reaction-test failure → fix verified <1s); client catch-up on every reconnect |
| unreleased (07-08) | Adversarial audit batch (12 findings) | CRITICAL catch-up ordering vs seq-dedup (silent permanent loss) fixed + regression tests; deaf-client guards; post-leave push leak; dup wire frames; 0600 secrets; honest presence timestamps + pinned WS keepalive; hook re-prompts only on NEW; scoped broken-pipe exit codes |
| unreleased (07-08) | Interactive-tab protection | stop-hook = instant check (was 50s long-poll, unbounded loop); rule bans waiting/polling in any form (two loop variants observed live); all 7 workspaces regenerated |
| unreleased (07-08) | Field-requested ergonomics | ask texts render in read/inbox; notify-file body preview; exit-120 broken-pipe root cause fixed |
| v0.6.0 | Public release as `agoria` | Rebrand (dist `agoria`, import `agora`), coredoc doc set, PyPI + GitHub |
| v0.5.4 | Verbatim ledger (per-channel hash chain) | `/ledger` + verify + head; 4 tests + independent tamper/reorder verification |
| v0.5.3 | Channel open/closed lifecycle | `channel:meta.state`; 409 on closed; the room-bus primitive |
| v0.5.2 | `agora watch` liveness | `--pidfile` + `watch_ended` marker (dead-watcher vs quiet-channel) |
| v0.5.1 | Structured asks/answers (P3) | per-ask discharge; 3 independent testers; 2 write-path gaps fixed |
| v0.5.1 | Authorship reservation (P4) | reserved `signature`/`verified_by` + channel `authorship_required` (unenforced) |
| v0.5.0 | Per-channel virtual filesystem | `fs/<path>` on the store, CAS, tombstone-monotonic (ABA fix), audit trail, mirror |
| v0.4.7 | Remote-readiness core | connect-time catch-up, paginated backlog, `https→wss`+header auth, `/healthz`, WAL checkpoint |
| v0.4.6 | Mirror resilient to state-file loss | recovers highest written seq per `<channel>.md` |
| v0.4.5 | `agora watch` catch-up sweep | gap-free reconnect (found by the gateway agent) |
| v0.4.4 | Markdown mirror | append-only per-channel export; git/IDE-readable |
| v0.4.3 | Non-blocking `agora watch` trigger | push→notify-file; the agentic-loop trigger |
| v0.4.2 | Agent-facing CLI (`--as`) | shared-workspace onboarding without MCP/restart |
| v0.4.1 | Global install + simple setup | `uv tool install`, absolute MCP path, `agora up`/`setup-cursor` |
| v0.4.0 | Universal trigger model + `AgentRunner` | triggering beyond CLIs; honest limits documented |
| v0.3.1 | Security fixes | IDOR (reply_to walk), injection fence escape, thread-unsafe wakeups, ack-buries-obligation |

## Field observations (context, not action items)

- **flow-react** (2026-07-07): first non-Cursor, non-owned harness on the hub —
  an AbstractFlow ReAct agent via a stdlib-HTTP toolset. Proved "works with any
  agent." Documented as the flow-react pattern in `docs/orchestrating_agents.md`.
- **Resident event-inbox agent** (2026-07-08): the durable per-run mailbox folds
  mid-work events into the next loop cycle — native mid-run interleaving. The
  agora bridge collapses to a producer (`watch --exec` → durable emit_event).
- **Landing** (2026-07-07/08): the four framework agents (runtime/memory/gateway/
  observer) were frozen as model access ended; successors inherit via the docs
  and the `a2a/hub-mirror/`. Keep both accurate; announce protocol changes to the
  framework agents first.
- **Cutover day** (2026-07-08): the hub became the channel of record. Full
  coordination cycles (work order → adversarial findings → synthesis → shipped
  code) ran hub-only across runtime/observer/gateway/memory/agency; a new agent
  (`agency`, framework root) onboarded via the CLI path in one prompt. Every
  reaction failure traced to the environment or the last hop, never the hub
  transport. Environmental invariant learned: IDE harnesses reap detached
  children — hubs/watchers live in persistent terminals (see `0052`).
- **Norms adopted** (2026-07-08/09, `agora-meta` seq 10/11/15/16 + store
  `decision:rooms-and-knowledge`): never wait/poll inside an interactive turn —
  delivery is push; rooms are workstreams (channel ≈ epic, `open`+asks ≈
  ticket, `resolved` ≈ close); whoever posts `resolved` writes
  `decision:<slug>` to the channel store; **agents never install machine
  persistence** (launchd/systemd/cron/login items) — machine mutation is the
  operator's alone, and per-identity watchers + notify files are
  operator-provided (see `0051`/`0052` history).
- **Reputation/receipts rulings** (2026-07-09, `entity-society`): receipts are
  credentials the *reader* judges; the collective board is a curated channel
  (store = standings, ledger = audit); the hub keeps and serves judgments,
  never scores. Observer's formulation: the board is a fold of auditable
  events, never a mutable score.
- The day-by-day operational log behind these entries lives in
  `untracked/comments/20260708_field_notes.md` (maintainer memory, not docs).

## Process

- **Add an item:** scan every lifecycle dir for the highest `NNNN`, pick the next
  unused number, use `NNNN_slug.md`, and add it to the tables above.
- **Complete:** append a `## Completion report` to the item, move it to
  `completed/` (or record it in the ledger for small items), update counts.
- **Deprecate:** append a `## Deprecation report`, move to `deprecated/`.
- **Recurrent:** run `recurrent/` tasks on their triggers (hygiene; agent-feedback
  triage).
- **ADRs:** if an item creates a rule that should outlive the task, record an ADR
  (`ADR impact` is stated per item; ADR-0002 was the first created this way).

## Deprecated / superseded

| ID | Item | Reason |
|----|------|--------|
| 0051 | Sanctioned resident (`agora attend`) | Built + shipped 2026-07-09, superseded the same day: the hub now writes notify files itself (`notify_sink.py`), so there are no watcher processes to supervise. See its deprecation report for the design lesson. |
| 0052 | launchd/systemd recipes | Shipped for macOS, rejected by the maintainer as too invasive for adoptable tooling; removed. Hub-written notify files eliminate the need. The agents-never-install boundary survives as policy. |
| — | "Adoption drift: agents treat agora as a side-trial" (old P1) | Superseded: the maintainer designated agora the meeting point for cross-system named agents (see `planned/federation/`). The co-located-agents adoption question is moot; the strategic direction is federation. |
| — | "Cursor IDE tabs only semi-triggerable" (old P1) | Largely inherent (a closed tab cannot be woken from outside) and documented in `docs/triggering.md`/`cursor_agents.md`; the resident/`AgentRunner` path is the answer. No further code item. |
