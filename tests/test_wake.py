"""Hub-driven wake sink: the alarm clock that turns delivery into a turn.

Field incident this guards against (2026-07-09): six turn-gated sessions sat
deaf for an hour on an urgent directive that was delivered to every inbox and
notify file within one second. The wake sink must: fire for wake-worthy
traffic when nothing live is listening, coalesce bursts, respect its budget,
let criticals through everything, feed the digest via stdin (never argv), and
record failures the operator can see.
"""

import json
import time
from pathlib import Path

from agora.hub.wake_sink import WakeSink, load_wake_config, wake_worthy
from agora.models import Envelope, Kind, Status, Urgency


def env(**kw) -> Envelope:
    base = dict(id="01TEST", channel="design", seq=1, sender="alice",
                kind=Kind.message, status=Status.fyi, urgency=Urgency.inbox,
                effective_urgency=Urgency.inbox, title="t", created_at=time.time())
    base.update(kw)
    return Envelope(**base)


def make_sink(tmp_path, *, command: str, debounce=0.05, budget=2,
              connected=False, state="offline") -> WakeSink:
    config = {"defaults": {"debounce_seconds": debounce,
                           "max_wakes_per_hour": budget,
                           "command_timeout_seconds": 10},
              "agents": {"bob": {"command": command}}}
    return WakeSink(config,
                    digest_fn=lambda aid: f"digest for {aid}\n",
                    has_connection=lambda aid: connected,
                    state_fn=lambda aid: state,
                    log_path=tmp_path / "wake.log")


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# -- wake_worthy: fyi broadcasts must NOT wake anyone ---------------------------

def test_wake_worthy_matches_inbox_stickiness():
    assert not wake_worthy(env(status=Status.fyi))
    assert not wake_worthy(env(status=Status.reply))
    assert wake_worthy(env(status=Status.open))
    assert wake_worthy(env(status=Status.blocked))
    assert wake_worthy(env(status=Status.fyi, to_me=True))
    assert wake_worthy(env(status=Status.reply, reply_to_me=True))
    assert wake_worthy(env(status=Status.fyi, escalated=True))
    assert wake_worthy(env(status=Status.fyi, critical=True))


# -- firing, coalescing, stdin digest -------------------------------------------

def test_burst_coalesces_into_one_wake_with_digest_on_stdin(tmp_path):
    out = tmp_path / "woken.txt"
    sink = make_sink(tmp_path, command=f"cat > {out}")
    for seq in range(1, 4):  # burst within the debounce window
        sink.consider("bob", env(status=Status.open, seq=seq))
    assert wait_for(out.exists)
    time.sleep(0.15)  # would-be second fire window
    assert out.read_text() == "digest for bob\n"   # stdin, not argv
    assert sink.last_result["bob"]["ok"] is True
    # One coalesced wake spent one budget slot, not three.
    assert len(sink._budgets["bob"]._times) == 1


def test_no_wake_when_agent_has_live_connection(tmp_path):
    out = tmp_path / "woken.txt"
    sink = make_sink(tmp_path, command=f"cat > {out}", connected=True)
    sink.consider("bob", env(status=Status.open))
    time.sleep(0.2)
    assert not out.exists()   # a live push consumer already has the message


def test_active_agent_deferred_not_dropped_and_critical_bypasses(tmp_path):
    out = tmp_path / "woken.txt"
    state = {"value": "active"}
    config = {"defaults": {"debounce_seconds": 0.05, "max_wakes_per_hour": 5,
                           "command_timeout_seconds": 10,
                           "recheck_seconds": 0.1},
              "agents": {"bob": {"command": f"cat > {out}"}}}
    sink = WakeSink(config, digest_fn=lambda aid: "d\n",
                    has_connection=lambda aid: False,
                    state_fn=lambda aid: state["value"],
                    log_path=tmp_path / "wake.log")
    sink.consider("bob", env(status=Status.open))
    time.sleep(0.2)
    assert not out.exists()   # likely mid-turn: deferred, never interrupted
    state["value"] = "offline"  # the turn ended and the agent aged out
    assert wait_for(out.exists)  # the DEFERRED wake fires — not dropped

    out2 = tmp_path / "woken2.txt"
    state["value"] = "active"
    sink.agents["bob"]["command"] = f"cat > {out2}"
    sink.consider("bob", env(status=Status.fyi, critical=True))
    assert wait_for(out2.exists)  # forced attention bypasses the active-skip


