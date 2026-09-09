"""OWED vs DUE — the hub says which debt justifies a turn NOW.

Every /owed row is owed: on the ledger, in the next check_inbox. Only a DUE
row may spawn the turn that reads it. Operator criteria (b)/(d), 2026-09-08;
definitions and the independent review that corrected the first cut are in
untracked/definitions-owed-vs-due-2026-09-08.md and
untracked/review-owed-vs-due-2026-09-08.md.

Each test here goes RED when the feature is removed: delete `due` from the
row and the signature test fails on a non-empty signature; restore the
`demand` exemption and the policy test fails on rc 4.
"""

from __future__ import annotations

import pytest

from agora.drive import Driver, ReceptionDebt
from agora.listen import (_DRIVER_BROADCAST_WAKE, _DRIVER_UNOWNED_WAKE,
                          _deliver_wake, _owed_snapshot)
from agora.models import ObligationRow


# --- the model -----------------------------------------------------------------

def test_row_is_due_by_default_and_the_field_is_called_due():
    row = ObligationRow(channel="c", id="01X", seq=1, sender="a")
    assert row.due is True
    assert not hasattr(row, "wakes"), "driver vocabulary must not leak into the hub row"


# --- the signature: only due (or escalated) debt rings -------------------------

def _snapshot(monkeypatch, rows, consume=()):
    payload = {"counts": {"to_answer": len(rows), "to_consume": len(consume)},
               "to_answer": rows, "to_consume": list(consume)}

    class R:
        status_code = 200
        def json(self): return payload

    monkeypatch.setattr("agora.config.get_cached_key", lambda hub, aid: "agora_" + "k" * 40)
    monkeypatch.setattr("httpx.get", lambda *a, **k: R())
    return _owed_snapshot("http://h:1", "me")


def test_owed_but_not_due_rows_never_enter_the_signature(monkeypatch):
    counts, sig, _ = _snapshot(monkeypatch, [
        {"id": "01FYI", "due": False},                 # an operator fyi
        {"id": "01WATCH", "due": False},               # a watchdog alert
    ])
    assert counts == (2, 0), "still OWED: the counts say so"
    assert sig is None, "nothing due -> no signature -> no arm-time wake"


def test_due_rows_ring_and_older_hubs_default_to_due(monkeypatch):
    _, sig, _ = _snapshot(monkeypatch, [{"id": "01ASK", "due": True},
                                       {"id": "01OLD"}])           # no field: pre-0.18 hub
    assert sig is not None and "01ASK" in sig and "01OLD" in sig


def test_a_waiting_row_never_rings_even_when_escalated(monkeypatch):
    """ADR-0005 §1: "can wait" does not become "must act" because time
    passed. A rotting fyi still escalates on /owed (the operator sees it);
    it does not buy a turn. Due debt keeps 0106's re-ring via the age band."""
    _, sig, _ = _snapshot(monkeypatch, [{"id": "01ROT", "due": False,
                                        "escalated": True, "created_at": 1.0}])
    assert sig is None
    _, sig, _ = _snapshot(monkeypatch, [{"id": "01DUE", "due": True,
                                        "escalated": True, "created_at": 1.0}])
    assert sig is not None and sig.startswith("01DUE!")


# --- the driver's wake policy --------------------------------------------------

def _wake(monkeypatch, events, *, rows, policy):
    def snap(hub, aid):
        n = len(rows)
        due = [r for r in rows if r.get("due", True)]
        return (n, 0), (",".join(r["id"] for r in due) or None), {"to_answer": rows}
    monkeypatch.setattr("agora.listen._owed_snapshot", snap)
    monkeypatch.setattr("agora.listen._record_owed_signature", lambda *a, **k: None)
    monkeypatch.setattr("agora.listen._emit", lambda *a, **k: None)
    monkeypatch.setattr("agora.listen._emit_stderr", lambda *a, **k: None)
    monkeypatch.setattr("agora.listen._emit_request_preview", lambda *a, **k: None)
    return _deliver_wake(events, "me", preview=False, once=True, hub="http://h:1",
                         classify_driver_wake=True, wake_policy=policy)


def _ev(flags, status="open", sender="peer"):
    return {"id": "m", "sender": sender, "status": status, "flags": flags,
            "channel": "c", "seq": 1, "title": "t"}


def test_addressed_policy_lets_a_room_wide_open_wait(monkeypatch):
    """A peer's open that names nobody: mail, not a task. Today's 'room'
    policy buys a broadcast turn for it; 'addressed' does not."""
    ev = _ev("unassigned,open")
    assert _wake(monkeypatch, [ev], rows=[], policy="room") == _DRIVER_BROADCAST_WAKE
    assert _wake(monkeypatch, [ev], rows=[], policy="addressed") == _DRIVER_UNOWNED_WAKE


def test_addressed_policy_still_wakes_on_a_line_that_names_me(monkeypatch):
    assert _wake(monkeypatch, [_ev("addressed,to-me,open")], rows=[], policy="addressed") == 2


def test_addressed_policy_still_wakes_on_the_operator(monkeypatch):
    assert _wake(monkeypatch, [_ev("from-operator,unassigned,open", sender="laurent")],
                 rows=[], policy="addressed") == _DRIVER_BROADCAST_WAKE


