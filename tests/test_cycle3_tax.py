"""Cycle 3 — the protocol tax, measured on the 2026-09-07 fleet run: 42% of
context tokens spent on agora protocol calls, a ~10.4-11.2k-token agora prefix
per turn (SKILL.md 7.6k + 58 tool schemas), and 7% of the bill on calls the
hub refused (95 of them a title over 120 chars). Each test goes red if its
lever is removed."""

from __future__ import annotations

from agora import drive
from agora.mcp.server import _DRIVEN_DROP, tools_to_drop
from agora.models import MAX_TITLE_CHARS


def test_driven_prefix_is_the_contract_not_the_whole_skill():
    text = drive._skill_text()
    assert 0 < len(text) < 4000, f"{len(text)} chars: the driven prefix must stay small"
    for phrase in ("check_inbox", "claim:", "fyi", "answers=", "never", "work product"):
        assert phrase in text, phrase


def test_driven_tier_drops_the_never_called_tools_and_keeps_the_working_set():
    member = {"ok": True, "operator": False, "delegations": []}
    drop = tools_to_drop(member, driven=True)
    for gone in ("wait_for_messages", "open_vote", "create_group", "get_board", "rate_agent"):
        assert gone in drop, gone
    for kept in ("check_inbox", "ack_inbox", "post_message", "read_message", "read_message_by_seq",
                 "store_set", "store_get", "fs_write", "fs_read", "search_hub", "send_dm", "read_charter"):
        assert kept not in drop, kept
    delegate = {"ok": True, "operator": False, "delegations": [{"powers": ["reporting"]}]}
    assert "supervise" not in tools_to_drop(delegate, driven=True), "delegates keep their radar"
    assert tools_to_drop({"ok": True, "operator": True}, driven=True) == set(), "operators see everything"
    assert not (tools_to_drop(member) & _DRIVEN_DROP), "the driven set is opt-in: interactive seats keep it"


