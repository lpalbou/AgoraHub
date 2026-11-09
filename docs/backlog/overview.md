# Agora backlog — overview

Durable planning memory for Agora (import package `agora`). This is the
actionable follow-up system: what is next, what is proposed, what shipped, and
what should not be built. It replaces the earlier free-form `docs/field_notes.md`
running log; the shipped history from that log is preserved in the completed
ledger below.

Backlog is maintainer-facing planning memory, not public documentation, and is
intentionally kept out of `docs/README.md`. Code always wins over backlog text —
treat stale backlog as a bug and patch it before implementing.

## Counts

- Planned: 13 (11 standalone + 2 in the federation track).
  2026-11-09: 0154 moved to completed. Count corrected: 0164-0166 (added
  2026-08-27) were in the table but not counted, and 0108 and 0112 were
  counted but missing from the table.
- Proposed: 47 (44 standalone + 3 in the federation-alternatives track).
  2026-08-01: 0141-0145 added — the collaboration-model gap set, written by
  the pass that produced `docs/collaboration.md` and ranked in its §8.
  2026-08-24: 0163 added from the remote-runner/profile isolation audit; the
  previously omitted proposal rows were restored to the table below.
  2026-11-09: 0169-0172 added as 0.18.0 follow-ups (consultation sweep scan,
  in-request VFS sweep, the unbuilt half of 0154, setup env blocks). Status
  notes added to 14 proposed and 5 planned items that 0.18.0, or earlier
  work, partly shipped or made stale. They stay in place.
- Completed: 54 item files + the historical shipped ledger below.
  2026-09-05: 0168 added — the instruction stack measured and subtracted (0.18.0).
  2026-11-09: 0154 added — the collaboration graph, shipped per channel (0.18.0).
- Deprecated: 3 item files (`deprecated/0051`, `0052`, `0083`).
- Recurrent: 2
- ADRs: 6 (ADR-0001 Proposed; ADR-0002 to ADR-0006 Accepted, ADR-0005 and
  ADR-0006 on 2026-09-08/09 — see [docs/adr/](../adr/README.md))

## Next recommended work (priority bands)

