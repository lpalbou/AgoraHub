"""The protocol handshake must be load-bearing: a client built for one
`agora/X.Y` warns (once) when the hub advertises another, and stays silent on
a match. See docs/protocol.md, "Versioning: one string".

The rule is ONE function (`agora.protocol_warning`) and every comparison site
calls it — there is no capability list to diff beside it, so this file is the
only place a compatibility decision is made.
"""

from __future__ import annotations

import asyncio
import warnings

import pytest

from agora import PROTOCOL_VERSION, SUPPORTED_PROTOCOLS, protocol_warning
from agora.client import AgoraClient


@pytest.fixture()
def client():
    c = AgoraClient("http://hub.example:8765", "key")
    yield c
    asyncio.run(c.close())  # release the underlying httpx client


def test_mismatch_warns_once_and_records_hub_protocol(client):
    with pytest.warns(RuntimeWarning, match="hub speaks agora/9.9"):
        client._check_protocol("agora/9.9")
    assert client.hub_protocol == "agora/9.9"

    # Second sighting is silent: one warning per client, not one per call.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        client._check_protocol("agora/9.9")


def test_match_and_missing_are_silent(client):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        client._check_protocol(PROTOCOL_VERSION)   # same protocol: silence
        client._check_protocol(None)               # pre-0.9 hub omits it: silence
    assert client.hub_protocol is None             # nothing advertised sticks


def test_the_rule_is_one_function_naming_both_versions():
    """One sentence, both versions in it: a warning that names only the hub
    leaves the reader guessing which side to upgrade."""
    note = protocol_warning("agora/9.9")
    assert note and "agora/9.9" in note and PROTOCOL_VERSION in note
    # Nothing to compare beyond the string: the version IS the capability
    # statement (agora/0.4 deleted the stamp ledger).
    assert protocol_warning(PROTOCOL_VERSION) is None
    assert all(protocol_warning(v) is None for v in SUPPORTED_PROTOCOLS)
    # A hub that advertises nothing is not a mismatch — silence must never
    # manufacture a warning.
    assert protocol_warning(None) is None and protocol_warning("") is None


def test_the_package_serves_no_capability_ledger():
    """The 0.4 unification DELETED PROTOCOL_SEMANTICS. Re-adding a list of
    capability strings re-adds the thing clients diffed, which is how a fold
    made a hub look like it LOST the features it had just gained."""
    import agora

    assert not hasattr(agora, "PROTOCOL_SEMANTICS")