def test_driver_serves_the_driven_tier_to_its_mcp_server(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    d = drive.Driver("worker", "http://hub:1", harness="claude", cwd=tmp_path)
    cmd = d._adapter.build_command("p", None)
    joined = " ".join(cmd)
    assert '"--tools", "driven"' in joined and "AGORA_MCP_TOOLS" not in joined


def test_over_cap_title_is_elided_at_the_mcp_server_not_refused():
    """The MCP server shortens an over-cap title visibly before the POST, so the
    hub's cap (a guaranteed-read surface) holds without a refusal round trip."""
    import inspect
    import agora.mcp.server as srv
    src = inspect.getsource(srv)
    i = src.index("def post_message(")
    body = src[i:src.index('return _call("POST", f"/channels/{channel}/messages"', i)]
    assert "elide(title.strip(), MAX_TITLE_CHARS)" in body
    from agora.models import elide
    assert len(elide("x" * (MAX_TITLE_CHARS + 10), MAX_TITLE_CHARS)) <= MAX_TITLE_CHARS


# --- D1/D2: reception in the prompt, ack of the presented snapshot ------------------

def _fake_hub(monkeypatch, owed, inbox, posts):
    class R:
        def __init__(self, payload): self._p = payload
        def json(self): return self._p
        status_code = 200
    def get(url, **k):
        return R(owed if url.endswith("/owed") else inbox)
    def post(url, **k):
        posts.append((url, k.get("json"))); return R({"ok": True})
    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr("httpx.post", post)
    monkeypatch.setattr("agora.config.get_cached_key", lambda hub, aid: "agora_" + "k" * 40)


def test_reception_block_renders_owed_and_inbox_and_collects_cursors(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    posts = []
    _fake_hub(monkeypatch,
              owed={"to_answer": [{"channel": "work", "seq": 8, "sender": "laurent", "reason": "asks_pending",
                                   "due": True, "asks_naming_you": ["1"], "title": "COMMISSION"},
                                  {"channel": "work", "seq": 9, "sender": "laurent", "reason": "names_you",
                                   "due": False, "asks_naming_you": [], "title": "FYI: heading style"}],
                    "to_consume": [{"channel": "work", "answer_seq": 14, "answered_by": "alpha", "your_asks": ["1"]}]},
              inbox=[{"channel": "work", "seq": 12, "sender": "beta", "status": "open", "to_me": True, "title": "beta: literal?"},
                     {"channel": "dm:alpha--me", "seq": 3, "sender": "alpha", "status": "fyi", "title": "hi"}],
              posts=posts)
    d = drive.Driver("me", "http://hub:1", harness="codex", cwd=tmp_path)
    block, cursors = d._reception_block()
    assert block.startswith("RECEPTION")
    assert "OWE work#8 from laurent [asks_pending; DUE] asks naming you ['1']" in block
    assert "OWE work#9 from laurent [names_you; waits]" in block, "a waiting row is still SHOWN (criterion d)"
    assert "USE work#14: alpha answered your ask ['1']" in block
    assert "OPEN work#12 from beta [to_me]" in block
    assert cursors == {"work": 12, "dm:alpha--me": 3}, "the per-channel high-water mark of what was presented"


def test_run_turn_carries_reception_and_acks_only_what_it_presented(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    posts = []
    _fake_hub(monkeypatch, owed={"to_answer": [], "to_consume": []},
              inbox=[{"channel": "work", "seq": 5, "sender": "x", "status": "open", "to_me": True, "title": "t"}],
              posts=posts)
    seen = []
    d = drive.Driver("me", "http://hub:1", harness="codex", cwd=tmp_path,
                     spawn=lambda prompt, sid: (seen.append(prompt) or (sid or "s1", True)))
    d.verify_reception_debt = False
    assert d.run_turn()
    assert seen and seen[0].startswith(drive.BOOT_PROMPT) and "RECEPTION" in seen[0] and "OPEN work#5" in seen[0]
    acks = [j for (u, j) in posts if u.endswith("/inbox/ack")]
    assert acks == [{"cursors": {"work": 5}}], acks


def test_a_failed_turn_acks_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    posts = []
    _fake_hub(monkeypatch, owed={"to_answer": [], "to_consume": []},
              inbox=[{"channel": "work", "seq": 5, "sender": "x", "status": "open", "to_me": True, "title": "t"}],
              posts=posts)
    d = drive.Driver("me", "http://hub:1", harness="codex", cwd=tmp_path, spawn=lambda p, s: (s, False))
    d.verify_reception_debt = False
    d.run_turn()
    assert not [j for (u, j) in posts if u.endswith("/inbox/ack")]


def test_without_a_hub_the_prompt_is_exactly_the_static_one(tmp_path, monkeypatch):
    """No key / no hub -> no block -> the prompt is byte-identical to before,
    so every existing prompt assertion keeps passing for the right reason."""
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    monkeypatch.setattr("agora.config.get_cached_key", lambda hub, aid: None)
    seen = []
    d = drive.Driver("me", "http://hub:1", harness="codex", cwd=tmp_path,
                     spawn=lambda prompt, sid: (seen.append(prompt) or ("s1", True)))
    d.verify_reception_debt = False
    d.run_turn()
    assert seen == [drive.BOOT_PROMPT]


def test_verdict_relaxation_is_narrow_and_never_on_a_truncated_block(tmp_path, monkeypatch):
    """Reviewer Round 2, Q1b + BLOCKER 2: only `incomplete-reception-pass` is
    relaxed when the reception rode in the prompt; `no-agora-tool-call` (the
    seat called nothing) is not, and nothing is relaxed when the block could
    not show everything."""
    from agora.drive import Driver, TurnEvidence
    d = Driver("worker", "http://hub:1", harness="claude", cwd=tmp_path)
    monkeypatch.setattr(d, "_verify_reception_debt", lambda ev, kind: ev)
    monkeypatch.setattr(d, "_classify_provider_failure", lambda ev, err: ev)
    def verdict(reason, presented, truncated):
        d._presented_cursors = {"work": 3} if presented else {}
        d._presented_truncated = truncated
        ev = TurnEvidence(ok=False, stage="mcp-use", reason=reason, tools=("read_message",))
        monkeypatch.setattr(d._adapter, "assess_turn", lambda *a, **k: ev)
        return d._assess(stdout_text="", stderr_text="", returncode=0, kind="wake")
    assert verdict("incomplete-reception-pass", True, False).ok is True
    assert verdict("incomplete-reception-pass", True, True).ok is False, "truncated: told it saw everything and did not"
    assert verdict("no-agora-tool-call", True, False).ok is False, "called nothing: produced nothing"
    assert verdict("incomplete-reception-pass", False, False).ok is False, "nothing presented: the old rule"


def test_reception_block_says_when_it_is_incomplete(tmp_path, monkeypatch):
    from agora import drive as _drive
    from agora.drive import Driver
    d = Driver("worker", "http://hub:1", harness="claude", cwd=tmp_path)
    monkeypatch.setattr(_drive._config, "get_cached_key", lambda hub, aid: "k")
    owed = {"to_answer": [{"channel": "work", "seq": i, "sender": "peer", "reason": "asks_pending",
                           "title": f"q{i}", "due": True} for i in range(_drive.RECEPTION_OWED_CAP + 3)],
            "to_consume": []}
    class R:
        def __init__(self, payload): self._p = payload
        def json(self): return self._p
    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, **kw: R(owed if url.endswith("/owed") else []))
    block, cursors = d._reception_block()
    assert "and 3 more not shown — call check_inbox" in block and "this list is incomplete" in block
    assert d._presented_truncated is True
