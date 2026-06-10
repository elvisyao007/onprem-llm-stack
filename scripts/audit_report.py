#!/usr/bin/env python3
"""
audit_report.py — One-page audit summary from data/audit.db

Usage:
    python3 scripts/audit_report.py
    python3 scripts/audit_report.py --since 2026-06-01
    python3 scripts/audit_report.py --since "2026-06-01 12:00:00"
    python3 scripts/audit_report.py --db /custom/path/audit.db

Output: per-user aggregated table of requests / tokens / cost / latency / failures.
Stdlib only — no third-party dependencies.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "audit.db"


# ── ANSI helpers ─────────────────────────────────────────────

def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m"

def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m"

def _colour(s: str, ok: bool) -> str:
    code = "32" if ok else "31"
    return f"\033[{code}m{s}\033[0m"


# ── Query ─────────────────────────────────────────────────────

def _query(db_path: str, since: str | None) -> list[dict]:
    """Return per-user aggregates from the requests table."""
    if not Path(db_path).exists():
        return []

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row

    where = "WHERE ts >= ?" if since else ""
    params = [since] if since else []

    rows = conn.execute(
        f"""
        SELECT
            user_id,
            COUNT(*)                                        AS req_count,
            SUM(CASE WHEN status = 'failure' THEN 1 ELSE 0 END) AS fail_count,
            SUM(tokens_in)                                  AS total_tokens_in,
            SUM(tokens_out)                                 AS total_tokens_out,
            SUM(tokens_in + tokens_out)                     AS total_tokens,
            ROUND(AVG(latency_ms), 0)                       AS avg_latency_ms,
            ROUND(SUM(CASE WHEN status = 'success' THEN latency_ms END)
                  / NULLIF(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), 0), 0)
                                                            AS avg_ok_latency_ms,
            MAX(ts)                                         AS last_seen
        FROM requests
        {where}
        GROUP BY user_id
        ORDER BY req_count DESC
        """,
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _recent_requests(db_path: str, since: str | None, limit: int = 8) -> list[dict]:
    """Return the most recent individual requests."""
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    where = "WHERE ts >= ?" if since else ""
    params = [since] if since else []
    rows = conn.execute(
        f"""
        SELECT ts, user_id, model, tokens_in, tokens_out, latency_ms, status
        FROM requests {where}
        ORDER BY ts DESC LIMIT ?
        """,
        params + [limit],
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Formatting ────────────────────────────────────────────────

def _fmt_int(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n:,}"


def _fmt_ms(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms >= 1000:
        return f"{ms/1000:.1f}s"
    return f"{ms:.0f}ms"


def _fmt_pct(fail: int, total: int) -> str:
    if total == 0:
        return "—"
    pct = fail / total * 100
    s = f"{pct:.1f}%"
    return _colour(s, pct < 5.0)


def _table(rows: list[dict], since: str | None, db_path: str) -> None:
    COL = [
        ("User",         "user_id",          "%-18s"),
        ("Requests",     "req_count",         "%9s"),
        ("Fail rate",    "_fail_rate",         "%10s"),
        ("Tokens in",    "total_tokens_in",   "%11s"),
        ("Tokens out",   "total_tokens_out",  "%12s"),
        ("Avg latency",  "avg_ok_latency_ms", "%12s"),
        ("Last seen",    "last_seen",          "%-24s"),
    ]

    # Header
    hdr = "  ".join(fmt % name for name, _, fmt in COL)
    sep = "  ".join("-" * (len(fmt % "")) for _, _, fmt in COL)

    print()
    print(_bold("onprem-llm-stack — Audit Report"))
    if since:
        print(_dim(f"Filter: ts >= {since}"))
    print(_dim(f"Database: {db_path}"))
    print()
    print(_bold(hdr))
    print(sep)

    for r in rows:
        fail_rate = _fmt_pct(r["fail_count"], r["req_count"])
        vals = {
            "user_id":           r["user_id"],
            "req_count":         _fmt_int(r["req_count"]),
            "_fail_rate":         fail_rate,
            "total_tokens_in":   _fmt_int(r["total_tokens_in"]),
            "total_tokens_out":  _fmt_int(r["total_tokens_out"]),
            "avg_ok_latency_ms": _fmt_ms(r["avg_ok_latency_ms"]),
            "last_seen":         (r["last_seen"] or "")[:19],
        }
        print("  ".join(fmt % vals[key] for _, key, fmt in COL))

    print(sep)
    totals = {
        "user_id":           f"TOTAL ({len(rows)} user{'s' if len(rows) != 1 else ''})",
        "req_count":         _fmt_int(sum(r["req_count"] for r in rows)),
        "_fail_rate":         _fmt_pct(
            sum(r["fail_count"] for r in rows),
            sum(r["req_count"] for r in rows),
        ),
        "total_tokens_in":   _fmt_int(sum(r["total_tokens_in"] or 0 for r in rows)),
        "total_tokens_out":  _fmt_int(sum(r["total_tokens_out"] or 0 for r in rows)),
        "avg_ok_latency_ms": "—",
        "last_seen":          "—",
    }
    print(_bold("  ".join(fmt % totals[key] for _, key, fmt in COL)))
    print()


def _recent_table(rows: list[dict]) -> None:
    if not rows:
        return
    print(_bold("Recent requests (newest first)"))
    print(f"  {'Timestamp':<24} {'User':<12} {'Model':<18} {'In':>6} {'Out':>6} {'Latency':>9}  Status")
    print(f"  {'-'*24} {'-'*12} {'-'*18} {'-'*6} {'-'*6} {'-'*9}  ------")
    for r in rows:
        status_str = _colour("success", r["status"] == "success") if r["status"] == "success" \
            else _colour("failure", False)
        print(f"  {(r['ts'] or '')[:23]:<24} {r['user_id']:<12} {r['model']:<18} "
              f"{r['tokens_in']:>6,} {r['tokens_out']:>6,} {_fmt_ms(r['latency_ms']):>9}  {status_str}")
    print()


# ── Main ─────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Audit report from data/audit.db")
    p.add_argument("--since",  metavar="DATETIME",
                   help="filter to requests at or after this ISO timestamp (e.g. 2026-06-01)")
    p.add_argument("--db",     metavar="PATH",
                   default=str(_DEFAULT_DB),
                   help=f"path to SQLite database (default: {_DEFAULT_DB})")
    p.add_argument("--no-recent", action="store_true",
                   help="suppress the recent-requests table")
    args = p.parse_args()

    if not Path(args.db).exists():
        print(f"Database not found: {args.db}", file=sys.stderr)
        print("Start the stack and make a few requests first.", file=sys.stderr)
        sys.exit(1)

    agg  = _query(args.db, args.since)
    if not agg:
        print("No matching requests found.")
        return

    _table(agg, args.since, args.db)

    if not args.no_recent:
        recent = _recent_requests(args.db, args.since)
        _recent_table(recent)


if __name__ == "__main__":
    main()
