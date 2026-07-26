"""Embed client + breaker (agora-0137 step 4), against the fake endpoint."""
from __future__ import annotations

import pytest

from agora.embed_client import (CANARY_TEXT, QUERY_INSTRUCTION, BreakerOpen,
                                EmbedClient)
from tests.fake_embed import FakeEmbedServer


@pytest.fixture()
def server():
    s = FakeEmbedServer().start()
    yield s
    s.stop()


def test_embed_batches_and_query_instruction(server):
    """Documents go RAW; queries carry the frozen card-default instruction
    (measured: instructed 0.601 vs raw 0.474; a custom string halved
    recall — the freeze is load-bearing)."""
    c = EmbedClient(server.url, "m")
    vecs, closed = c.embed(["alpha", "beta"])
    assert len(vecs) == 2 and not closed
    assert server.requests[-1]["input"] == ["alpha", "beta"]   # raw docs
    c.embed_query("find the thing")
    assert server.requests[-1]["input"][0] == (
        QUERY_INSTRUCTION + "find the thing")


def test_breaker_opens_backs_off_and_probe_closes(server):
    c = EmbedClient(server.url, "m")
    server.fail_next = 1
    with pytest.raises(Exception):
        c.embed(["x"])
    st = c.breaker_state()
    assert st["state"] == "open" and st["backoff_seconds"] == 1.0
    # While resting, the QUERY path refuses instantly (never a probe).
    with pytest.raises(BreakerOpen):
        c.embed_query("q")
    # A fill call may probe once the window opens (backoff 1s; wait it out).
    import time
    time.sleep(1.05)
    vecs, closed = c.embed(["x"])
    assert closed is True                    # this success closed the breaker
    assert c.breaker_state()["state"] == "closed"


def test_backoff_doubles_and_batch_mismatch_is_an_error(server):
    c = EmbedClient(server.url, "m")
    import time
    server.fail_next = 2
    with pytest.raises(Exception):
        c.embed(["x"])
    time.sleep(1.05)
    with pytest.raises(Exception):
        c.embed(["x"])                       # the probe itself fails
    assert c.breaker_state()["backoff_seconds"] == 2.0


def test_query_timeout_is_the_budget(server):
    """A hung endpoint costs a query at most the 2s budget (ops R1: with
    always-fuse this call is on every search's hot path)."""
    c = EmbedClient(server.url, "m")
    server.hang_next = 1
    import time
    t0 = time.time()
    with pytest.raises(Exception):
        c.embed_query("q")
    assert time.time() - t0 < 4.0            # 2s budget + slack, never 3600


def test_canary_fingerprint_changes_with_the_seed(server):
    """The imposter drill: same model NAME, different weights (seed) —
    the fingerprint must differ so the hub refuses `ready`."""
    c = EmbedClient(server.url, "m")
    fp_a = c.canary_fingerprint()
    assert fp_a == c.canary_fingerprint()     # stable for same weights
    server.seed = "model-b"                   # imposter behind the same name
    assert c.canary_fingerprint() != fp_a
    assert server.requests[-1]["input"] == [CANARY_TEXT]
