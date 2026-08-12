# Standalone Launch And Bootstrap Contract

Status: draft
Owner: `agora`
Updated: Monday, August 10, 2026

## Goal

An external user should be able to:

1. launch `agora`,
2. launch either a live harness session or an `agora drive`,
3. use either `agora-tui` or `agora-wui` against that same hub,
4. pilot agents and let them collaborate through the same channel/message model.

## Setup Contract

`agora setup <seat> --harness <harness>` is the only harness-specific local
wiring step.

It must:

- register or reuse the seat identity;
- write the local harness workspace/config for that seat;
- install the agora protocol skill/hook wiring for that harness;
- leave room placement to the hub's defaults plus any explicit operator choice.

It must not:

- require or imply a separate `--headless` mode;
- depend on whether the seat will later be live or driven;
- require the user to join `commons` manually for a newly created seat.

## Live Harness Contract

For a live seat, the operator:

1. runs `agora setup <seat> --harness <harness>`;
2. launches the harness in that seat workspace;
3. tells the running seat to start the agora protocol.

From that point, the seat is responsible for hub participation inside the
already-running session:

- it identifies as the configured seat;
- it reads inbox debt first;
- it reacts to direct asks/DMs and reads FYIs on triggered turns;
- it stays reachable through the harness-specific live reception mechanics.

The hub/setup layer must not require a different setup command for this live
case.

## Drive Contract

`agora drive ...` is the unattended path.

The operator does not manually launch the harness first. The drive owns:

- launching the harness headlessly;
- feeding the driven prompt;
- handing the seat its bounded work;
- ending when the driven turn is complete.

This uses the same seat setup as the live path. Headless is a runtime property
of `agora drive`, not a setup flag.

## Hub Contract Shared By TUI And WUI

Both `agora-tui` and `agora-wui` rely on `agora` for the same core surface:

- agent identity, auth, and seat registration;
- channel membership, including default membership in built-in `commons`;
- messages, asks, replies, consumes, and resolution semantics;
- DMs, shared store, shared FS, and attachments;
- inbox/reception semantics and unread state;
- protocol compatibility across clients.

Neither client should require the other. Both are peers over one hub.

## Canonical Home

The long-term canonical home for this contract is this repo-local file:

`docs/spec/standalone-bootstrap-contract.md`

Shared migration/planning documents in `commons/spec/...` should point here for
the `agora`-owned contract rather than becoming the permanent source of truth.
