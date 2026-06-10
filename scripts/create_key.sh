#!/usr/bin/env bash
# create_key.sh — Add a virtual key to configs/virtual_keys.yaml
#
# Usage:
#   bash scripts/create_key.sh \
#     --user alice \
#     --models "qwen3-32b,gemma4-31b" \
#     --budget 10.0 \
#     --rpm 60
#
# Output: JSON matching LiteLLM's /key/generate response shape (but file-backed,
# not database-backed — see DECISIONS.md ADR-0004 for rationale).
#
# Requires: python3 (stdlib only), configs/virtual_keys.yaml (created if absent).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
KEYS_FILE="$ROOT_DIR/configs/virtual_keys.yaml"

USER_ID=""
MODELS=""
BUDGET=""
RPM="60"

# ── Arg parsing ──────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)    USER_ID="$2";  shift 2 ;;
    --models)  MODELS="$2";   shift 2 ;;
    --budget)  BUDGET="$2";   shift 2 ;;
    --rpm)     RPM="$2";      shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$USER_ID" || -z "$MODELS" ]]; then
  echo "Usage: create_key.sh --user <name> --models <m1,m2,...> [--budget <usd>] [--rpm <n>]" >&2
  exit 1
fi

# ── Generate key ─────────────────────────────────────────────
NEW_KEY="sk-user-${USER_ID}-$(python3 -c 'import secrets; print(secrets.token_hex(16))')"

# ── Write to YAML ────────────────────────────────────────────
python3 - <<PYEOF
import sys, os, yaml
from pathlib import Path

keys_path = Path("$KEYS_FILE")
user_id   = "$USER_ID"
new_key   = "$NEW_KEY"
models    = [m.strip() for m in "$MODELS".split(",") if m.strip()]
budget    = "$BUDGET"
rpm       = "$RPM"

# Load existing or start fresh
if keys_path.exists():
    with open(keys_path) as fh:
        config = yaml.safe_load(fh) or {}
else:
    config = {}

existing_keys = config.get("keys", [])

# Check for duplicate user_id
dup = [k for k in existing_keys if k.get("user_id") == user_id]
if dup:
    print(f"Warning: user_id '{user_id}' already has {len(dup)} key(s); adding another.", file=sys.stderr)

entry = {
    "key": new_key,
    "user_id": user_id,
    "allowed_models": models,
}
if budget:
    entry["max_budget_usd"] = float(budget)
if rpm:
    entry["rpm"] = int(rpm)

existing_keys.append(entry)
config["keys"] = existing_keys

keys_path.parent.mkdir(parents=True, exist_ok=True)
with open(keys_path, "w") as fh:
    yaml.dump(config, fh, default_flow_style=False, sort_keys=False)
PYEOF

# ── Output ───────────────────────────────────────────────────
python3 - <<PYEOF
import json
from datetime import datetime, timezone

print(json.dumps({
    "key":             "$NEW_KEY",
    "user_id":         "$USER_ID",
    "models":          [m.strip() for m in "$MODELS".split(",") if m.strip()],
    "max_budget_usd":  $( [[ -n "$BUDGET" ]] && echo "$BUDGET" || echo "null" ),
    "rpm":             $RPM,
    "created_at":      datetime.now(timezone.utc).isoformat(),
    "backend":         "file",
    "keys_file":       "$KEYS_FILE",
}, indent=2))
PYEOF
