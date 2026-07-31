#!/usr/bin/env bash
# Acceptance tests for "abstractcode-tui: implement the agora harness contract".
# Every command below was executed for real during probe-e on 2026-07-30.
# Run from anywhere. Requires: uv + the agora checkout, a built abstractcode-tui,
# a python env that can `python -m abstractgateway serve`.
set -uo pipefail

SC="${SC:-/tmp/agora-harness-acceptance}"
AGORA_REPO="${AGORA_REPO:-/Users/albou/projects/agora}"
FW="${FW:-/Users/albou/tmp/abstractframework}"
TUI="${TUI:-$FW/abstractcode-tui/target/release/abstractcode-tui}"
HUB_PORT="${HUB_PORT:-8790}"
GW_PORT="${GW_PORT:-8099}"
GW_TOK="${GW_TOK:-probe-gateway-token}"
HUB="http://127.0.0.1:$HUB_PORT"
GW="http://127.0.0.1:$GW_PORT"
PROVIDER="${PROVIDER:-endpoint:airelay}"
MODEL="${MODEL:-gpt-5.4-mini}"

pass(){ echo "PASS  $*"; }
fail(){ echo "FAIL  $*"; FAILED=1; }
FAILED=0

# ---------------------------------------------------------------- SETUP: hub
mkdir -p "$SC"/{hub,keys,gwdata/config,gwflows,prefs,ws}
uv run --project "$AGORA_REPO" agora up --home "$SC/hub" --port "$HUB_PORT" --db "$SC/hub/agora.db" \
  > "$SC/hub/hub.log" 2>&1 &
sleep 8

SEAT_KEY=$(uv run --project "$AGORA_REPO" agora register seat-a --url "$HUB" --home "$SC/hub" \
           | grep -oE 'agora_[a-f0-9]+' | head -1)
SEAT_B_KEY=$(uv run --project "$AGORA_REPO" agora register seat-b --url "$HUB" --home "$SC/hub" \
           | grep -oE 'agora_[a-f0-9]+' | head -1)
ASKER_KEY=$(uv run --project "$AGORA_REPO" agora register asker --url "$HUB" --home "$SC/hub" \
           | grep -oE 'agora_[a-f0-9]+' | head -1)
for pair in "seat-a:$SEAT_KEY" "seat-b:$SEAT_B_KEY" "asker:$ASKER_KEY"; do
  uv run --project "$AGORA_REPO" agora seed-key "${pair%%:*}" --url "$HUB" --key "${pair##*:}" --home "$SC/keys" >/dev/null
done

uv run --project "$AGORA_REPO" agora create-channel room --as asker --url "$HUB" --home "$SC/keys" >/dev/null
uv run --project "$AGORA_REPO" agora add room seat-a seat-b --as asker --url "$HUB" --home "$SC/keys" --why test >/dev/null
INV=$(uv run --project "$AGORA_REPO" agora inbox --as seat-a --url "$HUB" --home "$SC/keys" 2>/dev/null \
      | grep -oE "invite_[a-f0-9]{32,}" | head -1)
uv run --project "$AGORA_REPO" agora join --as seat-a --channel room --invite "$INV" --url "$HUB" --home "$SC/keys" >/dev/null
INVB=$(uv run --project "$AGORA_REPO" agora inbox --as seat-b --url "$HUB" --home "$SC/keys" 2>/dev/null \
      | grep -oE "invite_[a-f0-9]{32,}" | head -1)
uv run --project "$AGORA_REPO" agora join --as seat-b --channel room --invite "$INVB" --url "$HUB" --home "$SC/keys" >/dev/null

# ------------------------------------------------------------ SETUP: gateway
cp "$FW/runtime/config/provider_endpoint_profiles.json" "$SC/gwdata/config/" 2>/dev/null
cp "$FW/runtime/config/abstractcore.json"               "$SC/gwdata/config/" 2>/dev/null
cp "$FW/abstractgateway/flows/bundles/basic-agent@0.0.3.flow" "$SC/gwflows/"

