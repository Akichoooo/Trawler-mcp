"""Persistent crawl frontier.

The frontier is the crawl job's durable queue. It records discovered URLs,
leases work to the in-process spider loop, and keeps per-URL status/errors for
agent-facing job inspection.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

TERMINAL_STATUSES = ("fetched", "error", "skipped")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def enqueue(
    conn: sqlite3.Connection,
    job_id: str,
    url: str,
    *,
    depth: int = 0,
    parent_url: str = "",
    priority: int = 0,
) -> bool:
    """Add a URL to the frontier. Returns True when inserted."""
    now = _now_iso()
    cur = conn.execute(
        "INSERT OR IGNORE INTO frontier_requests "
        "(job_id, url, status, priority, depth, parent_url, discovered_at, updated_at) "
        "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)",
        (job_id, url, priority, depth, parent_url, now, now),
    )
    return bool(cur.rowcount)


def lease_next(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    lease_seconds: float = 300.0,
) -> dict[str, Any] | None:
    """Lease the next queued URL for a job."""
    now = time.time()
    row = conn.execute(
        "SELECT * FROM frontier_requests "
        "WHERE job_id = ? AND status = 'queued' AND next_fetch_at <= ? "
        "ORDER BY priority DESC, depth ASC, discovered_at ASC LIMIT 1",
        (job_id, now),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE frontier_requests SET status = 'in_progress', lease_expires_at = ?, "
        "updated_at = ? WHERE job_id = ? AND url = ?",
        (now + lease_seconds, _now_iso(), job_id, row["url"]),
    )
    leased = conn.execute(
        "SELECT * FROM frontier_requests WHERE job_id = ? AND url = ?",
        (job_id, row["url"]),
    ).fetchone()
    return dict(leased) if leased is not None else None


def release_expired_leases(conn: sqlite3.Connection, job_id: str) -> int:
    """Return expired in-progress leases to queued."""
    cur = conn.execute(
        "UPDATE frontier_requests SET status = 'queued', lease_expires_at = 0, "
        "updated_at = ? WHERE job_id = ? AND status = 'in_progress' "
        "AND lease_expires_at > 0 AND lease_expires_at <= ?",
        (_now_iso(), job_id, time.time()),
    )
    return cur.rowcount or 0


def mark_fetched(
    conn: sqlite3.Connection,
    job_id: str,
    url: str,
    *,
    raw_id: str = "",
    content_hash: str = "",
) -> None:
    conn.execute(
        "UPDATE frontier_requests SET status = 'fetched', raw_id = ?, content_hash = ?, "
        "last_error = '', lease_expires_at = 0, updated_at = ? WHERE job_id = ? AND url = ?",
        (raw_id, content_hash, _now_iso(), job_id, url),
    )


def mark_error(conn: sqlite3.Connection, job_id: str, url: str, error: str) -> None:
    conn.execute(
        "UPDATE frontier_requests SET status = 'error', last_error = ?, "
        "lease_expires_at = 0, updated_at = ? WHERE job_id = ? AND url = ?",
        (error, _now_iso(), job_id, url),
    )


def mark_retry(
    conn: sqlite3.Connection,
    job_id: str,
    url: str,
    error: str,
    *,
    delay_seconds: float,
) -> int:
    """Return a failed lease to queued with a future fetch time."""
    row = conn.execute(
        "SELECT retry_count FROM frontier_requests WHERE job_id = ? AND url = ?",
        (job_id, url),
    ).fetchone()
    retry_count = int(row["retry_count"] or 0) + 1 if row is not None else 1
    conn.execute(
        "UPDATE frontier_requests SET status = 'queued', retry_count = ?, next_fetch_at = ?, "
        "last_error = ?, lease_expires_at = 0, updated_at = ? WHERE job_id = ? AND url = ?",
        (retry_count, time.time() + delay_seconds, error, _now_iso(), job_id, url),
    )
    return retry_count


def has_pending(conn: sqlite3.Connection, job_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM frontier_requests "
        "WHERE job_id = ? AND status IN ('queued', 'in_progress') LIMIT 1",
        (job_id,),
    ).fetchone()
    return row is not None


def counts(conn: sqlite3.Connection, job_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM frontier_requests "
        "WHERE job_id = ? GROUP BY status",
        (job_id,),
    ).fetchall()
    result = {row["status"]: int(row["count"]) for row in rows}
    result["terminal"] = sum(result.get(status, 0) for status in TERMINAL_STATUSES)
    result["total"] = sum(v for k, v in result.items() if k != "terminal")
    return result


def done_urls(conn: sqlite3.Connection, job_id: str, *, limit: int | None = None) -> list[str]:
    sql = (
        "SELECT url FROM frontier_requests WHERE job_id = ? AND status IN ('fetched', 'error') "
        "ORDER BY updated_at ASC"
    )
    params: tuple[Any, ...] = (job_id,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (job_id, limit)
    rows = conn.execute(sql, params).fetchall()
    return [row["url"] for row in rows]


def queued_urls(conn: sqlite3.Connection, job_id: str, *, limit: int = 200) -> list[str]:
    rows = conn.execute(
        "SELECT url FROM frontier_requests WHERE job_id = ? AND status = 'queued' "
        "ORDER BY priority DESC, depth ASC, discovered_at ASC LIMIT ?",
        (job_id, limit),
    ).fetchall()
    return [row["url"] for row in rows]


def errors(conn: sqlite3.Connection, job_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT url, depth, retry_count, last_error, updated_at FROM frontier_requests "
        "WHERE job_id = ? AND status = 'error' ORDER BY updated_at DESC LIMIT ?",
        (job_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def result_page(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    cursor: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 100))
    safe_cursor = max(0, cursor)
    rows = conn.execute(
        "SELECT url, status, raw_id, last_error, updated_at FROM frontier_requests "
        "WHERE job_id = ? AND status IN ('fetched', 'error') "
        "ORDER BY updated_at ASC LIMIT ? OFFSET ?",
        (job_id, safe_limit, safe_cursor),
    ).fetchall()
    items = [dict(row) for row in rows]
    next_cursor = safe_cursor + len(items) if len(items) == safe_limit else None
    return {"items": items, "next_cursor": next_cursor}
