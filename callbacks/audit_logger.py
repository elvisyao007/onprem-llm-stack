"""
SQLite audit logger for LiteLLM proxy.

Writes one record per completed LLM request to data/audit.db.
See DECISIONS.md ADR-0004 for the "no prompt/response content by default" rationale.

Registered in litellm_config.yaml:
    litellm_settings:
      callbacks: ["callbacks.audit_logger.sqlite_audit_logger"]

Environment variables:
    AUDIT_DB_PATH         path to SQLite file  (default: /app/data/audit.db)
    AUDIT_LOG_CONTENT     set "true" to log truncated prompt/response text
                          default: false (privacy-minimization default)
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from litellm.integrations.custom_logger import CustomLogger

_DB_PATH       = os.environ.get("AUDIT_DB_PATH", "/app/data/audit.db")
_LOG_CONTENT   = os.environ.get("AUDIT_LOG_CONTENT", "false").lower() == "true"
_CONTENT_TRUNC = 500        # chars; only relevant when AUDIT_LOG_CONTENT=true

# Notional cost rates for self-hosted models (no real billing, used for budget tracking).
# Update if you want tighter budget alignment with a particular model's commercial rate.
_COST_PER_1K_INPUT_USD  = 0.0005
_COST_PER_1K_OUTPUT_USD = 0.0010

_DDL = """
CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    user_id         TEXT    NOT NULL DEFAULT 'unknown',
    key_label       TEXT    NOT NULL DEFAULT 'unknown',
    model           TEXT    NOT NULL DEFAULT 'unknown',
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    latency_ms      REAL    NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL,          -- 'success' | 'failure'
    error_type      TEXT,                      -- null on success
    content_prompt  TEXT,                      -- null unless AUDIT_LOG_CONTENT=true
    content_response TEXT                      -- null unless AUDIT_LOG_CONTENT=true
);
CREATE INDEX IF NOT EXISTS idx_req_user ON requests(user_id);
CREATE INDEX IF NOT EXISTS idx_req_ts   ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_req_key  ON requests(key_label);

