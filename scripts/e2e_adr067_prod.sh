#!/usr/bin/env bash
# ADR-067 production E2E — run inside api container or on server with docker compose exec.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/claude-hermes/.env}"
USER_ID="${USER_ID:-5c619e3b-18ae-5349-8723-3ddf9ad246ca}"
API_KEY="$(grep '^CLIENT_API_KEY=' "$ENV_FILE" | cut -d= -f2-)"
ADMIN="$(grep '^ADMIN_API_SECRET=' "$ENV_FILE" | cut -d= -f2-)"
DEVICE="e2e-adr067-$(date +%s)"
BASE="${BASE:-http://127.0.0.1:8000}"

curl_api() {
  if [[ "${RUN_INSIDE_API:-}" == "1" ]]; then
    curl -sS "$@"
  else
    docker compose -f /opt/claude-hermes/docker-compose.prod.yml \
      -f /opt/claude-hermes/docker-compose.avorelio.yml \
      --env-file "$ENV_FILE" exec -T api curl -sS "$@"
  fi
}

echo "USER_ID=$USER_ID BASE=$BASE"
echo "=== Grant credits ==="
curl_api -X POST "$BASE/v1/admin/subscription/grant" \
  -H "X-Admin-Token: $ADMIN" \
  -H 'Content-Type: application/json' \
  -d "{\"userId\":\"$USER_ID\",\"plan\":\"pro\",\"expiresAt\":\"2026-12-31T00:00:00Z\",\"grantCredits\":true,\"idempotencyKey\":\"grant-$DEVICE\",\"reason\":\"ADR-067 E2E\"}"
echo

echo "=== Start run (no SSE) ==="
RUN_BODY="$(curl_api -w $'\nHTTP_CODE:%{http_code}' -X POST "$BASE/v1/agent/run" \
  -H "X-API-Key: $API_KEY" \
  -H "X-User-Id: $USER_ID" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Reply with exactly the word E2E_OK and nothing else.","model":"gpt-5-mini"}')"
HTTP_CODE="$(echo "$RUN_BODY" | tail -1 | cut -d: -f2)"
RUN_JSON="$(echo "$RUN_BODY" | sed '$d')"
echo "HTTP $HTTP_CODE"
echo "$RUN_JSON"
[[ "$HTTP_CODE" == "202" ]] || { echo "FAIL: expected 202"; exit 1; }
RUN_ID="$(echo "$RUN_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin)["runId"])')"
echo "RUN_ID=$RUN_ID"

echo "=== Poll /state ==="
FINAL_STATE=""
STATUS=""
for i in $(seq 1 36); do
  STATE="$(curl_api "$BASE/v1/agent/runs/$RUN_ID/state" \
    -H "X-API-Key: $API_KEY" -H "X-User-Id: $USER_ID")"
  SUMMARY="$(echo "$STATE" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["status"], len(d.get("resultText","")), d.get("usage"))')"
  echo "poll $i: $SUMMARY"
  STATUS="$(echo "$STATE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')"
  if [[ "$STATUS" == "completed" || "$STATUS" == "failed" || "$STATUS" == "stopped" ]]; then
    FINAL_STATE="$STATE"
    break
  fi
  sleep 5
done

[[ -n "$FINAL_STATE" ]] || { echo "TIMEOUT"; exit 1; }
echo "$FINAL_STATE" | python3 -m json.tool

echo "=== Wallet ==="
curl_api "$BASE/v1/admin/wallet/$USER_ID" -H "X-Admin-Token: $ADMIN" | python3 -m json.tool | head -20

echo "=== Events replay ==="
EVENTS="$(curl_api -N --max-time 15 "$BASE/v1/agent/runs/$RUN_ID/events" \
  -H "X-API-Key: $API_KEY" -H "X-User-Id: $USER_ID" | head -c 2048)"
echo "$EVENTS"
EVENT_LINES="$(echo "$EVENTS" | grep -cE '^(data:|event:)' || true)"
echo "sse lines in sample: $EVENT_LINES"

RESULT_TEXT="$(echo "$FINAL_STATE" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("resultText",""))')"
if [[ "$STATUS" == "completed" && -n "$RESULT_TEXT" ]]; then
  echo "PASS: completed, resultText len=${#RESULT_TEXT}"
else
  echo "FAIL: status=$STATUS resultText len=${#RESULT_TEXT}"
  exit 1
fi
[[ "$EVENT_LINES" -gt 0 ]] || { echo "FAIL: no replay events"; exit 1; }
echo "=== E2E OK ==="