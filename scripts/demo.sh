#!/usr/bin/env bash
# Hackathon demo driver: clean state, inject the same chaos the user does
# in the FastAPI Swagger UI at http://127.0.0.1:8003/docs, and wait for the
# swarm observer to detect it.
#
# Usage:
#   scripts/demo.sh                 # default: error_rate=0.4 latency=350 on payments
#   scripts/demo.sh inventory       # target a different service
#   scripts/demo.sh payments 0.6 500
#
# Service short-names: orders | inventory | payments | notifications
set -euo pipefail

SERVICE="${1:-payments}"
RATE="${2:-0.4}"
LATENCY_MS="${3:-350}"

port_for() {
  case "$1" in
    orders)        echo 8001 ;;
    inventory)     echo 8002 ;;
    payments)      echo 8003 ;;
    notifications) echo 8004 ;;
    *)             echo "" ;;
  esac
}
ALL_SERVICES="orders inventory payments notifications"

PORT="$(port_for "$SERVICE")"
if [[ -z "$PORT" ]]; then
  echo "unknown service: $SERVICE (expected: $ALL_SERVICES)" >&2
  exit 2
fi
BASE="http://127.0.0.1:${PORT}"
SWARM="http://127.0.0.1:8000"

bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
dim()   { printf "\033[2m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }

bold "== sre-swarm demo =="
dim  "service:   ${SERVICE} (${BASE})"
dim  "error_rate: ${RATE}"
dim  "latency_ms: ${LATENCY_MS}"
echo

# --- pre-flight ---
bold "[1/5] pre-flight"
if ! curl -sf "${SWARM}/api/integrations" >/dev/null; then
  echo "  swarm not running at ${SWARM} — start it with:" >&2
  echo "    PYTHONPATH=src ./.venv/bin/python -m sre_swarm.cli web --host 127.0.0.1 --port 8000" >&2
  exit 1
fi
green "  swarm OK"
if ! curl -sf "${BASE}/health" >/dev/null; then
  echo "  demo service not running at ${BASE}" >&2
  exit 1
fi
green "  ${SERVICE} OK"
echo

# --- clear stale chaos so the demo starts from a clean baseline ---
bold "[2/5] reset chaos on all services"
for svc in $ALL_SERVICES; do
  p="$(port_for "$svc")"
  if curl -sf "http://127.0.0.1:${p}/health" >/dev/null 2>&1; then
    curl -s -X POST "http://127.0.0.1:${p}/chaos/clear" >/dev/null || true
    dim "  cleared ${svc}"
  fi
done
echo

# --- inject — identical to clicking "Try it out" → Execute in Swagger UI ---
bold "[3/5] inject chaos via ${BASE}/docs API"
echo "  POST ${BASE}/chaos/error_rate?value=${RATE}"
curl -s -X POST "${BASE}/chaos/error_rate?value=${RATE}" | sed 's/^/    /'
echo "  POST ${BASE}/chaos/latency?ms=${LATENCY_MS}"
curl -s -X POST "${BASE}/chaos/latency?ms=${LATENCY_MS}" | sed 's/^/    /'
echo

# --- confirm injection is live ---
bold "[4/5] confirm chaos is live"
curl -s "${BASE}/health" | sed 's/^/  /'
echo

# --- now sit back and watch ---
bold "[5/5] observer is polling — open ${SWARM} in the browser"
dim "  the observer needs 2 consecutive bad polls (~20-30s) before paging."
dim "  an incident card will appear in the sidebar inbox — click 'Investigate'"
dim "  to focus it, then watch triage → rca → predict → chaos_replay → heal stream in."
echo

bold "== done =="
echo "next steps:"
echo "  • watch the pipeline run in the browser"
echo "  • approve the rca_confirm gate, then the heal plan"
echo "  • when you're done recording, clear chaos with:"
echo "      curl -s -X POST ${BASE}/chaos/clear"
