#!/usr/bin/env python3
"""Regenerate docs/templates/*.md from the canonical constants in
src/agora/governance.py (the hub serves those constants; the docs copies
exist for humans browsing the repo). tests/test_governance.py fails when
the two drift — run this script to re-sync after editing the module."""

from pathlib import Path

from agora.governance import (CHANNEL_CHARTER_SEED, CHANNEL_CHARTER_TEMPLATE,
                              GROUP_CHARTER_TEMPLATE, HUB_RULES_DEFAULT,
                              ROLE_CHARTER)

NOTE = ("<!-- Human-readable copy of the canonical text in src/agora/governance.py.\n"
        "     A test (tests/test_governance.py) keeps the two in sync — edit the\n"
        "     module, then regenerate this file with scripts/sync_templates.py. -->\n")

ROOT = Path(__file__).resolve().parent.parent


# Every packaged governance text, and the file it mirrors. group_charter.md
# was NOT in this table until 0146 and had silently drifted from the constant
# for weeks — an anti-drift lock with a hole in it is worse than none, because
# it is trusted.
TEXTS = {
    "hub_rules.md": HUB_RULES_DEFAULT,
    "hub_charter.md": ROLE_CHARTER,
    "channel_charter.md": CHANNEL_CHARTER_TEMPLATE,
    "channel_charter_seed.md": CHANNEL_CHARTER_SEED,
    "group_charter.md": GROUP_CHARTER_TEMPLATE,
}


def main() -> None:
    docs = ROOT / "docs" / "templates"
    docs.mkdir(parents=True, exist_ok=True)
    for name, text in TEXTS.items():
        (docs / name).write_text(NOTE + text)
    print(f"synced {len(TEXTS)} templates in {docs}: {', '.join(TEXTS)}")


if __name__ == "__main__":
    main()
