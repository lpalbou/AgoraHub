"""Chat REPL helpers + the channel-stats surface it relies on.

The REPL's interactive loop is exercised manually; what must not regress
mechanically: line parsing (say vs command vs escaped slash), title
derivation (the triage headline every agent reads), the room directory
rendering, and the hub's channel stats that feed it.
"""

import time

from fastapi.testclient import TestClient

from agora.chat import channel_table, derive_title, fmt_age, parse_line
from agora.hub.app import create_app

ADMIN_KEY = "test-admin"


def register(client: TestClient, agent_id: str) -> dict[str, str]:
    r = client.post("/agents", json={"id": agent_id},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


# -- parse_line ---------------------------------------------------------------

def test_plain_text_is_say():
    assert parse_line("hello everyone") == ("say", "hello everyone")


def test_slash_command_with_argument():
    assert parse_line("/switch entity-society") == ("switch", "entity-society")


def test_command_is_case_insensitive_and_bare_slash_is_help():
    assert parse_line("/WHO")[0] == "who"
    assert parse_line("/")[0] == "help"


def test_double_slash_escapes_a_literal_slash_message():
    assert parse_line("//etc/hosts is fine") == ("say", "/etc/hosts is fine")


# -- derive_title --------------------------------------------------------------

def test_title_is_first_line_collapsed():
    assert derive_title("fix the seam\nlong details follow") == "fix the seam"
    assert derive_title("  spaced   out   words  ") == "spaced out words"


def test_title_truncates_at_word_boundary():
    text = "word " * 40
    title = derive_title(text)
    assert len(title) <= 81 and title.endswith("…") and not title[:-1].endswith(" wor")


# -- fmt_age -------------------------------------------------------------------

def test_fmt_age_bands():
    assert fmt_age(None) == "-"
    assert fmt_age(30) == "now"
    assert fmt_age(300) == "5m"
    assert fmt_age(7200) == "2h"
    assert fmt_age(200000) == "2d"


# -- channel_table ---------------------------------------------------------------

def test_channel_table_marks_current_and_unjoined():
    now = time.time()
    channels = [
        {"name": "design", "private": True, "member": True,
         "member_count": 3, "last_seq": 42, "last_at": now - 120},
        {"name": "commons", "private": False, "member": False,
         "member_count": 9, "last_seq": 118, "last_at": now - 7200},
    ]
    table = channel_table(channels, {"design": 5}, current="design", now=now)
    design_row = next(l for l in table.splitlines() if "design" in l)
    commons_row = next(l for l in table.splitlines() if "commons" in l)
    assert design_row.strip().startswith(">") and " 5" in design_row
    assert commons_row.strip().startswith("·") and "public" in commons_row


def test_channel_table_separates_dms_with_peer_names():
    from agora.chat_render import Style, channel_table as render_table
    now = time.time()
    channels = [
        {"name": "commons", "private": False, "member": True,
         "member_count": 9, "last_seq": 118, "last_at": now - 60},
        {"name": "dm:laurent--runtime", "private": True, "member": True,
         "member_count": 2, "last_seq": 4, "last_at": now - 30},
    ]
    table = render_table(Style(False), channels, {}, current=None, me="laurent")
    assert "── direct messages ──" in table
    assert "@runtime" in table                      # peer, not the raw dm slug
    assert "dm:laurent--runtime" not in table.split("direct messages")[1].splitlines()[1]


# -- message_block (the one layout for history/live/read) -----------------------

def test_message_block_caps_long_bodies_with_read_hint():
    from agora.chat_render import Style, message_block
    body = "\n".join(f"line {i} " + "word " * 30 for i in range(40))
    block = message_block(Style(False), sender="runtime", seq=42, status="open",
                          created_at=time.time(), title="big report", body=body)
    lines = block.splitlines()
    assert len(lines) < 20                      # capped, not a wall
    assert "/read 42" in lines[-1] and "more line" in lines[-1]
    assert "big report" in block                # title surfaced
    assert "#42" in block and "open" in block


def test_message_block_marks_dms_and_foreign_channels():
    from agora.chat_render import Style, message_block
    s = Style(False)
    dm = message_block(s, sender="runtime", seq=3, status="fyi",
                       created_at=time.time(), body="hi",
                       me="laurent", channel="dm:laurent--runtime")
    assert dm.splitlines()[1].startswith("DM ")
    other = message_block(s, sender="memory", seq=9, status="reply",
                          created_at=time.time(), body="x",
                          me="laurent", channel="entity-society",
                          show_channel=True)
    assert "in entity-society" in other


def test_file_event_line_shows_edit_size():
    from agora.chat_render import Style, file_event_line
    line = file_event_line(Style(False), sender="observer",
                           title="fs:put plans/plan.md", channel="commons",
                           current="commons",
                           data={"version": 2, "size_bytes": 8280})
    assert "observer" in line and "put plans/plan.md" in line
    assert "v2" in line and "8280B" in line and "/fs plans/plan.md" in line
    # Degrades without data (live envelopes may not inline it).
    bare = file_event_line(Style(False), sender="core", title="fs:put x.md",
                           channel="commons", current="commons")
    assert "put x.md" in bare and "v" + "None" not in bare


def test_file_history_table_shows_created_then_deltas():
    from agora.chat_render import Style, file_history_table
    events = [
        {"sender": "gateway", "created_at": time.time(),
         "data": {"op": "put", "version": 1, "size_bytes": 8091}},
        {"sender": "observer", "created_at": time.time(),
         "data": {"op": "put", "version": 2, "size_bytes": 8280}},
    ]
    table = file_history_table(Style(False), "plans/plan.md", events)
    lines = table.splitlines()
    assert "created" in lines[1] and "8091B" in lines[1]
    assert "+189B" in lines[2] and "observer" in lines[2]


def test_wrap_body_preserves_paragraphs_and_counts_hidden():
    from agora.chat_render import wrap_body
    visible, hidden = wrap_body("a\n\nb", width=80, max_lines=10)
    assert [v.strip() for v in visible] == ["a", "", "b"] and hidden == 0
    visible, hidden = wrap_body("\n".join(str(i) for i in range(30)),
                                width=80, max_lines=10)
    assert len(visible) == 10 and hidden == 20


# -- dispatch: every /command in HELP must be wired ------------------------------

def test_every_help_command_is_dispatched():
    """The field bug this guards: /dm was documented in HELP and cmd_dm
    existed, but the dispatch table never registered it — users got
    'unknown command'. Every slash command HELP advertises must dispatch."""
    import asyncio
    import re

    from agora.chat import HELP, ChatApp

    app = ChatApp("http://127.0.0.1:1", "k", "tester")
    called = []
    # Stub every handler so dispatch resolves without I/O.
    for name in dir(app):
        if name.startswith("cmd_"):
            async def stub(*a, _n=name, **kw):
                called.append(_n)
            setattr(app, name, stub)

    commands = set(re.findall(r"^/([a-z]+)", HELP, re.MULTILINE))
    commands -= {"quit"}  # quit is handled before the table
    for cmd in sorted(commands):
        called.clear()
        keep_going = asyncio.run(app.dispatch(f"/{cmd} someargument here"))
        assert keep_going, f"/{cmd} unexpectedly quit"
        assert called, f"/{cmd} is in HELP but not dispatched"


# -- hub channel stats (the directory's data source) -----------------------------

def test_list_channels_carries_stats():
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY, rate_per_minute=600.0)
    client = TestClient(app)
    alice = register(client, "alice")
    bob = register(client, "bob")
    client.post("/channels", json={"name": "design"}, headers=alice)
    invite = client.post("/channels/design/invites", json={},
                         headers=alice).json()["invite_token"]
    client.post("/channels/design/join", json={"invite_token": invite}, headers=bob)
    for i in range(3):
        client.post("/channels/design/messages",
                    json={"body": f"m{i}", "title": f"m{i}"}, headers=alice)

    rows = client.get("/channels", headers=bob).json()
    design = next(r for r in rows if r["name"] == "design")
    assert design["member_count"] == 2
    # 3 posts + join system messages; head seq must match a fresh read.
    head = client.get("/channels/design/messages", headers=bob).json()[-1]["seq"]
    assert design["last_seq"] == head
    assert design["last_at"] is not None and design["last_at"] > 0
