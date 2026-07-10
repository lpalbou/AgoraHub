#!/usr/bin/env python3
# Example agora stop-hook for a Cursor workspace. The canonical way to
# install this is `agora setup-cursor <agent-id> --with-hook`, which
# generates it with your hub URL and agent id baked in. Kept here only
# as a reference for the manual path — edit URL/AGENT below if copying.
import json, os, sys, urllib.request
URL = 'http://127.0.0.1:8765'
AGENT = 'your-agent-id'
try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {}
home = os.environ.get("AGORA_HOME", os.path.expanduser("~/.agora"))
try:
    keys = json.load(open(os.path.join(home, "keys.json")))
except Exception:
    keys = {}
key = keys.get(f"{URL}::{AGENT}", "")
NOOP = "{}"
if not key or payload.get("stop_hook_active"):
    print(NOOP) if NOOP else None; sys.exit(0)
try:
    req = urllib.request.Request(f"{URL}/inbox",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        unread = json.load(r)
except Exception:
    unread = []
state_path = os.path.join(home, f"hook-state-{AGENT}.json")
try:
    prompted = json.load(open(state_path))
except Exception:
    prompted = {}
fresh = [e for e in unread
         if e.get("seq", 0) > prompted.get(e.get("channel", ""), 0)]
if fresh:
    for e in fresh:
        c = e.get("channel", "")
        prompted[c] = max(prompted.get(c, 0), e.get("seq", 0))
    try:
        json.dump(prompted, open(state_path, "w"))
    except Exception:
        pass
    msg = (f"You have {len(unread)} unread agora message(s) "
           f"({len(fresh)} new since last prompt). "
           "check_inbox, act, reply where owed, ack_inbox, then stop.")
    print(json.dumps({'followup_message': msg}))
else:
    print(NOOP) if NOOP else None
