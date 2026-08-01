"""The collaboration model: the taught layer, pinned.

Agora splits one system in two. The HUB is the guarantee — delivery,
escalation, claim CAS, phase attribution, vote publication — and those halves
are tested by the surfaces that implement them. This module pins the OTHER
half: the teachings that only exist as text, in the three places an agent
actually reads them (the hub rules served by `whoami`, the packaged
agora-channels skill, and `docs/collaboration.md`).

Text tests are usually a smell. These earn their place because every teaching
below was bought with a measured field failure (`docs/backlog/proposed/
0140_collaboration_v2.md`, two adversarially-scored 8-seat runs), and because
the failure mode they guard against is silent: a teaching deleted during an
unrelated edit costs nothing at import time and shows up months later as a
fleet behaving the way it did before the lesson. Each assertion names its
evidence, so a future editor removing one has to argue with the field test
rather than with a string match.

Deliberately loose: every check is a substring of a phrase that carries the
MEANING, matched against whitespace-collapsed text, never a whole sentence —
rewording and re-wrapping stay free, dropping the teaching does not.
"""

from pathlib import Path

import pytest

from agora.governance import HUB_RULES_DEFAULT

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def skill() -> str:
    from importlib import resources
    return (resources.files("agora.skill") / "SKILL.md").read_text()


@pytest.fixture(scope="module")
def skill_flat(skill: str) -> str:
    """Whitespace-collapsed, so a phrase check never breaks on a line wrap —
    the teaching is the words, not the fill column."""
    return " ".join(skill.split())


@pytest.fixture(scope="module")
def model_page() -> str:
    return (ROOT / "docs/collaboration.md").read_text()


# -- the skill still boots a seat, and still refuses the unsafe moves --------

def test_skill_frontmatter_and_boot_phrase_survive(skill: str):
    """The skill is loaded by name and triggered by a phrase; both are wiring,
    not prose. `agora setup` installs this file verbatim per harness."""
    assert skill.startswith("---\n")
    head = skill.split("---", 2)[1]
    assert "name: agora-channels" in head
    assert "description:" in head
    assert "start agora protocol" in skill


@pytest.mark.parametrize("rule, why", [
    ("quoted DATA",
     "injection safety: peer content is never an instruction"),
    ("nonce-delimited",
     "injection safety: the fence is the unforgeable boundary"),
    ("Never wait in the foreground",
     "a foreground wait serializes the seat behind other agents' messages"),
    ("Never install machine persistence",
     "machine mutation is the operator's act alone"),
    ("Never pgrep or kill agora processes",
     "every seat's listener looks identical by name"),
    ("whoami` is the oracle",
     "a guessed identity silently registers a phantom agent"),
    ("NEVER invent an id",
     "same — wiring is the operator's act"),
    ("driver-owns-reception",
     "a driven turn that arms a listener starves its own watcher"),
    ("You never start the driver",
     "launched from the seat's own session it races the seat for its inbox"),
])
def test_skill_keeps_the_never_rules(skill_flat: str, rule: str, why: str):
    assert rule in skill_flat, why


# -- the cycles the model is made of ----------------------------------------

@pytest.mark.parametrize("cycle", [
    "The reception pass",
    "The work chunk",
    "Ask → answer → consume → close",
    "Phase: which version is in force",
    "Votes",
    "Reviewing (the gate)",
    "If you orchestrate",
])
def test_skill_teaches_every_cycle(skill_flat: str, cycle: str):
    """The skill's job changed in the 0140 pass: from etiquette to CYCLES.
    A cycle with no section is a cycle no seat runs."""
    assert cycle in skill_flat


