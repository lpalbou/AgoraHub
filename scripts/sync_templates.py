#!/usr/bin/env python3
"""Regenerate docs/templates/*.md from the canonical constants in
src/agora/governance.py (the hub serves those constants; the docs copies
exist for humans browsing the repo). tests/test_governance.py fails when
the two drift — run this script to re-sync after editing the module."""

from pathlib import Path

from agora.governance import CHANNEL_CHARTER_TEMPLATE, HUB_RULES_DEFAULT

NOTE = ("<!-- Human-readable copy of the canonical text in src/agora/governance.py.\n"
        "     A test (tests/test_governance.py) keeps the two in sync — edit the\n"
        "     module, then regenerate this file with scripts/sync_templates.py. -->\n")

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    docs = ROOT / "docs" / "templates"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "hub_rules.md").write_text(NOTE + HUB_RULES_DEFAULT)
    (docs / "channel_charter.md").write_text(NOTE + CHANNEL_CHARTER_TEMPLATE)
    print(f"synced {docs}/hub_rules.md and channel_charter.md")


if __name__ == "__main__":
    main()
