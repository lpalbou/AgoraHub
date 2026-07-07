#!/bin/sh
# Fake harness: stands in for `codex exec resume --last "$(cat)"` or
# `claude -p --resume <session> "$(cat)"` in the attache demo.
#
# The attache invokes this command when messages arrive for an idle agent.
# It hands over the rendered message digest two equivalent ways:
#   - on STDIN (what the resume CLIs consume via "$(cat)")
#   - in the file named by $AGORA_DIGEST_FILE
# plus $AGORA_CHANNELS and $AGORA_COUNT for shell-level routing.
#
# A real harness would start/resume an agent turn with the digest as the user
# message; this script just proves the wake fired by appending everything to
# a log file (default /tmp/agora_c4_harness_log.txt, override with $HARNESS_LOG).

LOG="${HARNESS_LOG:-/tmp/agora_c4_harness_log.txt}"

{
  echo "===== HARNESS WOKEN $(date '+%H:%M:%S') ====="
  echo "channels=$AGORA_CHANNELS count=$AGORA_COUNT digest_file=$AGORA_DIGEST_FILE"
  echo "--- digest (from stdin) ---"
  cat                                   # the digest, exactly as a resume CLI would see it
  echo "===== END WAKE ====="
} >> "$LOG"