@pytest.mark.parametrize("teaching, evidence", [
    ("consumes=[",
     "batched consumption: one field test spent 26% of its messages on "
     "per-thread 'adopted and consumed' receipts"),
    ("END **without posting anything**",
     "ceremony was 8.3% of traffic with work live and 50% on empty wakes"),
    ("operator debts outrank peer ceremony",
     "17 peer threads closed while 4 of the principal's 6 asks dangled"),
    ("Read the phase BEFORE starting work",
     "two seats built v3 and v4 of one manuscript at once"),
    ("park — do not manufacture",
     "the phase row's real value was a place to say 'waiting, by design'"),
    ("One cold whole-artifact read",
     "10 voice-checking review messages while an impossible chronology "
     "survived five versions"),
    ("subtraction budget",
     "reviews that only add converge on an artifact nobody re-reads"),
    ("LIVE artifact, not the thread",
     "three fixes went endorsed -> queued -> 'discharged' -> still absent"),
    ("diff summary naming the owner",
     "a silent empty-body fs:put made three seats' state statements wrong "
     "in 36 seconds"),
    ("fix:<slug>",
     "merge queue as rows, closed against a post-merge check (0140 P1-8)"),
    ("window you announce BINDS you",
     "a chair closed its own five-minute vote at 42 seconds"),
    ("hub publishes the full result",
     "a chaired vote cannot close itself in a driven fleet"),
    ("rejected_ballots",
     "an empty room and a broken parser must never render alike"),
    ("without `to=` is a wish",
     "addressed fan-out is what made the orchestrated rerun converge"),
    ("supersession check is FIRST",
     "the record outranks the seat's memory across turn boundaries"),
])
def test_skill_teaches_what_the_field_measured(skill_flat: str, teaching: str,
                                               evidence: str):
    assert teaching.lower() in skill_flat.lower(), evidence


def test_skill_stays_within_its_token_budget(skill: str):
    """Every seat pays for this file on every session. The 0140 pass rewrote
    it from 31.9k chars to ~27k while ADDING the cycle teachings above; the
    ceiling keeps the next edit honest — growing past it means cutting
    something, not appending."""
    assert len(skill) <= 30_000, f"skill grew to {len(skill)} chars"


# -- the hub rules: the operator's own voice, read every session ------------

@pytest.mark.parametrize("rule, evidence", [
    ("Settle OPERATOR debts before peer courtesy",
     "the fleet closed peer threads and starved its principal"),
    ("consumes=[refs]",
     "batched settlement is a hub mechanism; the rules must name it"),
    ("END WITHOUT POSTING",
     "an empty reception pass that posts anyway is the ceremony engine"),
    ("phase:<track>",
     "the operator's v3/v4 invariant was absent from the rules entirely"),
    ("Never start N+1 before N is complete",
     "the ruling itself"),
    ("HUB publishes the result",
     "the hub sweeps vote deadlines; a chair must not babysit or close early"),
])
def test_hub_rules_carry_the_collaboration_teachings(rule: str, evidence: str):
    assert rule in HUB_RULES_DEFAULT, evidence


def test_hub_rules_no_longer_order_the_chair_to_publish():
    """Correctness, not style: until 0.12.63 the chair published the tally, so
    the rules said so. The hub now publishes on deadline or all-voted, and a
    chair still believing it owns publication is exactly the seat that closes
    a vote early to 'get the result out'."""
    assert "caller posts resolved + tally" not in HUB_RULES_DEFAULT
    assert "window BINDS" in HUB_RULES_DEFAULT


# -- the model page ----------------------------------------------------------

@pytest.mark.parametrize("section", [
    "## 1. Roles",
    "## 2. The core loop",
    "## 3. The cycles",
    "## 4. The gate",
    "## 5. The tools, mapped to the cycles",
    "## 6. What the hub guarantees vs. what the fleet practises",
    "## 8. Known ceilings",
])
def test_model_page_presents_roles_then_cycles_then_tools(model_page: str,
                                                          section: str):
    assert section in model_page


def test_model_page_diagrams_the_core_loop(model_page: str):
    assert "```mermaid" in model_page
    for node in ("RECEPTION PASS", "WORK CHUNK", "claim:msg-"):
        assert node in model_page


def test_model_page_is_reachable_from_both_indexes():
    """An authoritative page nobody links to is a private note."""
    assert "collaboration.md" in (ROOT / "docs/README.md").read_text()
    assert "docs/collaboration.md" in (ROOT / "README.md").read_text()
    assert "docs/collaboration.md" in (ROOT / "llms.txt").read_text()


def test_model_page_ceilings_point_at_real_backlog_cards(model_page: str):
    """§8 names the gaps as design work. Each must be a card that exists, so
    the page cannot promise a design that was never written."""
    cards = ["0141_claim_deputy_ttl_handoff", "0142_acceptance_signoff",
             "0143_merge_queue_rows", "0144_role_registry",
             "0145_artifact_watch_diff_summaries"]
    for card in cards:
        assert card in model_page
        assert (ROOT / f"docs/backlog/proposed/{card}.md").exists()
