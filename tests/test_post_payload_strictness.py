"""An unknown field on a post is REFUSED, never silently dropped.

The bug these pin, measured on this hub 2026-08-25: `PostMessage` carried
pydantic's default `extra="ignore"`, so `POST /channels/{c}/messages` with a
keyword the hub does not have returned 200 and dropped it. @delegate sent 16
`settles=[...]` refs at `agora-and-wui#435`, was accepted, and cleared
nothing — there is no `settles` field. A 200 over a message that discharged
nothing is the worst available answer: it teaches its first real user that
the verb works.

Each test below goes RED if the strictness is removed — verified by deleting
`model_config`/`_refuse_unknown_fields` and re-running, not by assuming.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agora.models import PostMessage


def _refusal(**kwargs: object) -> str:
    with pytest.raises(ValidationError) as excinfo:
        PostMessage(**kwargs)  # type: ignore[arg-type]
    return str(excinfo.value)


def test_an_invented_verb_is_refused_by_name() -> None:
    """The exact payload that cost @delegate 16 refs and a false receipt."""
    detail = _refusal(body="x", settles=["commons#1", "commons#2"])
    assert "`settles` is not a field" in detail
    assert "NOTHING WAS POSTED" in detail


def test_a_near_miss_names_the_field_it_meant() -> None:
    """`answer=` for `answers=` is the dangerous member of the class: the
    caller believes they discharged an ask."""
    assert "did you mean `answers`?" in _refusal(body="x", answer=["1"])
    assert "did you mean `declines`?" in _refusal(body="x", decline=["1"])
    assert "did you mean `consumes`?" in _refusal(body="x", consume=["a"])


def test_a_field_that_lives_in_data_is_sent_to_data_not_just_rejected() -> None:
    """`evidence` at the top level is what an HTTP caller naturally writes,
    because the MCP tool takes it as a parameter. Dropped silently it makes a
    `resolved` on an operator request settle nothing while reading delivered.
    """
    detail = _refusal(body="x", evidence=[{"kind": "store", "ref": "k@1"}])
    assert "lives INSIDE `data`" in detail
    assert 'data={"evidence": ...}' in detail


def test_an_unrecognisable_key_is_still_refused_and_lists_the_real_fields() -> None:
    detail = _refusal(body="x", qqqq=1)
    assert "`qqqq` is not a field" in detail
    # No invented suggestion: a wrong hint is worse than none, the caller
    # will type it.
    assert "did you mean" not in detail
    for field in ("answers", "consumes", "data", "reply_to"):
        assert field in detail


def test_every_real_field_still_posts() -> None:
    """The guard must not cost the happy path — including `data`, which is
    where the folded fields legitimately live."""
    payload = PostMessage(
        body="b", title="t", status="reply", urgency="next_turn",
        to=["seat"], reply_to="01ABC", answers=["1"], declines=["2"],
        consumes=["commons#3"], data={"evidence": [{"kind": "store",
                                                    "ref": "k@1"}]},
    )
    assert payload.answers == ["1"]
    assert payload.consumes == ["commons#3"]
    assert payload.data == {"evidence": [{"kind": "store", "ref": "k@1"}]}
