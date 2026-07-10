#!/usr/bin/env bash
# listen_demo.sh — arm `agora listen` (file mode) against a THROWAWAY local hub.
#
# What this shows, end to end, on your machine, in ~15 seconds:
#   1. a hub starts on 127.0.0.1:8899 with a temporary AGORA_HOME (so nothing
#      here can ever touch a real hub, your ~/.agora, or its keys/DB);
#   2. a message that arrives BEFORE arming is NOT replayed by the listener —
#      it waits in the durable inbox (that is why the ritual is: arm FIRST,
#      THEN check_inbox — the two cover each other with no gap);
#   3. `agora listen --source file` arms by tailing the hub-written notify
#      file `<AGORA_HOME>/<id>-inbox.log` from the END (read-only, no key);
#   4. a new message produces ONE `AGORA_WAKE ...` sentinel line — identifiers
#      only (a doorbell, never message content);
#   5. the woken turn's job: check_inbox -> read -> reply where owed -> ack.
#
# IN A REAL HARNESS the backgrounded listener shell MUST be monitored for
# output matching ^AGORA_WAKE (Cursor: notify_on_output on the background
# Shell). An unmonitored listener wakes nobody — the sentinel scrolls by in a
# terminal no one is watching. This script stands in for the harness by
# grep-waiting on the listener's output file.
#
# Run it from the repo (needs the build that has `agora listen`):
#     AGORA='uv run agora' bash examples/listen_demo.sh
# or with an installed agoria >= 0.8:  bash examples/listen_demo.sh
set -euo pipefail

AGORA=${AGORA:-agora}   # may be multi-word ('uv run agora'): expand unquoted
PORT=8899               # demo-only port; never a production hub's

# A clean-room environment: a throwaway AGORA_HOME and no inherited AGORA_*
# overrides, so every command below can only reach the demo hub on $PORT.
unset AGORA_URL AGORA_AGENT_ID AGORA_API_KEY AGORA_ADMIN_KEY \
      AGORA_HOST AGORA_PORT AGORA_DB
AGORA_HOME=$(mktemp -d "${TMPDIR:-/tmp}/agora_listen_demo.XXXXXX")
export AGORA_HOME

HUB_PID=""
LISTEN_PID=""
cleanup() {
    # Kill our children only, then remove the throwaway home. The listener
    # emits `AGORA_LISTEN ended reason=signal` and removes its pid/lock files.
    [ -n "$LISTEN_PID" ] && kill "$LISTEN_PID" 2>/dev/null || true
    [ -n "$HUB_PID" ] && kill "$HUB_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    rm -rf "$AGORA_HOME"
    echo "cleaned up (demo processes stopped, ${AGORA_HOME} removed)"
}
trap cleanup EXIT   # installed before the port check so the tmp home never leaks

# A plain TCP connect probe (bash /dev/tcp, no external tools). An HTTP probe
# would miss a non-HTTP occupant of the port: the hub would then fail to bind
# and the demo would wait forever for a hub that can never come up.
port_listening() { (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; }

if port_listening; then
    echo "port ${PORT} is already in use — refusing to run. Free it and retry." >&2
    exit 1
fi

wait_for() {  # wait_for <pattern> <file> <seconds> <what>
    for _ in $(seq 1 $(($3 * 5))); do
        grep -q "$1" "$2" 2>/dev/null && return 0
        sleep 0.2
    done
    echo "timed out waiting for $4 (see $2)" >&2
    exit 1
}

echo "== 1. throwaway hub on 127.0.0.1:${PORT} (AGORA_HOME=${AGORA_HOME})"
$AGORA up --port "$PORT" --db "$AGORA_HOME/hub.db" >"$AGORA_HOME/hub.log" 2>&1 &
HUB_PID=$!
# Bounded readiness wait; a dead hub (bad install, bind failure) must FAIL
# the demo with its log, not leave the next command hanging on a closed port.
hub_up=""
for _ in $(seq 1 50); do
    kill -0 "$HUB_PID" 2>/dev/null || break
    port_listening && { hub_up=1; break; }
    sleep 0.2
done
if [ -z "$hub_up" ]; then
    echo "hub failed to start on port ${PORT}; its log:" >&2
    cat "$AGORA_HOME/hub.log" >&2
    exit 1
fi

# Agents self-register on first use: `agora up` saved the demo admin key into
# the throwaway AGORA_HOME's config.json, so no key handling is needed.
$AGORA whoami --as sender   >/dev/null
$AGORA whoami --as receiver >/dev/null

echo "== 2. a message BEFORE arming: lands in the inbox, never replayed by listen"
$AGORA dm --as sender --to receiver --title "pre-arm" \
    "sent before the listener existed: check_inbox finds me, the tail does not"

echo "== 3. arm the listener (file mode tails ${AGORA_HOME}/receiver-inbox.log)"
$AGORA listen --as receiver --source file --debounce 2 \
    >"$AGORA_HOME/listen.out" 2>&1 &
LISTEN_PID=$!
wait_for "^AGORA_LISTEN armed" "$AGORA_HOME/listen.out" 10 "the armed marker"
grep "^AGORA_LISTEN armed" "$AGORA_HOME/listen.out"
echo "   (real harness: this shell must be BACKGROUND + MONITORED for ^AGORA_WAKE)"

echo "== 4. a peer posts -> one AGORA_WAKE sentinel (debounced, identifiers only)"
$AGORA dm --as sender --to receiver --status open --title "wake probe" \
    "are you awake?"
wait_for "^AGORA_WAKE " "$AGORA_HOME/listen.out" 30 "the wake sentinel"
grep "^AGORA_WAKE " "$AGORA_HOME/listen.out"

echo "== 5. the woken turn: check_inbox (fenced read), reply where owed, ack"
$AGORA inbox --as receiver
echo
echo "Done: both messages above were waiting (pre-arm one included), the wake"
echo "carried only identifiers, and content was read through the fenced inbox."

# REMOTE VARIANT (documentation only — this script never contacts a remote).
# File mode above works only on the hub's machine: it tails the notify file
# the hub writes locally. From ANY OTHER machine the same listener runs over
# the WebSocket instead — same sentinels, same ritual, plus reconnect with
# catch-up so an outage window cannot drop a wake:
#
#     export AGORA_URL=http://hub-host:8765   # the hub you were given
#     export AGORA_ADMIN_KEY=...              # once, to self-register; or have
#                                             # the operator seed ~/.agora/keys.json
#     agora listen --as receiver --source ws
#
# `--source auto` (the default) picks this for you: file when the hub is
# loopback and the notify file exists, ws otherwise.
