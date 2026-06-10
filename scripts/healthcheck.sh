#!/usr/bin/env bash
# Probe all running stack services and print a status table.
# Usage: bash scripts/healthcheck.sh
# Exits 0 if all expected services are healthy, 1 otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Load .env from repo root (sets LITELLM_PORT, WEBUI_PORT, etc.)
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ROOT_DIR/.env"
  set +a
fi

LITELLM_PORT="${LITELLM_PORT:-4000}"
WEBUI_PORT="${WEBUI_PORT:-3000}"

# ── ANSI colours ─────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; NC='\033[0m'

# ── Helpers ───────────────────────────────────────────────────
http_status() {   # url [extra curl args...]
  local url="$1"; shift
  if curl -sf --max-time 6 "$@" "$url" >/dev/null 2>&1; then
    printf "healthy"
  else
    printf "unreachable"
  fi
}

container_state() {   # container-name
  docker inspect --format='{{.State.Status}}' "$1" 2>/dev/null || printf "not_found"
}

colour_status() {   # status-string
  case "$1" in
    healthy)     printf "${GREEN}%-11s${NC}" "$1" ;;
    skipped*)    printf "${YELLOW}%-11s${NC}" "$1" ;;
    *)           printf "${RED}%-11s${NC}"   "$1" ;;
  esac
}

row() {   # service  container  endpoint  status
  printf "│ %-20s │ %-22s │ %-38s │ " "$1" "$2" "$3"
  colour_status "$4"
  printf " │\n"
}

# ── Header ────────────────────────────────────────────────────
printf "\n${BOLD}onprem-llm-stack — Service Health${NC}   %s\n\n" \
  "$(date '+%Y-%m-%d %H:%M:%S')"

hdr="┌──────────────────────┬────────────────────────┬────────────────────────────────────────┬─────────────┐"
sep="├──────────────────────┼────────────────────────┼────────────────────────────────────────┼─────────────┤"
ftr="└──────────────────────┴────────────────────────┴────────────────────────────────────────┴─────────────┘"

printf "%s\n" "$hdr"
printf "│ %-20s │ %-22s │ %-38s │ %-11s │\n" \
  "Service" "Container" "Endpoint" "Status"
printf "%s\n" "$sep"

# ── LiteLLM ──────────────────────────────────────────────────
litellm_ep="http://localhost:${LITELLM_PORT}/health/liveliness"
litellm_st=$(http_status "$litellm_ep")
row "LiteLLM Gateway" "onprem-litellm" "$litellm_ep" "$litellm_st"

# ── Open WebUI ────────────────────────────────────────────────
webui_ep="http://localhost:${WEBUI_PORT}/health"
webui_st=$(http_status "$webui_ep")
row "Open WebUI" "onprem-open-webui" "$webui_ep" "$webui_st"

# ── vLLM (only shown when container exists) ───────────────────
vllm_state=$(container_state "onprem-vllm")
if [[ "$vllm_state" == "running" ]]; then
  vllm_ep="http://localhost:8000/health"
  vllm_st=$(http_status "$vllm_ep")
  row "vLLM Inference" "onprem-vllm" "$vllm_ep" "$vllm_st"
else
  row "vLLM Inference" "onprem-vllm (not running)" "http://localhost:8000/health" "skipped/dev"
fi

printf "%s\n\n" "$ftr"

# ── Ollama on host (informational) ────────────────────────────
# Try direct port 11434 first; fall back to ollama-proxy.py on 11435 (Linux workaround)
ollama_st=$(http_status "http://localhost:11434/api/tags")
if [[ "$ollama_st" == "unreachable" ]]; then
  proxy_st=$(http_status "http://localhost:11435/api/tags")
  [[ "$proxy_st" == "healthy" ]] && ollama_st="healthy(proxy)"
fi
printf "Host Ollama:           "
colour_status "$ollama_st"
printf "\n\n"

# ── Exit code ─────────────────────────────────────────────────
all_ok=true
[[ "$litellm_st" != "healthy" ]] && { printf "${RED}ERROR${NC}: LiteLLM unreachable\n"; all_ok=false; }
[[ "$webui_st"   != "healthy" ]] && { printf "${RED}ERROR${NC}: Open WebUI unreachable\n"; all_ok=false; }
if [[ "$vllm_state" == "running" ]] && [[ "${vllm_st:-}" != "healthy" ]]; then
  printf "${RED}ERROR${NC}: vLLM running but unhealthy\n"; all_ok=false
fi

if $all_ok; then
  printf "${GREEN}All expected services are healthy.${NC}\n\n"
  exit 0
else
  exit 1
fi