# Author a seat bundle: basic-agent with the agora toolset pinned in the flow.
python3 - "$SC" <<'PY'
import json, os, shutil, sys, zipfile
SC = sys.argv[1]
work = os.path.join(SC, "bundle"); shutil.rmtree(work, ignore_errors=True); os.makedirs(work)
with zipfile.ZipFile(os.path.join(SC, "gwflows", "basic-agent@0.0.3.flow")) as z: z.extractall(work)
m = json.load(open(f"{work}/manifest.json")); m["bundle_id"]="agora-seat"; m["bundle_version"]="0.1.0"
json.dump(m, open(f"{work}/manifest.json","w"), indent=2)
fp = f"{work}/flows/81795ea9.json"; d = json.load(open(fp))
TOOLS = ["agora_whoami","agora_check_inbox","agora_ack_inbox","agora_read_channel",
         "agora_read_message","agora_post_message","agora_send_dm",
         "channel_fs_list","channel_fs_read","channel_fs_write",
         "channel_store_get","channel_store_set","read_file","write_file","list_files"]
for n in d["nodes"]:
    if n["type"] == "on_flow_start":
        n["data"].setdefault("pinDefaults", {})["tools"] = TOOLS
json.dump(d, open(fp,"w"), indent=1)
with zipfile.ZipFile(f"{SC}/gwflows/agora-seat@0.1.0.flow","w",zipfile.ZIP_DEFLATED) as z:
    z.write(f"{work}/manifest.json","manifest.json")
    for f in sorted(os.listdir(f"{work}/flows")): z.write(f"{work}/flows/{f}", f"flows/{f}")
print("authored agora-seat@0.1.0")
PY

start_gateway() {  # $1 = "global" | "alias"
  pkill -f "abstractgateway serve --host 127.0.0.1 --port $GW_PORT" 2>/dev/null; sleep 3
  export PYTHONPATH="$FW/abstractgateway/src:$FW/abstractruntime/src:$FW/abstractmemory/src:$FW/abstractflow/src:$FW/abstractcamera/src:$FW/abstractagent/src:$FW/abstractcore"
  export ABSTRACTGATEWAY_DATA_DIR="$SC/gwdata" ABSTRACTGATEWAY_FLOWS_DIR="$SC/gwflows"
  export ABSTRACTGATEWAY_AUTH_TOKEN="$GW_TOK" ABSTRACT_ENABLE_AGORA_TOOLS=1
  unset AGORA_API_KEY AGORA_URL
  export AGORA_API_KEY__SEAT_A="$SEAT_KEY"   AGORA_URL__SEAT_A="$HUB"
  export AGORA_API_KEY__SEAT_B="$SEAT_B_KEY" AGORA_URL__SEAT_B="$HUB"
  if [ "$1" = "global" ]; then export AGORA_API_KEY="$SEAT_KEY" AGORA_URL="$HUB"; fi
  ( "$FW/.venv/bin/python" -P -m abstractgateway serve --host 127.0.0.1 --port "$GW_PORT" \
      > "$SC/gw.log" 2>&1 & ) ; sleep 15
}

echo '{}' > "$SC/prefs/prefs.json"
run_exec() {  # $1 = prompt, rest = extra flags
  local prompt="$1"; shift
  ( cd "$SC/ws" && ABSTRACTCODE_GATEWAY_URL="$GW" ABSTRACTCODE_GATEWAY_TOKEN="$GW_TOK" \
    ABSTRACTCODE_TUI_PREFS_FILE="$SC/prefs/prefs.json" \
    "$TUI" exec "$prompt" --workflow agora-seat:81795ea9 \
      --provider "$PROVIDER" --model "$MODEL" --permissions all --timeout 300 "$@" )
}

################################################################################
# AC-0  BASELINE (must pass BEFORE the fix — it already does today)
#       Headless single turn + gateway-side toolset + a real hub turn,
#       with the gateway holding ONE process-global identity.
################################################################################
start_gateway global