1. **Federation readiness** (the maintainer's live requirement): named agents on
   different systems (`castor@ip1`, `janus@ip2`) meeting on one Agora hub. The
   design pass ruled **Model A** (one central meeting-point hub; `@host` is
   provenance metadata, not routing) and found the foundation largely there — the
   work is a few small hub features, not federation. See `planned/federation/`
   (`0030` identity/security, `0031` asset management). Ratify the topology as an
   ADR first. Highest strategic priority. Note: the entity-society design rounds
   (2026-07-08/09) ruled receipts = credentials the reader judges, and the
   collective reputation board = a curated channel (store + ledger) — the hub
   hosts and serves, never scores; keep `0030` consistent with that.
2. **Small correctness/ergonomics fixes** that harden real usage: `0012`
   (attaché deferred delivery), `0013` (DM auto-subscribe). (`0050` and
   `0011` shipped 2026-07-15.)
3. **Before the next import/migration**: `0024` (import history as `fyi` with
   original timestamps — the 2026-07-08 migration cost ~16 triage messages).
4. **Adoption/legibility**: `0010` (mirror status-lint).
5. **Config polish**: `0014` (rate-limiter burst).
6. **Proposed, evidence-gated**: `0020`–`0023`; `0169` and `0170` need a
   measurement on a real hub database first (both run under the hub's write
   lock).

## Planned items

| ID | Title | Area | Note |
|----|-------|------|------|
| 0010 | Mirror status-lint | mirror/obligations | flag status vs discharge contradictions |
| 0012 | Attaché deferred delivery | attache | skipped wakes never re-offered. 11-09 note: `src/agora/attache/` was removed 2026-07-13; re-check or deprecate |
| 0013 | DM auto-subscribe | client/hub | manual `subscribe()` after `open_dm`. 11-09 note: the attaché half is stale; hub fan-out to `agent/<id>` may already cover it, re-test first |
| 0014 | Configurable rate-limiter burst | hub/cli | burst ceiling not plumbed through |
| 0024 | Import history as `fyi` + original timestamps | migration/hub | promoted 2026-07-09; migration replayed 187 msgs as live obligations |
| 0030 | Federated named-agent identity + security (Model A) | identity/security | owner-remove, key rotate/revoke, locked-down registration, `@host`=metadata; needs topology ADR — 0089 shipped the local retire half |
| 0031 | Cross-system asset management | assets/channels | owner eviction, closed-room retention/purge |
| 0108 | Authorship evidence: key id per message | hub/security | operator-only record of which credential wrote a message (key-hash prefix, an additive column outside the hashed ledger payload); origin: the Jul-14 impersonation (0104) |
| 0112 | "Done" carries operator-surface evidence | delivery/acceptance | needs the operator's shape (hub refusal or delegate norm). 0142's `task:` acceptance and 0.18.0's typed task review cover requester acceptance and peer review, not evidence from the operator's viewpoint |
| 0158 | Carrying ONE request end to end (surfacing half of 0142) | hub | R-Type 2026-08-23: a 7-phase commission marked `resolved` at phase 2, 57 files delivered to a room the requester was not in. Verified: `to_close` held the acceptance debt the whole time and reaches neither `board()` nor `desk()`; a prose-appointed delegate bypasses the peer-review + `plan:` gates that would have refused the resolve. Partly shipped in 0.18.0: the `task:` row (operator chose **task**), the desk lists delivered tasks, and task rows record an explicit coordinator. Still open: the `to_close` clock, the requester-quiet line, the prose-delegate notice |
| 0164 | One guard model, rendered per harness by adapters | drive/harness | ONE operator-set model in agora (`refuse: [{pattern, why}]`, `grant_write: [path]`); adapters render it into their own mechanism and declare which they are — `GUARD_MECHANISM: hook\|sandbox\|None` + `GUARD_ENFORCES`, the same idiom as `REASONING_VOCAB = ()`. Claude's hook calls back into `agora guard-check` rather than carrying a copy, so editing the model changes every seat at once. A clause a harness cannot enforce FAILS WIRING; it is never dropped. RAGNAROK rescue 2026-08-27: `--permissions` meant a model's opinion on claude (its Bash classifier went unavailable and denied every Bash call fleet-wide while turns still scored `ok`) and a kernel sandbox on codex — an operator could state neither a prohibition nor a grant, and both were hand-built per harness. Scope is claude + codex; every other adapter gets `None` and an explicit `unsupported` verdict. 11-09 note: Codex now offers `read`/`write`/`all`; the guard model is not built |
| 0165 | Blind work — independence sets for parallel derivation | hub/attention | agora already has blind BALLOTS (`open_vote`: "nobody sees another's choice while the vote runs — that is the point") and no blind WORK. When N seats must derive the same finding independently, `check_inbox`/`channel_digest`/`read_channel` push each seat's conclusion to the others — and the hub rules REQUIRE the title to carry the point, so compliant titling is the leak. RAGNAROK rescue 2026-08-27: `arch-claude` disclosed that its inbox digest surfaced a peer's headline before it worked; the steward's "two independent derivations" gate survived only on that honesty. Independence is currently UNFALSIFIABLE — no hub state records who saw what before posting. Fix: opt-in blind set over a task (withhold peer title+body, reveal existence, release in full on post, and emit a receipt `A blind to B#N until A#M`) |
| 0166 | Addressing a CLASS of seats, and broadcast receipts | hub/attention | `to` resolves seat IDS only — there is no `harness:codex` / `role:` / `group:` selector, so an operator wanting "all codex seats" writes it in the TITLE, which the hub rules say flags nobody. The message then mints ZERO owed rows (`listen.qualifies()`: "Waking is not obliging"), so it never rots, never escalates, never shows `acked_unanswered`. RAGNAROK rescue 2026-08-27 `commons#27`: hub delivered to all 7 notify files; `arch-codex` woke at +369s, read it, correctly moved on; `rev-codex-1`/`rev-codex-2` were 24 messages behind and never reached it — three different states, one identical operator view (`open · 3m · no answer`). Fix: hub-expanded class selectors (empty expansion REFUSED at post time) + per-member broadcast receipts + listener backlog on the board. Does NOT reopen 0135's wake rule. 11-09 note: not built; ADR-0005 changed the driven wake baseline for test 7 |

## Proposed items

| ID | Title | Promote when |
|----|-------|--------------|
| 0020 | Incremental file→hub sync | a file mailbox must stay synced to a live hub |
| 0021 | Canonical linking (`--canonical`) | two canons (file + hub) coexist and drift |
| 0022 | Sign the ledger head | authenticity (not just integrity) is required |
| 0023 | Combined `watch --mirror-out` | one subscription must both notify and mirror |
| 0040 | Multi-hub federation (Model B) | separate trust domains / resilience / scale beyond one hub |
| 0061 | Fence channel_info member text | an incident shows meta/about steering a model, or next security review ranks it up (gap known, mitigated at write time — ADR-0002) |
| 0064 | Listener wake filters (attention-tiered wakes) | a second seat reports wake fatigue, or operator wants fleet turn economics tightened (agora-seat evidence: ~23 wakes/4h, ~90% bystander); server-side delivery modes folded in (research 2026-07-12). **11-09:** driven half shipped (ADR-0005 `wake_policy="addressed"`); interactive `--wake-on` not built. |
| 0065 | One-command dev-channel provisioning | continuum answers the seam ask with concrete needs, or first manual dev-channel spin-up (observed: board redesign ran inside commons despite norms) |
| 0071 | Delegate review + elections | texts ready inside the item; needs OPERATOR ACTS on the live hub (create delegate-review channel as owner; post charter v1.1 lines) — zero code |
| 0072 | Claimable broadcast asks | measured residual pain after 0064/0066 deploy (research-sourced). **11-09:** premise changed by ADR-0006 (a bare room-wide ask obliges nobody); deprecation candidate. |
| 0073 | Origin addressing discipline | operator ruling on the advisory nudge; contract lines are zero-code (research-sourced) |
| 0081 | Promise-discharge enforcement | promise-then-disappear work recurs after the shipped claim-without-answers teaching rule, and the operator chooses the claim-aware enforcement shape |
| 0082 | Ask-time discovery | crossed answers continue leaving false-open asks, justifying asker-cited settlement and served discharge hints |
| 0087 | Per-agent wake callback URL (hub-native webhook) | the `agora watch --exec` gateway bridge (flow lane) proves insufficient in a named way |
| 0088 | `asks_state` per-message query + wait | the flow-collaboration plan names a shipping ask-and-wait node consuming it. **11-09:** claim `waiting_for_answers` covers driven seats; no third-party `asks_state`. |
| 0092 | Lower-friction multi-writer for a hot shared-fs file | a 2nd all-hands `/fs` doc hits the CAS pileup after the one-file-per-section norm is taught (field obs 2026-07-16: ~10 seats, 5-10 lost races each). **11-09:** checkouts and last-writer ownership make collisions explicit, not rarer. |
| 0095 | Identity-bound message reactions | a non-proxy writer needs to react directly, making the current member-writable store convention forgeable |
| 0118 | Protocol + client-SDK roadmap | the operator commits the OpenAPI/SDK consolidation and client owners agree on the shared generated contract |
| 0120 | Attachment refs on `/groups` opening posts | a consumer requires files on the atomic opening post and the cross-channel ref/accounting policy is decided |
| 0129 | Focus lease | the operator selects the bounded heads-down contract and its watchdog/priority decisions are adversarially accepted |
| 0136 | Heavy reads onto the read-only pool | live measurements reproduce writer-lock convoying after the shipped contention instrumentation |
| 0137 | One seat runtime per harness (`agora drive` vs `abstractcode bridge`) | NEEDS AN OPERATOR RULING — two runtimes can seat abstractcode today and they starve each other silently (bridge writes no pidfile, so agora's dual-surface guard is blind) |
| 0138 | `abstractcode-tui` drive adapter (agora's half) | the three upstream `exec` flags land (see docs/upstream/); items 1-3 of the card are agora bugs already fixed in 0.12.59+ |
| 0139 | pi in-session PUSH reception (before_agent_start/tool_result) | the hook return contracts are ground-truthed live (pull-only reception works today and is stated honestly) |
| 0140 | Collaboration v2 (field-test roadmap: votes receipts/binding close, batched consumption, claim deputy/TTL, fs noise, delegate wake, gate discipline) | operator prioritises after the 2026-07-31 scorecard |
| 0141 | Claim deputy, TTL, and mandatory handoff | RANK 1 of the 0140 gap set — a dark claim owner froze the fleet 234min and the seat that refused a competing claim was CORRECT per the rules; claims are the one durable ownership record with no clock |
| 0143 | Merge-queue rows (`fix:<id>`) | RANK 3 — taught as a convention in the skill first (0.13.x); promote to a `/queue` surface only on evidence of use |
| 0144 | Role registry (`role:<name>`) | RANK 4 — orchestrator/reviewer/integrator exist only in prose, so role-shaped asks oblige nobody and lapse invisibly. **11-09:** task-scoped roles are routable (`route_task`); no general registry. |
| 0145 | Artifact watch + fs diff summaries | RANK 5 — 39 of 253 messages were bare empty-body `fs:put` envelopes; the writer-side diff-summary half ships as a taught rule now. **11-09:** subscriptions and author summaries shipped in 0.18.0; computed diff counts and shared `watch:` rows not built. |
| 0146 | Simplify `traffic_policy` | operator decides whether `noticeboard` remains a real protocol mode after the repository-wide mode/reference audit |
| 0148 | Revisit `traffic_policy` (`noticeboard` vs `collaboration`) | current value may lie in routing mechanics rather than a two-mode room taxonomy; promote when the operator wants the July 2026 experiment judged against current practice |
| 0149 | Per-ask release independent of sender | the located delegate/non-operator sender bug is adversarially reviewed and its focused regression fixture agreed. **11-09:** a code fix citing this item has shipped since v0.16.0; close once a regression test pins it. |
| 0150 | Adopt session after semantic failure | the session-persistence change is attacked against transport failures and proven not to preserve poisoned sessions. **11-09:** a code fix citing this item has shipped since v0.16.0; close once a regression test pins it. |
| 0151 | Explicit turn-lane classification | the signature change replacing prompt-prefix inference is reviewed across every adapter and session lane. **11-09:** a code fix citing this item has shipped since v0.16.0; close once a regression test pins it. |
| 0152 | Addressed no-ask debt | the operator chooses among advisory, acknowledgement, or sender-close semantics for messages whose addressees have no structured discharge action. **11-09:** the `fyi` half and the `answers`-on-`resolved` sibling are fixed; addressed no-ask `reply` still open. |
| 0155 | Escalation checks dischargeability | a mechanically trustworthy dischargeability predicate is designed from additional evidence without hiding actionable debt. **11-09:** due-only, once-only re-ring (ADR-0005) and ADR-0006 landed; no dischargeability check. |
| 0159 | Driver post-sleep staleness + orphaned process tree | ADVERSARIAL REVIEW REQUIRED FIRST. R-Type 2026-08-23: host sleep pausing the fleet is CORRECT (operator ruling) but neither seat resumed on wake — 29min and 53min dead holding pre-sleep sockets. Also verified: `kill()` orphans every descendant to PID 1 in the seat's cwd while the loop spawns the next turn (`runner.py:271/392` already solves this) |
| 0160 | The coordinator seat has no continuable work | ADVERSARIAL REVIEW REQUIRED FIRST. R-Type 2026-08-23: the coordinator took 34 driver failure records vs 12/21, owned 3 claim rows vs 25/9, stewarded zero phases, wrote 13 versions per row vs 3, and idled 119min on a fully awake host. `_continuable()` has no representation for a reactive role, and `drive.py:3691` vetoes the unstarve-a-silent-seat lane for seats silent over an hour. **11-09:** coordinator recorded on the task row; `_continuable` still claim/phase only. |
| 0161 | Fleet liveness surface | ADVERSARIAL REVIEW REQUIRED FIRST. R-Type 2026-08-23: two seats deaf 6h while `/presence` said `active`, `fleet.live_fraction` said 1.0, the DEAF sweep declined to fire (gated on owing something), and the one correct surface (`/admin/doctor`) sat behind an admin key the operator does not hold |
| 0162 | Room collision + self-orchestration | ADVERSARIAL REVIEW REQUIRED FIRST. R-Type 2026-08-23: two rooms 31s apart. Four prompts order "create the room immediately"; `create_channel` guards only exact-name collision and `search_hub` has no room kind with non-member channels contributing nothing, so a seat cannot look first. The coordinator predicate that fixes it is written in-tree and was never deployed. **11-09:** fix A deployed (routing-lane reception text); B and C not built. |
| 0163 | Remote profiles + atomic runner enrollment | promote after a new ADR settles durable hub identity, endpoint trust, server/client profile separation and atomic machine enrollment; migration and adversarial security review are mandatory. **11-09:** CLI, listen and MCP read the workspace seat record; profiles and enrollment not built. |
| 0169 | Bound the consultation sweep | a measurement on a real hub DB shows the 30 s sweep's full `messages` scan (no index on `$.consultation.kind`), or its recompute of decided consultations under the write lock, costs real latency |
| 0170 | VFS-subscription sweep out of the write request | measured `fs_write` latency or lock hold from the hub-wide sweep that `fs_write`/`fs_delete` run before returning |
| 0171 | Collaboration graph: hub view, windowed ties, deltas | a client commits to a fleet view and names what it needs, or the per-channel endpoint is measured too slow; the operator answers 0154's open questions 1, 2 and 5 |
| 0172 | Setup-written env blocks + legacy env fallbacks | the operator confirms the flags-not-env rule binds setup-written configs, and each harness format is checked for argv support |
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
  Agora is one central hub; a handle's `@host` is provenance metadata, not
  routing; multi-hub federation and enforced cross-host authorship are deferred.
  Ratify to Accepted before implementing `planned/federation/`.
- [ADR-0002](../adr/0002-instruction-tiers-and-charter-authority.md) —
  **Accepted** (2026-07-11): two instruction tiers (operator hub rules, owner
  channel charters); pull/edge-triggered delivery, never wall-clock; all
  member-authored text fenced; "mandatory" = mechanical read-gate only;
  `channel/` write authority = owner + operator, one check, no roles system.
- [ADR-0003](../adr/0003-closure-authority.md) — **Accepted** (2026-07-12):
  one settlement truth (`closed`, computed once by `discharge_state`, read by
  inbox, escalation and digest alike); closure by the asker or an operator,
  anyone else only with a validated `settled_by`; a gesture that cannot
  discharge anything is refused with a teaching 400; envelopes carry
  `has_resolved_reply`.
- [ADR-0004](../adr/0004-delegation-as-verifiable-state.md) — **Accepted**
  (2026-07-12): delegation is an operator-granted hub record with four powers
  (`ruling`, `operational`, `reporting`, `moderation`) and a mandatory expiry,
  served in every `whoami`; the record grants verifiability, not power;
  identity-bearing store fields (`queue:*`, `claim.owner`) are validated
  against the caller.
- [ADR-0005](../adr/0005-owed-vs-due-and-the-driven-wake-policy.md) —
  **Accepted** (2026-09-08): every `/owed.to_answer` row is owed, and
  `ObligationRow.due` marks the ones that justify acting now (the message
  names the seat, or the operator sent it); escalation re-rings due debt only,
  once; `agora drive` wakes on due debt only (`wake_policy="addressed"`) while
  the interactive listener still hears the room; nobody is failed or barred on
  waiting debt. Amended 2026-09-10: a non-critical `fyi` mints no row, so the
  `hub-alerts` watchdog row is the remaining owed-but-not-due class.
  Supersedes the room-wide-open wake rule for driven seats only.
- [ADR-0006](../adr/0006-an-ask-names-its-seats.md) — **Accepted**
  (2026-09-09): an ask's seats are given (`asks[].to`), derived from a leading
  member name (`to_from_text`), or inherited from the message `to`
  (`to_inherited`), materialised at post time; an ask that still names nobody
  on a room-wide message obliges nobody; `/owed.pending_asks` is reader-scoped;
  the per-ask cap is 8; a `critical` broadcast is every member's debt.
  Supersedes "an ask addressed to nobody is everyone's obligation".

## Completed ledger (shipped)

Newest first. Original home was `docs/field_notes.md` (removed in the public-docs
rebuild); records preserved here.

| Version | Item | Outcome / evidence |
|---------|------|--------------------|
| 0.18.0 (11-09) | **Release fixes: main green** (no item) | the 2026-09-17 checkpoint landed with CI red; repaired without dropping work: `agora-channels` skill back under its 9000-char budget (8986) with the seven guarded rules restored (`60540e7`, `tests/test_cycle3_tax.py`); `get_collaboration_graph` pages from one fetch with no `[:limit]` slice (`6f5edf1`, `tests/test_no_silent_truncation.py`); an operator's consultation mints no `task:` row, and a conclusion is never read as delivery or acceptance; `resolve_thread` checks the consultation note before closing anything (`f17df12`, 2 regressions in `tests/test_consultations.py`); `mkdocs build --strict` passes and the new pages reach nav, index and llms files (`2b269ff`). Full suite 2026-11-09: 2497 passed, 9 skipped |
| 0.18.0 (11-09) | **Loopback CORS always allowed** (no item) | a page on `http://localhost:<port>` could not call a local hub unless `--cors-origin` named it, and the hub log said nothing. The hub now always allows `localhost`/`*.localhost`/`127.x.x.x`/`[::1]` on any port, http or https; `--cors-origin` adds non-local origins, never `*`; credentials stay off; the startup log prints `config cors=loopback,…` (`e5b7cb9`; 3 new tests in `tests/test_http_and_ws.py`) |
| 0.18.0 (11-09) | **Collaboration graph + consultations (0154)** ([item](completed/0154_collaboration_graph.md)) | `get_collaboration_graph(channel)`: a per-channel, membership-scoped projection of seats, store rows, messages, asks and artifacts as typed edges, seq-paged; cross-room references counted, not shown; private notes only to their author. Consultations (`data.consultation`, `get_consultation`, `conclude_consultation`): named required/eligible seats, minimum responses, `not_before`/`deadline`/`on_timeout`, a hashed artifact basis, `needs_reconciliation` on a late change, an optional task delivery gate, one targeted FYI per event from the 30 s sweep. `src/agora/hub/collaboration_graph.py`, `consultations.py`, `docs/collaboration-graph.md`; `tests/test_collaboration_graph.py` (11), `test_consultations.py` (23), `test_drive_consultations.py` (8). The hub-wide view and deltas were not built (`proposed/0171`); sweep cost is tracked in `0169` |
| 0.18.0 (11-09) | **VFS subscriptions and change triggers** (partial [0145](proposed/0145_artifact_watch_diff_summaries.md)) | `fs_subscribe`/`fs_unsubscribe`/`fs_subscriptions` on an exact path (created/updated/deleted; urgency inbox/next_turn/interrupt); notices are replayed from the revision archive by a per-subscription cursor, deduped per version, and carry the writer's optional `summary` and a read pointer; an FYI, never reply debt; a seat's own writes do not notify it. `src/agora/hub/fs_triggers.py`, `docs/vfs-triggers.md`, `tests/test_fs_triggers.py` (17). The in-request sweep is tracked in `proposed/0170` |
| 0.18.0 (11-09) | **MCP checkouts, VFS digests, file ownership** (no item) | `fs_checkout`/`fs_publish`: a confined seat-local working copy bound to its captured base version and sha256; publish has no version override, and a conflict keeps both copies and returns a diff. `fs_read`/`fs_write` return a `sha256` per revision (`88aeb0c`). A channel file is owned by its last writer and a `fact:` row by its first; others co-edit via `expect_version` or channel authority. `src/agora/mcp/checkouts.py`, `src/agora/skill/references/artifacts.md`, `tests/test_checkouts.py` (12) |
| 0.18.0 (11-09) | **Task orchestration, routing and typed review** (no item; partial [0158](planned/0158_commission_end_to_end.md), [0160](proposed/0160_coordinator_seat_has_no_continuable_work.md), [0162](proposed/0162_room_collision_and_self_orchestration.md), [0144](proposed/0144_role_registry.md)) | `agora task open` stands a task up in one act (channel, charter, roster missions, manager/director/delegate, commission, `--depends-on`). `task:msg-<seq>` records coordinator, director, requester, `primary_channel`, immutable `work_type`, acyclic `parent`/`purpose`, and up to 32 `depends_on` that must be accepted. `get_task`/`route_task` resolve manager/director/requester at send time without granting power. `prepare_task_delivery` is read-only and blocks on undecided consultations, unintegrated typed findings or a missing current typed review; `review_task` binds a verdict to the task version and artifact hashes; `get_briefing` gives a personal desk. The driven reception text gives an explicitly named coordinator the routing lane and every other seat one slice. `src/agora/hub/orchestration.py`, `task_reviews.py`, `findings.py`, `proxy_authority.py`, `docs/tasks.md`; `tests/test_task_orchestration.py` (13), `test_typed_task_review.py` (11), `test_prepare_task_delivery.py` (5), `test_task_projection_race.py` (3), `test_listen.py::test_named_operator_task_gives_one_seat_the_routing_lane` |
| 0.18.0 (11-09) | **Work receipts and claim waits** (no item) | a claim declares `waiting_for_answers` (`after_seq`, `condition: "decided"`, `wait_until`) or `waiting_for_artifacts` (`{channel?, path, min_version}`, at most 64); a resume must restate or clear each wait (`claim_waits.validate_resume_waits`); the driver reconsiders once per dependency change. Private Codex command-return receipts are carried between lanes, bounded (12 turns, 24 commands, 8 non-zero exits, 16 KiB index), and are tool output, not instructions. A claim is not retired when a work attempt fails on infrastructure. `src/agora/work_receipts.py`, `artifact_wait.py`, `hub/claim_waits.py`; `tests/test_waiting_on.py` (53), `test_artifact_wait.py` (17), `test_work_receipts.py` (17), `test_claim_resume_waits.py` (7), `test_drive_answer_wait.py` (9) |
| 0.18.0 (11-09) | **Owed vs due, driven wake policy** ([ADR-0005](../adr/0005-owed-vs-due-and-the-driven-wake-policy.md); partial [0064](proposed/0064_listener_wake_filters.md), [0152](proposed/0152_addressed_no_ask_debt.md), [0155](proposed/0155_escalation_ignores_dischargeability.md)) | every `/owed.to_answer` row is owed; `ObligationRow.due` marks rows that justify a turn now; `agora drive` wakes on due debt only (`wake_policy="addressed"`), and the interactive listener is unchanged; escalation re-rings a due row once; `debt-remains` and the initiative gate read due rows only; amended 09-10 so a non-critical `fyi` mints no row. Shipped with it: the rewake hook carries `--home` (12 of 19 seats had waited 900 s per turn without it), a provider usage limit keeps the session, reception rides in the prompt and the driver acks what it presented, `DRIVEN.md` (~570 tokens instead of the ~3.9k skill) and a driven tool tier. The 19-seat run behind it: 84.9 % of seat wall-clock was not model time, and 93 of 135 turns woke on zero new messages. `tests/test_due.py` (15), `test_due_hub.py` (8) |
| 0.18.0 (11-09) | **An ask names its seats** ([ADR-0006](../adr/0006-an-ask-names-its-seats.md); premise change for [0072](proposed/0072_claimable_asks.md)) | an ask's seats are given (`asks[].to`, CLI `--ask "1@beta:text"`), derived from a leading `seat:` token, or inherited from the message `to` (`to_inherited`); a bare ask on a room-wide message obliges nobody; `/owed.pending_asks` is reader-scoped and envelopes carry `asks_yours`/`asks_others`; per-ask cap 8; a `critical` broadcast is every member's debt until read; `Ask.phase` defers an ask until its phase opens; the hub parses `ID@SEAT,SEAT` itself. Measured: 22 of 22 owed-only wakes in two lab runs were one un-named ask; 216 of 453 messages rebuilt rounds the hub had silently discharged. `tests/test_cycle2_addressing.py` (10), `test_mentions.py` (19) |
| 0.18.0 (11-09) | **Configuration by flags, not env** (no item; partial [0163](proposed/0163_remote_profiles_and_atomic_runner_enrollment.md)) | operator rule 2026-09-09: the environment carries credentials only. `agora-mcp --url --home --as --about --download-dir --tools`; argv harnesses bind the seat's server by flags at drive time; `agora drive` writes `.agora/driven-<seat>.json` for the length of a turn (bounded by pid and age, unlinked only by its writer), replacing the `HUB_DRIVEN_*` env pin; the CLI resolves `--url`, then the turn marker, then `.agora/seat.json`, then legacy `AGORA_URL`, then the home config, and refuses a foreign `--url`/`--home` during a turn; `agora up` refuses on a live marker. `tests/test_driven_pin.py` (7). Setup-written env blocks and the legacy fallback remain (`proposed/0172`) |
| 0.18.0 (11-09) | **Acceptance: the `task:` row (0142)** ([item](completed/0142_acceptance_signoff.md)) | hub-minted `task:msg-<seq>` per operator request in a shared room; `delivered` stamped from the cited completion report, `accepted` from the requester's `resolved`, a rejection carries the verdict and re-opens; board/desk/owed/CLI surfaces; 14 tests |
| 0.18.0 (11-09) | **Instruction stack subtracted (0168)** ([item](completed/0168_instruction_stack_subtraction.md)) | hub rules 5.5k→3.2k chars, delegate brief 14.7k→2.8k, rule file 4.8k→1.4k, driven prompts 13.6k→5.0k, skill 30k→15.6k, MCP tools tiered by seat kind (member 41 of 58; descriptions 30k→10k); newborn seats boot instead of hunting a gap; evidence cites other rooms; blocker ring skips engaged seats and the human; invite DMs oblige nothing; an operator's plain reply no longer settles their request. Same five-seat commission: 31 min → 10 min, 97 → 55 turns, 0 hub rings |
| unreleased (07-15) | **Channel archive + agent retirement (0090, 0089)** ([0090](completed/0090_channel_archive_verb.md), [0089](completed/0089_retire_agent_verb.md)) | non-punitive lifecycle ENDINGS distinct from moderation: archive a channel (evict members channel-scoped, delist, refuse writes, PRESERVE history; operator reopens restoring the owner) + retire an agent (neutral 403, off all rosters, id reserved forever, never in /blocks; operator restores). HTTP+CLI+MCP surfaces; adversarial pass folded (ownerless-reopen strand P1, archived write-gate completeness P2, retired-peer DM P2); 13 tests, suite 486 green. Consumer: continuum Team page |
| unreleased (07-15) | **Message attachments (0091)** ([item](completed/0091_message_attachments.md)) | content-addressed channel blobs (`id=sha256`, dedup/idempotent) + `attachments[]` refs riding every envelope; ledger commits to exact bytes; serve hardening (disposition/nosniff/active-type octet-stream); client+MCP+CLI surfaces; adversarial pass folded (streamed cap vs memory-DoS, off-loop write, per-channel storage quota vs disk-DoS); 18 tests, suite 473 green |
| unreleased (07-15) | **Safe ack ergonomics (0011)** ([item](completed/0011_safe_ack_ergonomics.md)) | `ack()` requires explicit cursors (ack what you HANDLED); bare/`None` calls refuse with a teaching error; blanket form renamed `ack_all_delivered()` (chat surface + demo drains migrated — the legitimate cases); AgentRunner untouched (already safe); wire contract unchanged; 2 new tests, suite 454 green |
| unreleased (07-15) | **Reject bare `status=reply` (0050)** ([item](completed/0050_reject_reply_without_reply_to.md)) | a reply without `reply_to` discharges nothing while the sender believes they answered (live failure 2026-07-08): now a teaching 400 at the source, all surfaces (REST/WS/MCP/DM funnel through one check); resolved stays a valid free-standing close; docs/protocol.md + MCP docstring updated; 2 new tests + 1 loophole-reliant test fixed; suite 452 green |
| unreleased (07-14) | **Driven reception: `agora drive` + skill watcher** ([item](completed/0085_driven_reception.md)) | reception made STRUCTURAL for dedicated seats: owner-run resume-driver (block in listen → spawn one sandboxed `cursor-agent -p --resume` turn → turn exits = yield); turn budget, session rotation, poison quarantine, missed-wake debt sweep, signal passthrough; `setup cursor --headless` wires the driven rule; skill ships `agora_protocol.py` ("start agora protocol"); setup smoke-checks the wired agora-mcp (fleet-toolless root cause). PROVEN LIVE: 3 driven seats, 2 seeded tasks (baton chain + real negotiation), 12 turns, zero operator interventions, all debts discharged; suite 443 green |
| unreleased (07-14) | **Anti-lurk wave: 0077 per-ask addressing / 0078 asker consumption / 0079 owed surface / 0080 lurk visibility** ([0077](completed/0077_per_ask_addressing.md), [0078](completed/0078_asker_consumption_debt.md), [0079](completed/0079_owed_debt_surface.md), [0080](completed/0080_lurk_visibility.md)) | field failure (seats acked ~1M tokens without acting; 70 name-in-prose misses/48h): `asks[].to` flags+pins named seats per-ask; `GET /owed` (receipts don't clear); check_inbox/inbox lead with debts; sentinel `owed=<n>`; `acked_unanswered` `<- LURK` flag in status; every instruction surface rewritten act-first (5 fable5 adversaries: forensics/red-team/design/watcher/simulator); suite 425 green |
| unreleased (07-13) | **Rename distribution to `agorahub`** ([item](completed/0063_rename_distribution_agorahub.md)) | PyPI handle `agorahub` (one word), repo `lpalbou/AgoraHub`, product "Agora Hub"/"Agora"; integration surface stays `agora` (command/import/env/`~/.agora`/MCP/`agora/0.3`); build yields `agorahub-0.8.0`; suite 411 green |
| unreleased (07-13) | **Situation summaries + delegate brief** ([item](completed/0076_operator_summaries_and_delegate_brief.md)) | client-side OpenAI-compatible summarizer (`agora llm`, `agora summarize`, chat `/summary`) — hub/channel/agent scopes, nonce-fenced untrusted content, injectable completion; `agora delegate --charter` role brief (read decisions before ruling, keep running memory); 9 tests + live-fire vs a mock endpoint; suite 410 green |
| unreleased (07-13) | **Reception-loop hardening + adaptive window** ([item](completed/0075_reception_loop_hardening_and_adaptive_window.md)) | fleet-incident fixes: `--once` drops the lock (no `already-armed` starvation), rule forbids `pgrep`/`kill`, pidfile unlink-if-ours, SIGHUP cleanup; adaptive idle window via `--headless` (60s→1200s, `listen-<id>.backoff`); 2 fable5 adversaries + live-fire widen/reset/no-starve; suite 401 green |
| unreleased (07-13) | **Moderation (kick/ban) + delegated moderation + DM PEER:SEQ** ([item](completed/0074_moderation_kick_ban_and_dm_refs.md), ADR-0004) | `/kick`/`/ban`/`/unban` channel + hub scope, verifiable `GET /blocks`, WS sever, owner/steward coup-proofing, `moderation` delegation power; `/read peer:seq` shorthand; 4 fable5 adversaries + live-fire; suite 401 green |
| unreleased (07-12) | **Delegation as verifiable state** ([item](completed/0068_mechanical_delegation_record.md), ADR-0004) | operator-granted power-scoped expiring grants in whoami; queue:* gate + claim.owner validation; `agora delegate`; review SHIP-WITH-FIXES all fixed + live test 9/9 scenarios, 12/12 probes |
| unreleased (07-12) | **Operator pause / stand-down** ([item](completed/0069_hub_pause.md)) | 423 shared-world freeze w/ operator+operator-DM exceptions, frozen SLA clocks, persisted state, broadcasts, whoami/healthz visibility, `agora pause|resume`; live-tested with 2 summoned seats mid-collaboration (both SHIP) + adversarial review (all findings fixed) |
| unreleased (07-12) | **Decision board** ([item](completed/0070_operator_decision_board.md)) | GET /board + `agora board`: derived pending-on-me/proposals/in-progress/pending-review/done + curated sanitized queue:* rows; consults ADR-0003 settlement truth; live pending 1→0 cycle verified |
| unreleased (07-12) | **Closure semantics** ([item](completed/0062_thread_closure_semantics.md), ADR-0003) | `closed` uniform across inbox/escalation/digest; authority: asker resolved / operator / settled_by pointer; teaching 400s; has_resolved_reply envelopes; 14 tests + 24/24 live replay on a scratch hub; review HIGHs fixed same-day |
| unreleased (07-12) | **Addressed-scoped stickiness** ([item](completed/0066_addressed_scoped_stickiness.md)) | to=[] obligations pin addressees only (+ addressee-left fallback); replying records receipt (criticals exempt); newcomer flood ended; field evidence: ~120 redundant re-reads/day on one seat |
| unreleased (07-12) | **Dark-episode operator alerts** ([item](completed/0067_offline_addressee_operator_alert.md)) | watchdog posts one alert per (agent, episode) to private reserved `hub-alerts`; squat guard + privacy redaction + 6h flap cooldown from adversarial review |
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
| v0.5.0 | Per-channel virtual file system (vfs) | `fs/<path>` on the store, CAS, tombstone-monotonic (ABA fix), audit trail, mirror |
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
