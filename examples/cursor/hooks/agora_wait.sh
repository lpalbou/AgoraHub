#!/bin/bash
# agora `stop`-hook attache for a Cursor IDE tab.
#
# Fires when this tab finishes a turn. Long-polls the agora inbox (<=55s). If a
# message is waiting, it returns a `followup_message` that re-prompts THIS tab
# to handle it, then the stop hook fires again and we wait once more — a
# self-sustaining loop that needs no human relay. If nothing arrives within the
# poll window, we return empty (no follow-up) and the tab goes quiet until the
# user speaks or the next `stop`.
#
# Requires: curl, jq. Set AGORA_URL / AGORA_API_KEY (same values as this
# workspace's .cursor/mcp.json, e.g. sourced from .cursor/agora.env).
set -euo pipefail

: "${AGORA_URL:=http://127.0.0.1:8765}"
: "${AGORA_API_KEY:?set AGORA_API_KEY for this agent}"

# Long-poll for unread envelopes (<=50s to stay under the hook timeout).
unread=$(curl -s -m 60 \
  -H "Authorization: Bearer ${AGORA_API_KEY}" \
  "${AGORA_URL}/inbox?wait=50" || echo '[]')

count=$(echo "$unread" | jq 'length' 2>/dev/null || echo 0)

if [ "$count" -gt 0 ]; then
  # Re-prompt this tab. Keep it short: the agent will use its MCP tools
  # (check_inbox / read_message) to actually read and act. This just wakes it.
  jq -n --arg n "$count" '{
    followup_message: ("You have \($n) unread agora message(s). Call check_inbox, "
      + "triage them, read (read_message) what warrants it, act, reply where a "
      + "reply is owed (status open/blocked), then ack_inbox. When done, stop — "
      + "this hook will wait for the next message.")
  }'
else
  # Nothing waiting: no follow-up, let the tab rest.
  echo '{}'
fi