curl -s -H "Authorization: Bearer $GW_TOK" "$GW/api/gateway/discovery/tools" \
  | python3 -c 'import json,sys;i=json.load(sys.stdin)["items"];a=[t for t in i if t["name"].startswith("agora_")];print("AGORA_ENABLED",all(t["enabled"] for t in a),len(a))' \
  | grep -q "AGORA_ENABLED True 7" && pass "AC-0a agora toolset enabled on the gateway" \
                                   || fail "AC-0a agora toolset NOT enabled"

uv run --project "$AGORA_REPO" agora post --channel room "What is 6 times 7? Answer here." \
  --as asker --url "$HUB" --home "$SC/keys" --status open --to seat-a >/dev/null
run_exec "Call agora_check_inbox, answer the open question in channel room with agora_post_message, then agora_ack_inbox." \
  --max-iterations 8 > "$SC/ac0.out" 2>"$SC/ac0.err"
echo "  exit=$?"
grep -q "✓ agora_post_message" "$SC/ac0.out" && pass "AC-0b headless turn called agora_post_message" \
                                             || fail "AC-0b no agora_post_message in output"
uv run --project "$AGORA_REPO" agora history --channel room --as asker --url "$HUB" --home "$SC/keys" \
  | grep -q "from: seat-a" && pass "AC-0c the hub has a real message from seat-a" \
                           || fail "AC-0c nothing from seat-a on the hub"

################################################################################
# AC-1  SEAT IDENTITY (the ask). Gateway holds ONLY per-alias keys.
#       Requires: (a) abstractruntime carries _runtime.agora_agent across the
#       agent sub-run hop, (b) abstractcode-tui exec can write it.
################################################################################
start_gateway alias

run_exec "Call agora_whoami once and report the exact id it returns." \
  --agora-agent seat-b --max-iterations 3 > "$SC/ac1.out" 2>"$SC/ac1.err"
grep -q "seat-b" "$SC/ac1.out" && pass "AC-1a --agora-agent seat-b -> agora_whoami returns seat-b" \
                               || fail "AC-1a whoami did not return seat-b (see $SC/ac1.out)"

run_exec "Call agora_whoami once and report the exact id it returns." \
  --agora-agent seat-a --max-iterations 3 > "$SC/ac1b.out" 2>&1
grep -q "seat-a" "$SC/ac1b.out" && pass "AC-1b same binary, same gateway, different seat" \
                                || fail "AC-1b second alias did not resolve"

# Negative: an alias with no key in the gateway env must fail LOUD, never
# silently fall back to a global identity.
run_exec "Call agora_whoami once." --agora-agent nobody-here --max-iterations 2 > "$SC/ac1c.out" 2>&1
grep -qi "AGORA_API_KEY__NOBODY_HERE\|not set\|no key" "$SC/ac1c.out" \
  && pass "AC-1c unknown alias fails loud" || fail "AC-1c unknown alias did not fail loud"

# Negative: a non-slug alias must be rejected AT PARSE (exit 2, no run started).
run_exec "x" --agora-agent "Not A Slug" > "$SC/ac1d.out" 2>&1; rc=$?
[ "$rc" = "2" ] && ! grep -q "^run " "$SC/ac1d.out" \
  && pass "AC-1d bad alias rejected at parse (exit 2, no run)" || fail "AC-1d bad alias not rejected at parse (exit $rc)"

################################################################################
# AC-2  PER-INVOCATION TOOLSET (--tools)
################################################################################
run_exec "List the exact names of every tool you have. Output only a comma-separated list." \
  --tools agora_whoami,read_file --max-iterations 2 > "$SC/ac2.out" 2>&1
grep -q "agora_whoami" "$SC/ac2.out" && ! grep -q "agora_post_message" "$SC/ac2.out" \
  && pass "AC-2a --tools narrows the flow pin" || fail "AC-2a --tools did not take effect"

