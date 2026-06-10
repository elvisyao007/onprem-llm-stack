"""
File-based virtual key authentication for LiteLLM proxy.

Replaces LiteLLM's database-backed key validation with a YAML file store.
See DECISIONS.md ADR-0004 for why we do not use LiteLLM's built-in /key/generate.

Registered in litellm_config.yaml:
    general_settings:
      custom_auth: "callbacks.key_auth.user_api_key_auth"
"""

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException, Request, status

try:
    from litellm.proxy._types import UserAPIKeyAuth
except ImportError as exc:
    raise ImportError("litellm.proxy._types not found — is this running inside LiteLLM?") from exc

_KEYS_PATH = Path(os.environ.get("VIRTUAL_KEYS_PATH", "/app/configs/virtual_keys.yaml"))
_DB_PATH   = os.environ.get("AUDIT_DB_PATH", "/app/data/audit.db")

# Cache the key definitions to avoid reading YAML on every request.
# Refreshed every _CACHE_TTL_SECONDS or whenever the file mtime changes.
_CACHE_TTL_SECONDS = 30
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {}          # {key_string: key_info_dict}
_cache_loaded_at: float = 0.0
_cache_mtime: float = 0.0


def _load_keys() -> dict[str, Any]:
    """Return {raw_key: key_info} from virtual_keys.yaml, with TTL caching."""
    global _cache, _cache_loaded_at, _cache_mtime

    now = time.monotonic()
    current_mtime = _KEYS_PATH.stat().st_mtime if _KEYS_PATH.exists() else 0.0

    with _cache_lock:
        if (
            _cache
            and now - _cache_loaded_at < _CACHE_TTL_SECONDS
            and current_mtime == _cache_mtime
        ):
            return _cache

        if not _KEYS_PATH.exists():
            _cache = {}
            _cache_loaded_at = now
            _cache_mtime = 0.0
            return _cache

        try:
            with open(_KEYS_PATH) as fh:
                config = yaml.safe_load(fh) or {}
            _cache = {k["key"]: k for k in config.get("keys", []) if "key" in k}
            _cache_loaded_at = now
            _cache_mtime = current_mtime
        except Exception as exc:
            print(f"[key_auth] failed to load {_KEYS_PATH}: {exc}")

        return _cache


def _key_label(key: str) -> str:
    """Non-reversible short identifier for logging: sk-alice...a1b2."""
    if len(key) > 12:
        return key[:8] + "..." + key[-4:]
    return key


def _get_budget_spent(key_label: str) -> float:
    """Return cumulative USD spend for this key from the audit database."""
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=5)
        row = conn.execute(
            "SELECT total_cost_usd FROM key_budgets WHERE key_label = ?",
            (key_label,),
        ).fetchone()
        conn.close()
        return float(row[0]) if row else 0.0
    except Exception:
        return 0.0


async def user_api_key_auth(request: Request, api_key: str) -> "UserAPIKeyAuth":
    """
    Custom auth handler for LiteLLM proxy.

    Handles three cases:
      - master_key  → full access, user_id="master"
      - virtual key → validate against virtual_keys.yaml, enforce models + budget
      - unknown key → HTTP 401
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Missing API key", "code": "auth_error"},
        )

    master_key = os.environ.get("LITELLM_MASTER_KEY", "")
    if api_key == master_key:
        return UserAPIKeyAuth(
            api_key=api_key,
            user_id="master",
            models=[],              # empty = all models allowed
            metadata={"key_label": "master"},
        )

    keys = _load_keys()
    key_info = keys.get(api_key)

    if key_info is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Invalid API key", "code": "auth_error"},
        )

    label = _key_label(api_key)

    # Budget enforcement
    max_budget = key_info.get("max_budget_usd")
    if max_budget is not None:
        spent = _get_budget_spent(label)
        if spent >= float(max_budget):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": f"Budget exhausted: ${spent:.4f} of ${max_budget:.2f} spent",
                    "code": "budget_exceeded",
                    "user_id": key_info.get("user_id", "unknown"),
                },
            )

    allowed_models: list[str] = key_info.get("allowed_models") or []

    return UserAPIKeyAuth(
        api_key=api_key,
        user_id=key_info.get("user_id", "unknown"),
        models=allowed_models,          # LiteLLM enforces this at the routing layer
        max_budget=float(max_budget) if max_budget is not None else None,
        metadata={
            "key_label": label,
            "rpm_limit": key_info.get("rpm"),
        },
    )
