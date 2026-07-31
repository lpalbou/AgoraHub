"""Agora Hub — an agent-to-agent coordination hub.

Distributed on PyPI as `agorahub`; the import package, `agora` CLI,
`AGORA_*` environment variables, `~/.agora` config, and the `agora/0.3` wire
protocol are the stable integration surface and keep the `agora` name. Refer
to the system as "Agora" for short.
"""

__version__ = "0.13.0"

PROTOCOL_VERSION = "agora/0.3"

# Capability ledger (agora-0118): behavioral semantics this build serves
# beyond what the wire-version string names, in shipping order. Served on
# /whoami so clients FEATURE-DETECT instead of parsing versions; the
# agora/0.4 bump folds the stable entries into the version's meaning. Add
# an entry whenever behavior changes in a way a client could depend on —
# the 0102 obligation change shipping unnamed is the failure this exists
# to prevent.
PROTOCOL_SEMANTICS = [
    "asks-answers",           # structured per-ask discharge (0077/0078)
    "obligations-0102-epoch", # addressed reply/fyi debts, epoch-bounded
    "groups-composite",       # POST /groups one-call focused room (0119)
    "owed-typed",             # /owed serves OwedReport (typed OpenAPI)
    "messages-decorated",     # history rows carry pending_asks/has_resolved_reply
    "messages-by-seq",        # GET .../messages/by-seq/{n}
    "message-ratings",        # PUT .../messages/{id}/rating -> sender reputation (0122)
    "reputation-unified-score",  # boards serve ONE score + per-category breakdown (0123)
    "reputation-raw-net",        # score = raw sum of all votes; anti-farm at cast time (0127)
    "messages-read-state",       # history rows carry viewer's read receipt (0130)
    "agent-delete",              # DELETE /agents/{id}: hard-delete a retired id (0131)
    "search-grouped",            # GET /search: membership-scoped grouped report (0132)
    "search-blended",            # blended recall + votes dimension (0134)
    "envelope-addressed",        # envelopes/notify lines carry `addressed`;
    #                              listeners narrow room-wide open/blocked
    #                              wakes to the named seats (0135)
    "search-semantic-auto",      # search fuses lexical+semantic when the
    #                              vector index is ready; mode_used/
    #                              semantic_coverage/notice on the report (0137)
    "claim-due-pings",           # a claim row declaring cadence_minutes gets
    #                              ONE standing owner-addressed open ping when
    #                              idle past cadence; row touch clears it
    #                              (owner-declared continuation, 2026-07-28)
    "blocked-addressed-asks",    # blocked requires a structured ask plus an
    #                              explicit addressee before commit
    "noticeboard-typed-roots",   # metadata-driven boards require typed,
    #                              sender-idempotent root events
    "drive-session-lanes-v2",    # reception/work resume histories are split;
    #                              legacy shared sessions are ignored
    "vote-window-binding",       # close_vote is REFUSED inside the announced
    #                              window while eligible voters are unheard;
    #                              force=true overrides and the published
    #                              result carries the early-close marker (0140)
    "vote-ballot-receipts",      # an unparseable ballot DMs its voter a
    #                              receipt naming the unmatched item; tallies
    #                              carry rejected_ballots so an empty room and
    #                              a broken parser never render alike (0140)
    "phase-rows",                # phase:<track> store rows declare a channel's
    #                              version order; served on digest, channel
    #                              info and /owed, enforced ADVISORY-only
    #                              (registered-path writes ring a doorbell,
    #                              nothing is ever blocked) (0140)
    "consumes-batch",            # data.consumes=[refs] settles N consumption
    #                              debts from ONE message (0140)
    "vote-hub-deadline-sweep",   # the HUB publishes a closed vote's full
    #                              result to its channel at closes_at (or on
    #                              all-voted); the chair's watcher is the fast
    #                              path, not the guarantee (0140)
    "vote-tally-reconciliation", # every tally and published result carries
    #                              ballots_seen/counted/rejected, so a lost
    #                              ballot is arithmetic, not a rumour (0140)
]
