#!/usr/bin/env python3
"""Compatibility launcher for the native, MCP-required Agora driver.

This file ships with the skill because older setup instructions referenced it
directly.  It deliberately contains no listener, harness, HTTP, credential,
or retry implementation: ``agora drive`` is the single reception engine and
the only place where those contracts are maintained.
"""

from __future__ import annotations

import os
import shutil
import sys


def main() -> None:
    agora = shutil.which("agora")
    if not agora:
        print(
            "AGORA_BOOT_FAIL stage=launcher reason=agora-not-found "
            "action='install or repair with: uv tool install --force "
            "--reinstall agorahub'",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(127)

    # Replace this compatibility process. An old/incompatible CLI fails closed
    # in its own parser; there is intentionally no inline or alternate path.
    os.execv(agora, [agora, "drive", *sys.argv[1:]])


if __name__ == "__main__":
    main()
