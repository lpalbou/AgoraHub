"""A NEWBORN seat must get one pass at its own mission.

THE DEFECT THIS PINS (laurent, `dm:agora--laurent#71`, 2026-08-23). The first
seat ever spawned through `agora spawn` was given the mission *"you are an
observer, say hi when you arrive"*. It joined, its mission was mirrored into
`CLAUDE.md`, its driver reported `event=ready status=ok` — and it said nothing.

`_initiative_step` is the only lane that authorises a seat to speak first, and
it opened with:

    if not self._turn_times or max(self._turn_times) <= self._last_initiative:
        return False

`_turn_times` records turns this PROCESS has taken. A seat that has just been
born has taken none, so `not self._turn_times` was true and the lane refused —
every pass, forever, until some other seat happened to message it. The mission
an operator wrote was mirrored to disk and then never read by anybody.

The bound is right and stays: *"a dead room buys zero passes"*, so a seat may
not manufacture work out of silence. **A newborn seat is not a dead room.** Its
own creation is the event — an operator deliberately made it, with a mission,
seconds ago. That is strictly more warrant than the traffic the gate was
looking for.

So the lane now admits exactly one pass on a seat that has never taken a turn.
It is bounded by the process: `_last_initiative` is stamped by that pass, and
every later pass needs real traffic again. Every other gate is untouched — a
seat that owes anything, holds continuable work, is a delegate, is out of
budget, or cannot read the hub still does not get the lane.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.drive import Driver, ReceptionDebt

NOTHING_OWED = ReceptionDebt(to_answer=frozenset(), refs=())


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    return tmp_path


def _seat(home: Path, **kw) -> Driver:
    d = Driver("scribe", "http://127.0.0.1:1", cwd=home, **kw)
    d._scan_ok = True                      # the store walk completed
    return d


def _lane(d: Driver, monkeypatch, debt=NOTHING_OWED) -> list[str]:
    """Run one lane pass, capturing the prompt it would spend a chunk on."""
    seen: list[str] = []
    monkeypatch.setattr(d, "_reception_debt", lambda: debt)
    monkeypatch.setattr(
        d, "run_work_turn",
        lambda *, prompt_override=None: (seen.append(prompt_override or ""),
                                         True)[1])
    # A newborn's one pass is a BOOT RECEPTION turn (whoami, charter, inbox),
    # not a lane chunk: measured live, the lane pass on an empty room
    # manufactured an ask and two premature builds. Captured as "BOOT".
    monkeypatch.setattr(d, "run_turn",
                        lambda **kw: (seen.append("BOOT"), True)[1])
    d._initiative_step()
    return seen


def test_a_seat_that_has_never_taken_a_turn_gets_its_one_pass(home, monkeypatch):
    """The defect, from the seat's own side: no turn history, nothing owed,
    no continuable row — exactly a freshly spawned seat, one second old."""
    d = _seat(home)
    assert d._turn_times == [], "premise: a newborn has no turn history"

    assert _lane(d, monkeypatch) == ["BOOT"], (
        "a newborn seat gets exactly one boot reception pass — the turn that "
        "reads its mission (scribe saying hi) — and never a lane chunk")


def test_the_pass_is_once_per_process_not_once_per_loop(home, monkeypatch):
    """The bound that keeps this from becoming the ceremony lane: the birth
    warrant is spent by the pass that uses it. A second pass needs real
    traffic like any other, and a newborn still has none.

    THE COOLDOWN IS NEUTRALISED ON PURPOSE. Written first without this, the
    test passed against a build with the bound deleted — the 900s cooldown was
    refusing the second pass and the assertion was reading that instead. A
    false green of exactly the shape this room spent the night cataloguing:
    green, and not measuring its own subject. With the cooldown at zero the
    only thing that can refuse the second pass is the newborn branch.
    """
    monkeypatch.setattr("agora.drive.DRIVE_INITIATIVE_COOLDOWN", 0.0)
    d = _seat(home)
    assert _lane(d, monkeypatch) == ["BOOT"], "first pass is the boot"
    assert d._last_initiative > 0, "the pass must stamp the clock"

    assert not _lane(d, monkeypatch), (
        "the newborn warrant fired twice — with no turn in between, the "
        "second pass has exactly the dead-room shape the gate exists for")


def test_an_established_idle_seat_is_still_refused_without_traffic(
        home, monkeypatch):
    """The negative twin, and the reason the original gate was written. A seat
    that HAS taken turns, none since its last lane pass, is a dead room: it
    must not buy a pass. Deleting the newborn branch must not be the only way
    to keep this green — this is what says the fix is narrow.

    ON THE BOUNDARY, deliberately: the last turn is the lane pass itself. With
    `<= _last_initiative` that is refused; with `<` it is admitted, and a seat
    whose only "traffic" was its own previous lane pass buys another one off
    it — the self-feeding loop. First written with a turn at 500 against a
    pass at 1000, where `<` and `<=` agree and the mutant stayed green.
    """
    d = _seat(home)
    d._last_initiative = 1000.0
    d._turn_times = [1000.0]               # the lane pass IS the last turn

    assert not _lane(d, monkeypatch)


def test_traffic_since_the_last_pass_still_opens_the_lane(home, monkeypatch):
    """The other side of that gate, unchanged: a turn happened after the last
    lane pass, so the room is alive and the seat may think."""
    d = _seat(home)
    d._last_initiative = 1000.0
    d._turn_times = [1500.0]

    assert _lane(d, monkeypatch)


def test_a_newborn_that_OWES_something_settles_it_instead(home, monkeypatch):
    """Reception outranks a thought, newborn or not. A seat spawned into a
    room that already named it must answer, not muse — and the lane's own
    docstring says an unread answer outranks a thought."""
    d = _seat(home)
    owing = ReceptionDebt(to_answer=frozenset({"01ASK"}),
                          refs=(("01ASK", "commons#1"),))

    assert not _lane(d, monkeypatch, debt=owing)


def test_a_newborn_whose_hub_walk_failed_gets_nothing(home, monkeypatch):
    """`_scan_ok` false means the store walk did not complete. A seat that
    cannot see its own work must never conclude it has none — the same rule
    the loop applies before printing `no-continuable-work`."""
    d = _seat(home)
    d._scan_ok = False

    assert not _lane(d, monkeypatch)


def test_a_newborn_DELEGATE_still_has_no_lane(home, monkeypatch):
    """A delegate holding no row is told to supervise; INITIATIVE_PROMPT says
    "open a claim row and do a first real slice", which is the contradiction
    the SUPERVISE branch exists to delete. Birth does not change that."""
    d = _seat(home)
    monkeypatch.setattr(d, "_is_delegate_seat", lambda: True)

    assert not _lane(d, monkeypatch)
