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
