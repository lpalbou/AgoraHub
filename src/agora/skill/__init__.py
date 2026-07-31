"""The agora-channels skill, shipped inside the package.

`agora setup <harness> <id>` installs/refreshes these files into the
harness's skills directory (see setup_harness.install_skill), so "start
agora protocol" works without any manual copying — and every setup re-run
re-syncs the skill to the installed agora version, killing copy drift.

SKILL.md is the skill (etiquette + the boot-phrase contract);
agora_protocol.py is a compatibility launcher that execs the native driver.
"""