def test_budget_bounds_wakes_but_critical_bypasses(tmp_path):
    counter = tmp_path / "count.txt"
    sink = make_sink(tmp_path, command=f"echo x >> {counter}", budget=1)
    sink.consider("bob", env(status=Status.open, seq=1))
    assert wait_for(lambda: counter.exists() and counter.read_text() == "x\n")

    sink.consider("bob", env(status=Status.open, seq=2))   # budget exhausted
    time.sleep(0.2)
    assert counter.read_text() == "x\n"
    assert sink.last_result["bob"]["note"] == "wake budget exhausted"

    sink.consider("bob", env(status=Status.fyi, critical=True, seq=3))
    assert wait_for(lambda: counter.read_text() == "x\nx\n")


def test_unconfigured_agent_is_ignored(tmp_path):
    sink = make_sink(tmp_path, command="true")
    sink.consider("stranger", env(status=Status.open))
    time.sleep(0.1)
    assert "stranger" not in sink.last_result


# -- failure visibility -----------------------------------------------------------

def test_failed_command_is_recorded_and_logged(tmp_path):
    sink = make_sink(tmp_path, command="echo broken >&2; exit 3")
    sink.consider("bob", env(status=Status.open))
    assert wait_for(lambda: "bob" in sink.last_result)
    result = sink.last_result["bob"]
    assert result["ok"] is False and result["exit_code"] == 3
    assert "broken" in result["note"]
    logged = [json.loads(l) for l in (tmp_path / "wake.log").read_text().splitlines()]
    assert logged[-1]["agent_id"] == "bob" and logged[-1]["ok"] is False


# -- config safety -----------------------------------------------------------------

def test_world_readable_wake_config_is_refused(tmp_path, capsys):
    path = tmp_path / "wake.json"
    path.write_text(json.dumps({"agents": {"bob": {"command": "true"}}}))
    path.chmod(0o644)
    assert load_wake_config(path) is None      # hub runs shell commands from
    assert "DISABLED" in capsys.readouterr().err  # this file: 0600 or nothing

    path.chmod(0o600)
    config = load_wake_config(path)
    assert config is not None and "bob" in config["agents"]


def test_missing_or_malformed_config_disables_wake(tmp_path, capsys):
    assert load_wake_config(tmp_path / "absent.json") is None
    bad = tmp_path / "wake.json"
    bad.write_text("{not json")
    bad.chmod(0o600)
    assert load_wake_config(bad) is None


# -- end to end through the hub ---------------------------------------------------

def test_post_wakes_configured_offline_agent_end_to_end(tmp_path):
    """The incident scenario, inverted: an open question lands for an agent
    with no live connection -> the hub itself runs the wake command with the
    full inbox digest on stdin. Delivery becomes a turn."""
    from fastapi.testclient import TestClient

    from agora.hub.app import create_app

    woken = tmp_path / "woken.txt"
    app = create_app(
        db_path=":memory:", admin_key="test-admin", rate_per_minute=600.0,
        wake_config={"defaults": {"debounce_seconds": 0.05},
                     "agents": {"bob": {"command": f"cat > {woken}"}}},
        wake_log=str(tmp_path / "wake.log"))
    client = TestClient(app)

    def register(agent_id):
        r = client.post("/agents", json={"id": agent_id},
                        headers={"Authorization": "Bearer test-admin"})
        return {"Authorization": f"Bearer {r.json()['api_key']}"}

    alice, bob = register("alice"), register("bob")
    client.post("/channels", json={"name": "design"}, headers=alice)
    invite = client.post("/channels/design/invites", json={},
                         headers=alice).json()["invite_token"]
    client.post("/channels/design/join", json={"invite_token": invite},
                headers=bob)
    # The incident precondition: bob has been idle past the activity window
    # (a just-active agent is deliberately deferred, not woken mid-turn).
    app.state.service.presence._last_seen["bob"] = time.time() - 3600
    client.post("/channels/design/messages",
                json={"body": "are you there?", "title": "urgent seam question",
                      "status": "open", "to": ["bob"]},
                headers=alice)

    assert wait_for(woken.exists), "hub never ran the wake command"
    digest = woken.read_text()
    assert "urgent seam question" in digest  # the digest carries the inbox