# A name no toolset provides must not vanish in silence.
run_exec "Say OK." --tools read_file,definitely_not_a_real_tool --max-iterations 2 > "$SC/ac2b.out" 2>&1
grep -qi "definitely_not_a_real_tool" "$SC/ac2b.out" \
  && pass "AC-2b unresolvable tool name is surfaced" || fail "AC-2b unresolvable tool name dropped in silence"

################################################################################
# AC-3  MACHINE-READABLE EVIDENCE (--json)
################################################################################
run_exec "Call agora_whoami once." --agora-agent seat-a --json --max-iterations 3 > "$SC/ac3.ndjson" 2>&1
python3 - "$SC/ac3.ndjson" <<'PY'
import json,sys
rows=[]
for line in open(sys.argv[1]):
    line=line.strip()
    if not line: continue
    try: rows.append(json.loads(line))
    except json.JSONDecodeError: print("FAIL  AC-3a non-JSON line on stdout:",line[:80]); sys.exit(1)
tools=[r for r in rows if r.get("type")=="tool"]
assert tools, "no tool rows"
assert all({"name","status","success"} <= set(t) for t in tools), "tool row missing name/status/success"
term=[r for r in rows if r.get("type")=="result"]
assert term and {"status","llm_calls","tool_calls"} <= set(term[-1]), "no terminal summary row"
assert any(t["name"]=="agora_whoami" and t["success"] for t in tools), "agora_whoami not proven successful"
print("PASS  AC-3a stdout is NDJSON with per-tool {name,status,success}")
print("PASS  AC-3b terminal row carries {status,llm_calls,tool_calls}")
PY

################################################################################
# AC-4  NO AMBIENT WORKFLOW / BYTE-PARITY
################################################################################
printf '{"bundle_id":"basic-agent","flow_id":"81795ea9"}' > "$SC/prefs/ambient.json"
( cd "$SC/ws" && ABSTRACTCODE_GATEWAY_URL="$GW" ABSTRACTCODE_GATEWAY_TOKEN="$GW_TOK" \
  ABSTRACTCODE_TUI_PREFS_FILE="$SC/prefs/ambient.json" \
  "$TUI" exec "Say OK." --provider "$PROVIDER" --model "$MODEL" --timeout 90 ) 2>&1 \
  | grep -q "workflow basic-agent" \
  && pass "AC-4a (known hazard) no --workflow silently inherits prefs.json" \
  || fail "AC-4a ambient selection changed unexpectedly"

# Byte-parity: with none of the new flags, input_data must be what it is today.
( cd "$SC/ws" && ABSTRACTCODE_GATEWAY_URL="$GW" ABSTRACTCODE_GATEWAY_TOKEN="$GW_TOK" \
  ABSTRACTCODE_TUI_PREFS_FILE="$SC/prefs/prefs.json" \
  "$TUI" exec "Say OK." --workflow agora-seat:81795ea9 --provider "$PROVIDER" --model "$MODEL" \
  --timeout 90 ) 2>&1 | tee "$SC/ac4.out" | grep -oE 'run [0-9a-f-]{36}' | head -1 | awk '{print $2}' > "$SC/ac4.rid"
python3 - "$SC" "$GW" "$GW_TOK" <<'PY'
import json,sys,urllib.request
SC,GW,TOK=sys.argv[1],sys.argv[2],sys.argv[3]
rid=open(f"{SC}/ac4.rid").read().strip()
d=json.load(open(f"{SC}/gwdata/run_{rid}.json"))
rt=(d.get("vars") or {}).get("_runtime") or {}
assert "agora_agent" not in rt, "agora_agent leaked into a run that did not ask for it"
print("PASS  AC-4b no flags -> no agora_agent in run vars (byte-parity)")
PY

echo
[ "$FAILED" = "0" ] && echo "ALL ACCEPTANCE CRITERIA PASS" || echo "SOME CRITERIA FAILED"
echo "artifacts in $SC"
