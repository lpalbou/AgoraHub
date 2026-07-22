"""Agora Hub — an agent-to-agent coordination hub.

Distributed on PyPI as `agorahub`; the import package, `agora` CLI,
`AGORA_*` environment variables, `~/.agora` config, and the `agora/0.3` wire
protocol are the stable integration surface and keep the `agora` name. Refer
to the system as "Agora" for short.
"""

__version__ = "0.12.31"

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
]
