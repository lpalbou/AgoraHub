"""NO TRUNCATION, NO SILENT FALLBACK, NO SILENT LIMIT (operator, standing).

A cap may REFUSE a write. It may never quietly deliver less than was written
and let the author believe it arrived whole.

WHAT WENT WRONG. On 2026-08-06 an operator set a three-rule mission on a
delegate seat. It went through `sanitize_text(text, 500)`, which ended in
`[:cap]`. The write returned 200. Rule 2 was cut mid-word, rule 3 was gone,
and the seat ran for an hour holding one and a half rules. The only way it
was found was reading the stored value by hand.

That call was one of forty. The mechanism was the defect, not the call site,
so the mechanism is what changed: `sanitize_text`/`sanitize_block` now RAISE
`TextTooLong`, and the only function in the codebase that shortens anything
is named `elide` and leaves a visible marker.

This file is the tripwire. It is deliberately grep-shaped: a future author
who reaches for `[:cap]` on a stored field trips it in CI rather than in a
field test six weeks later.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agora.hub.app import create_app
from agora.models import (
    MAX_ABOUT_CHARS,
    TextTooLong,
    elide,
    sanitize_block,
    sanitize_text,
)

SRC = Path(__file__).parent.parent / "src" / "agora"
ADMIN_KEY = "test-admin-key"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}


@pytest.fixture()
def client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY, rate_per_minute=600.0)
    return TestClient(app)


# -- the primitives --------------------------------------------------------

def test_the_sanitizers_refuse_rather_than_trim():
    for fn in (sanitize_text, sanitize_block):
        assert fn("x" * 10, 10) == "x" * 10          # exactly at cap is fine
        with pytest.raises(TextTooLong) as exc:
            fn("x" * 11, 10, field="mission")
        assert exc.value.field == "mission"
        assert exc.value.length == 11 and exc.value.cap == 10
        assert exc.value.status_code == 400


def test_the_refusal_says_what_to_shorten_and_by_how_much():
    """An error that only says 'too long' makes the author guess. The one
    thing they need is the number."""
    with pytest.raises(TextTooLong) as exc:
        sanitize_text("x" * 750, 500, field="about")
    msg = str(exc.value)
    assert "about" in msg and "750" in msg and "500" in msg
    assert "250" in msg                        # how much to cut


def test_elide_is_the_only_shortener_and_it_leaves_a_mark():
    assert elide("short", 10) == "short"       # untouched under the limit
    out = elide("abcdefghij", 6)
    assert out.endswith("…") and len(out) == 6
    assert out != "abcdef"                     # never a bare silent prefix


# -- the write path end to end --------------------------------------------

def test_an_over_cap_about_is_a_400_and_stores_nothing(client):
    r = client.post("/agents", headers=ADMIN, json={"id": "lead"})
    seat = {"Authorization": f"Bearer {r.json()['api_key']}"}
    client.put("/me/about", headers=seat, json={"about": "the real one"})

    over = client.put("/me/about", headers=seat,
                      json={"about": "y" * (MAX_ABOUT_CHARS + 1)})
    assert over.status_code == 400
    assert str(MAX_ABOUT_CHARS) in over.text
    # The previous value survives: a refused write is a no-op, never a
    # partial one.
    assert client.get("/whoami", headers=seat).json()["about"] == "the real one"


def test_an_over_cap_mission_is_a_400_and_stores_nothing(client):
    """The exact 2026-08-06 failure, as a test."""
    from agora.models import MAX_MISSION_CHARS

    client.post("/agents", headers=ADMIN, json={"id": "lead"})
    client.put("/admin/agents/lead/mission", headers=ADMIN,
               json={"mission": "Delegate. Never decides alone."})

    over = client.put("/admin/agents/lead/mission", headers=ADMIN,
                      json={"mission": "y" * (MAX_MISSION_CHARS + 1)})
    assert over.status_code == 400
    rows = {a["agent_id"]: a["mission"]
            for a in client.get("/admin/missions", headers=ADMIN).json()}
    assert rows["lead"] == "Delegate. Never decides alone."


# -- the tripwire ----------------------------------------------------------

#: Modules whose whole job is rendering for a human eye. Shortening a table
#: cell is not the violation — storing half a field is. These still may not
#: use a bare slice on a stored value; they are exempt only from the
#: "no shortening at all" reading.
_DISPLAY_MODULES = {"chat.py", "chat_render.py", "render.py", "cli.py",
                    "summarize.py", "hook.py", "vote.py", "listen.py",
                    "search_index.py"}

_SANITIZER_CALL = re.compile(r"\bsanitize_(?:text|block|title)\s*\(")


def test_no_sanitizer_result_is_sliced_anywhere():
    """`sanitize_text(x, cap)[:n]` would reintroduce the exact defect while
    looking like it respects the new contract."""
    offenders = []
    for path in SRC.rglob("*.py"):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if _SANITIZER_CALL.search(line) and re.search(r"\)\s*\[\s*:", line):
                offenders.append(f"{path.relative_to(SRC)}:{n}: {line.strip()}")
    assert not offenders, (
        "a sanitizer result is being sliced — use `elide` if this is display, "
        "or let the cap refuse if it is a write:\n" + "\n".join(offenders))


def test_no_named_cap_is_applied_with_a_silent_slice():
    """`value[:MAX_SOMETHING_CHARS]` is a text cap by definition — the name
    says so. A cap must refuse; only `elide` may shorten.

    Deliberately narrow: it looks for slices bounded by a MAX_* constant or a
    `cap`/`limit` name, which are unambiguously caps, and ignores list paging
    (`rows[:6]`) and hash prefixes (`digest()[:12]`), which are neither text
    nor lossy in the sense that matters."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text()
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Subscript):
                continue
            sl = node.slice
            if not (isinstance(sl, ast.Slice) and sl.lower is None
                    and sl.upper is not None and sl.step is None):
                continue
            up = sl.upper
            named_cap = (isinstance(up, ast.Name)
                         and (up.id.startswith("MAX_") or up.id in {"cap", "limit"}))
            if not named_cap:
                continue
            src = ast.get_source_segment(text, node) or ""
            if "text[:max(0, limit" in src:      # elide's own single slice
                continue
            # List paging that STATES what it left out is not a silent limit.
            # The bound is real (a digest of 200 rooms is not a digest); the
            # omission is reported in the same object the reader gets.
            if "MAX_CHANNELS" in src and "channels_omitted" in text:
                continue
            offenders.append(f"{path.relative_to(SRC)}:{node.lineno}: {src[:90]}")
    assert not offenders, (
        "a named cap is being applied as a silent slice — refuse with "
        "TextTooLong on a write path, or use `elide` at a display boundary:\n"
        + "\n".join(offenders))


def test_a_signature_is_never_shortened(client):
    """Half a signature is not a shorter signature, it is a corrupt one —
    and it would verify as forged the day verification is wired up."""
    from agora.models import MAX_SIGNATURE_CHARS

    r = client.post("/agents", headers=ADMIN, json={"id": "alice"})
    alice = {"Authorization": f"Bearer {r.json()['api_key']}"}
    client.post("/channels", json={"name": "design"}, headers=alice)

    ok = client.post("/channels/design/messages", headers=alice,
                     json={"body": "b", "signature": "s" * MAX_SIGNATURE_CHARS})
    assert ok.status_code == 200

    over = client.post("/channels/design/messages", headers=alice,
                       json={"body": "b", "signature": "s" * (MAX_SIGNATURE_CHARS + 1)})
    assert over.status_code == 400 and "signature" in over.text
