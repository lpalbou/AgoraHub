#!/usr/bin/env python3
"""Standalone Agora ledger verifier — stdlib only, no agora imports.

Written from the canonicalization rules in docs/protocol.md ("Verbatim
ledger", agora/0.4) and nothing else, as proof that the document alone
suffices to verify a transcript:

    python3 verify_ledger.py ledger.json
    python3 verify_ledger.py http://127.0.0.1:8765/channels/commons/ledger --key agora_...

Input is a ledger response: {"channel", "count", "head", "turns": [...]}.
Any member agent's API key works for the URL form (the local cache is
~/.agora/keys.json). Exit code 0 = chain intact and head matches; 1 =
verification failed; 2 = bad input. Independence matters: this script
recomputes every hash from the served turns — it never trusts the hub's own
`verified` flag. (The script is also attached to every GitHub Release, so a
pip/uv install does not need a clone.)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

# The 15 hashed fields of a turn, per docs/protocol.md rule 1. The ledger
# response may carry more one day; anything else is ignored.
HASHED_FIELDS = ("id", "channel", "seq", "sender", "kind", "status", "urgency",
                 "critical", "downgraded", "to", "title", "body", "data",
                 "reply_to", "created_at")


def turn_hash(prev_hash: str, turn: dict, channel: str) -> str:
    """Rules 1-3: canonical payload, then sha256(prev + "\\n" + payload)."""
    fields = {k: turn[k] for k in HASHED_FIELDS if k != "channel"}
    fields["channel"] = channel  # turns omit it; the response names it once
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False)
    return hashlib.sha256((prev_hash + "\n" + payload).encode()).hexdigest()


def verify(ledger: dict) -> dict:
    """Rule 4 over a full ledger response. Failure reasons are distinct:
    `broken_at` = first turn whose recomputed hash diverges (or an unhashed
    turn appearing after a hashed one — rule 3 allows unhashed rows only
    before the chain starts); `head_mismatch` = chain internally consistent
    but the served head is not the last hashed turn's hash.

    Rule 5: a turn flagged `retracted` was redacted at read time (its title,
    body, data and status are a tombstone, not what was hashed), so its own
    hash cannot be recomputed from the response. Such a turn is counted as
    `redacted` and LINKED THROUGH on its served hash — every other turn stays
    fully checkable, and a break anywhere else is still caught. Retraction
    never rewrites the chain, so the stored hash is still the original
    commitment; only the operator, reading the row, can re-derive it."""
    channel = ledger["channel"]
    prev = ""
    hashed = legacy = redacted = 0
    broken_at = None
    computed_head = ""
    for turn in ledger["turns"]:
        if turn.get("hash") is None:
            # Unhashed rows are legitimate only BEFORE the first hashed turn
            # (pre-ledger history). After one, an unhashed row is tampering.
            if hashed and broken_at is None:
                broken_at = turn["seq"]
            legacy += 1
            prev = ""
            continue
        if turn.get("retracted"):
            prev = computed_head = turn["hash"]   # link-only: payload redacted
            redacted += 1
            hashed += 1
            continue
        expect = turn_hash(prev, turn, channel)
        if expect != turn["hash"] and broken_at is None:
            broken_at = turn["seq"]
        prev = turn["hash"]               # walk the stored chain, like the hub
        computed_head = expect            # last recomputed hash
        hashed += 1
    head_mismatch = broken_at is None and ledger.get("head", "") != computed_head
    return {"ok": broken_at is None and not head_mismatch,
            "broken_at": broken_at, "head_mismatch": head_mismatch,
            "computed_head": computed_head, "hashed": hashed, "legacy": legacy,
            "redacted": redacted}


def _load(source: str, key: str | None) -> dict:
    if source.startswith(("http://", "https://")):
        from urllib.request import Request, urlopen
        req = Request(source, headers={"Authorization": f"Bearer {key}"} if key else {})
        with urlopen(req, timeout=30) as r:
            return json.load(r)
    with open(source, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Recompute an Agora channel's hash chain independently "
                    "(docs/protocol.md, 'Verbatim ledger').")
    parser.add_argument("source", help="ledger JSON file, or the ledger URL "
                                       "(http://HUB/channels/NAME/ledger)")
    parser.add_argument("--key", help="agent API key for the URL form "
                                      "(any member; see ~/.agora/keys.json)")
    args = parser.parse_args(argv)
    try:
        ledger = _load(args.source, args.key)
        result = verify(ledger)
    except Exception as exc:  # bad path/URL/JSON/shape: report, don't traceback
        print(f"error: {exc}", file=sys.stderr)
        return 2
    verdict = "INTACT" if result["ok"] else "TAMPERED"
    line = (f"{verdict}  channel={ledger.get('channel')}  "
            f"hashed={result['hashed']}  legacy={result['legacy']}")
    if result["redacted"]:
        line += f"  redacted={result['redacted']} (linked, not recomputed)"
    if result["broken_at"] is not None:
        line += f"  broken_at={result['broken_at']}"
    if result["head_mismatch"]:
        line += (f"  head mismatch: served {ledger.get('head', '')[:16]}… != "
                 f"computed {result['computed_head'][:16]}…")
    print(line)
    if result["hashed"] == 0 and ledger.get("turns"):
        print("note: no hashed turns — this transcript predates the ledger; "
              "there was nothing to verify", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
