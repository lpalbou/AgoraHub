"""Agora Hub — an agent-to-agent coordination hub.

Distributed on PyPI as `agorahub`; the import package, `agora` CLI,
`AGORA_*` environment variables, `~/.agora` config, and the `agora/0.4` wire
protocol are the stable integration surface and keep the `agora` name. Refer
to the system as "Agora" for short.
"""

__version__ = "0.17.4"

#: The ONE protocol identifier. The string does not name a subset of the
#: hub's behavior — it NAMES THE WHOLE CONTRACT this build serves: every
#: route, field, and obligation rule in docs/protocol.md. There is no second
#: capability ledger to diff (the stamp list `/whoami` served through 0.13
#: was deleted at 0.4): additive changes ship inside a version and an
#: older client simply does not call the new tools, so the only thing a
#: client ever needs to ask is "do I speak this version?".
PROTOCOL_VERSION = "agora/0.4"

#: Every protocol version this build can talk to. Usually one; a tuple only
#: while a bump is being rolled out and this build genuinely handles both
#: wire shapes. Membership is the whole compatibility test.
SUPPORTED_PROTOCOLS = (PROTOCOL_VERSION,)


def protocol_warning(hub_protocol: str | None) -> str | None:
    """The compatibility rule, written once: `None` when the hub speaks a
    version this build speaks, else ONE sentence naming BOTH versions.

    A warning, never a refusal — a refusal turns a cosmetic skew into an
    outage, and the operator upgrades hub and seats with the same install.
    A hub that advertises nothing is not a mismatch (an unauthenticated
    probe, or a surface that omits the field); silence there must not
    manufacture a warning.
    """
    if not hub_protocol or hub_protocol in SUPPORTED_PROTOCOLS:
        return None
    return (f"hub speaks {hub_protocol}, this client speaks "
            f"{PROTOCOL_VERSION} — upgrade both sides to the same agorahub "
            "release (wire shapes may differ)")


def is_agora_protocol(value: object) -> bool:
    """IDENTITY, not compatibility: does this `protocol` string come from an
    agora hub at all? Used by the port preflight to tell a hub from a
    squatter — it must stay a prefix test, because taking a port over from
    (or refusing to kill) a hub of ANY version is a different question from
    whether we can talk to it. Compatibility is `protocol_warning`."""
    return isinstance(value, str) and value.startswith("agora/")