def test_addressed_policy_never_lets_a_room_wide_peer_open_buy_a_turn(monkeypatch):
    """Whatever debt I hold: a peer's open that names nobody is mail. My own
    due debt rings through the owed signature at arm (backlog poll), never by
    riding along on someone else's line (cycle 2: lead and gamma woke on an
    open addressed to beta because they owed something unrelated)."""
    fyi_only = [{"id": "01FYI", "due": False}]
    assert _wake(monkeypatch, [_ev("unassigned,open")], rows=fyi_only,
                 policy="addressed") == _DRIVER_UNOWNED_WAKE
    due = [{"id": "01ASK", "due": True}]
    assert _wake(monkeypatch, [_ev("unassigned,open")], rows=due,
                 policy="addressed") == _DRIVER_UNOWNED_WAKE


# --- the driver's gates ------------------------------------------------------------

def test_reception_debt_separates_due_from_owed(monkeypatch):
    monkeypatch.setattr("agora.drive._owed_snapshot", lambda hub, aid: (
        (2, 0), "x", {"to_answer": [
            {"id": "01ASK", "channel": "c", "seq": 1, "due": True, "pending_asks": []},
            {"id": "01FYI", "channel": "c", "seq": 2, "due": False, "pending_asks": []},
        ]}))
    d = Driver("me", "http://h:1", harness="codex")
    debt = d._reception_debt()
    assert debt.to_answer == {"01ASK", "01FYI"}, "both are OWED"
    assert debt.to_answer_due == {"01ASK"}, "only one is DUE"


def test_initiative_lane_is_gated_on_due_debt_only():
    waiting = ReceptionDebt(to_answer=frozenset({"01FYI"}), to_answer_due=frozenset())
    due = ReceptionDebt(to_answer=frozenset({"01ASK"}), to_answer_due=frozenset({"01ASK"}))
    assert not waiting.due, "an fyi must not bar the seat from its own work"
    assert due.due


def test_absent_narrowing_means_everything_is_due():
    """A caller that never heard of `due` (every pre-existing constructor)
    must keep today's semantics: all owed debt counts. Defaulting to EMPTY
    silently excused every commission from the anti-lurk bound."""
    legacy = ReceptionDebt(to_answer=frozenset({"commission"}))
    assert legacy.due == {"commission"}


def test_debt_remains_is_scored_on_due_rows_only(monkeypatch):
    """A turn that left an fyi unread is not a failed turn."""
    from agora.drive import TurnEvidence
    d = Driver("me", "http://h:1", harness="codex")
    before = ReceptionDebt(to_answer=frozenset({"01FYI"}), to_answer_due=frozenset())
    d._reception_debt_before = before
    d._reception_debt_verification_required = True
    monkeypatch.setattr(Driver, "_reception_debt", lambda self: before)
    ev = TurnEvidence(ok=True, stage=None, reason=None, detail="", tools=("check_inbox",))
    out = d._verify_reception_debt(ev, "wake")
    assert out.ok, out


# --- the CLI can finally address an ask -------------------------------------------

def test_cli_ask_syntax_carries_per_ask_to():
    from agora import cli
    import inspect
    src = inspect.getsource(cli)
    assert 'ID[@SEATS]:TEXT' in src, "help must advertise the per-ask `to` form"


def test_addressed_policy_ignores_another_seats_escalation_and_my_unrelated_debt(monkeypatch):
    """The hub re-emits an escalating line to every member; `escalated` alone is
    not mine. And my own unrelated due debt never turns someone else's message
    into my wake (cycle 2: lead and gamma woke on an open addressed to beta)."""
    others_escalated = _ev("addressed,open,escalated")
    assert _wake(monkeypatch, [others_escalated], rows=[{"id": "01MINE", "due": True}],
                 policy="addressed") == _DRIVER_UNOWNED_WAKE
    others_open = _ev("addressed,open,from-operator", sender="laurent")      # operator, but addressed to someone else
    assert _wake(monkeypatch, [others_open], rows=[{"id": "01MINE", "due": True}],
                 policy="addressed") == _DRIVER_UNOWNED_WAKE
    mine_escalated = _ev("addressed,to-me,open,escalated")
    assert _wake(monkeypatch, [mine_escalated], rows=[], policy="addressed") == 2
    assert _wake(monkeypatch, [_ev("critical,from-operator", status="fyi", sender="laurent")],
                 rows=[], policy="addressed") == 2


def test_wake_line_carries_every_seq_in_the_batch():
    """The sentinel's per-channel maximum stays (its contract); a `seqs=` field
    names every message so a wake can be attributed (reviewer Round 2, Q3a)."""
    from agora.listen import wake_line
    events = [{"channel": "work", "seq": 24, "sender": "lead", "flags": "to-me", "status": "open"},
              {"channel": "work", "seq": 23, "sender": "alpha", "flags": "", "status": "reply"}]
    line = wake_line(events, "beta")
    assert "channels=work#24" in line and "seqs=work#23,work#24" in line
    assert "seqs=" not in wake_line(events[:1], "beta"), "one event: the channel field already names it"