CREATE TABLE IF NOT EXISTS key_budgets (
    key_label           TEXT PRIMARY KEY,
    user_id             TEXT    NOT NULL DEFAULT 'unknown',
    total_tokens_in     INTEGER NOT NULL DEFAULT 0,
    total_tokens_out    INTEGER NOT NULL DEFAULT 0,
    total_cost_usd      REAL    NOT NULL DEFAULT 0.0,
    request_count       INTEGER NOT NULL DEFAULT 0,
    failure_count       INTEGER NOT NULL DEFAULT 0,
    last_updated        TEXT
);
"""


class SQLiteAuditLogger(CustomLogger):
    """Async CustomLogger that appends every request outcome to a local SQLite file."""

    def __init__(self, db_path: str = _DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()
        super().__init__()

    # ── DB setup ──────────────────────────────────────────────

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.executescript(_DDL)
        conn.commit()
        conn.close()

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _extract_user_info(kwargs: dict) -> tuple[str, str]:
        """Return (user_id, key_label) from callback kwargs."""
        lp   = kwargs.get("litellm_params") or {}
        meta = lp.get("metadata") or {}
        # LiteLLM populates these from UserAPIKeyAuth after custom_auth runs
        user_id   = meta.get("user_api_key_user_id") or kwargs.get("user") or "unknown"
        key_meta  = meta.get("user_api_key_metadata") or {}
        key_label = key_meta.get("key_label") or meta.get("user_api_key") or "unknown"
        return str(user_id), str(key_label)

    @staticmethod
    def _extract_usage(response_obj: Any) -> tuple[int, int]:
        """Return (prompt_tokens, completion_tokens) from a response object."""
        if response_obj is None:
            return 0, 0
        usage = getattr(response_obj, "usage", None)
        if usage:
            return (getattr(usage, "prompt_tokens", 0) or 0,
                    getattr(usage, "completion_tokens", 0) or 0)
        if isinstance(response_obj, dict):
            u = response_obj.get("usage", {}) or {}
            return u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        return 0, 0

    def _write(
        self,
        ts: str,
        user_id: str,
        key_label: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        latency_ms: float,
        status: str,
        error_type: Optional[str],
        content_prompt: Optional[str],
        content_response: Optional[str],
        cost_usd: float,
        is_failure: bool,
    ) -> None:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=10)
            try:
                conn.execute(
                    """
                    INSERT INTO requests
                      (ts, user_id, key_label, model, tokens_in, tokens_out,
                       latency_ms, status, error_type, content_prompt, content_response)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (ts, user_id, key_label, model, tokens_in, tokens_out,
                     latency_ms, status, error_type, content_prompt, content_response),
                )
                conn.execute(
                    """
                    INSERT INTO key_budgets
                      (key_label, user_id, total_tokens_in, total_tokens_out,
                       total_cost_usd, request_count, failure_count, last_updated)
                    VALUES (?,?,?,?,?,1,?,?)
                    ON CONFLICT(key_label) DO UPDATE SET
                      total_tokens_in  = total_tokens_in  + excluded.total_tokens_in,
                      total_tokens_out = total_tokens_out + excluded.total_tokens_out,
                      total_cost_usd   = total_cost_usd   + excluded.total_cost_usd,
                      request_count    = request_count    + 1,
                      failure_count    = failure_count    + excluded.failure_count,
                      last_updated     = excluded.last_updated
                    """,
                    (key_label, user_id, tokens_in, tokens_out,
                     cost_usd, 1 if is_failure else 0, ts),
                )
                conn.commit()
            except Exception as exc:
                print(f"[audit] SQLite write error: {exc}")
            finally:
                conn.close()

    # ── LiteLLM callback entry points ─────────────────────────

    async def async_log_success_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: datetime,
        end_time: datetime,
    ) -> None:
        try:
            user_id, key_label = self._extract_user_info(kwargs)
            model              = kwargs.get("model", "unknown")
            tokens_in, tokens_out = self._extract_usage(response_obj)
            latency_ms = (end_time - start_time).total_seconds() * 1000
            cost_usd   = (tokens_in  / 1000 * _COST_PER_1K_INPUT_USD +
                          tokens_out / 1000 * _COST_PER_1K_OUTPUT_USD)
            ts = start_time.astimezone(timezone.utc).isoformat()

            prompt_text = None
            response_text = None
            if _LOG_CONTENT:
                msgs = kwargs.get("messages") or []
                prompt_text = str(msgs)[:_CONTENT_TRUNC] if msgs else None
                try:
                    response_text = str(response_obj.choices[0].message.content)[:_CONTENT_TRUNC]
                except Exception:
                    pass

            self._write(ts, user_id, key_label, model, tokens_in, tokens_out,
                        latency_ms, "success", None,
                        prompt_text, response_text, cost_usd, False)
        except Exception as exc:
            print(f"[audit] success handler error: {exc}")

    async def async_log_failure_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: datetime,
        end_time: datetime,
    ) -> None:
        try:
            user_id, key_label = self._extract_user_info(kwargs)
            model              = kwargs.get("model", "unknown")
            latency_ms = (end_time - start_time).total_seconds() * 1000
            ts = start_time.astimezone(timezone.utc).isoformat()
            error_type = type(kwargs.get("exception", Exception())).__name__

            self._write(ts, user_id, key_label, model, 0, 0,
                        latency_ms, "failure", error_type,
                        None, None, 0.0, True)
        except Exception as exc:
            print(f"[audit] failure handler error: {exc}")


# Module-level singleton loaded by LiteLLM via:
#   callbacks: ["callbacks.audit_logger.sqlite_audit_logger"]
sqlite_audit_logger = SQLiteAuditLogger()
