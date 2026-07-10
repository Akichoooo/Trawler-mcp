"""crawl_jobs 异步作业 — crawl_site 用。

crawl_site 立即返回 job_id, 后台蜘蛛爬。
wait_for_job 阻塞等结果。
启动时清理僵尸 job (status=running → failed)。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def create_job(conn: sqlite3.Connection, start_url: str, total: int = 0) -> str:
    """创建异步作业, 返回 job_id。"""
    job_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO crawl_jobs (job_id, start_url, status, visited_json, queue_json, "
        "updated_at, total, completed) VALUES (?, ?, 'running', '[]', '[]', ?, ?, 0)",
        (job_id, start_url, _now_iso(), total),
    )
    return job_id


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM crawl_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def get_status(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    """非阻塞 peek。"""
    return get_job(conn, job_id)


def update_progress(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    visited: list[str],
    queue: list[str],
    completed: int,
) -> None:
    """更新作业进度 (visited/queue/completed)。原子。"""
    conn.execute(
        "UPDATE crawl_jobs SET visited_json=?, queue_json=?, completed=?, updated_at=? "
        "WHERE job_id=?",
        (json.dumps(visited), json.dumps(queue), completed, _now_iso(), job_id),
    )


def complete_job(conn: sqlite3.Connection, job_id: str, status: str = "completed") -> None:
    """标记作业结束。status: completed / failed。"""
    conn.execute(
        "UPDATE crawl_jobs SET status=?, updated_at=? WHERE job_id=?",
        (status, _now_iso(), job_id),
    )


def cancel_job(conn: sqlite3.Connection, job_id: str) -> bool:
    """Mark a running job as cancelled."""
    cur = conn.execute(
        "UPDATE crawl_jobs SET status='cancelled', updated_at=? "
        "WHERE job_id=? AND status IN ('running', 'crawling')",
        (_now_iso(), job_id),
    )
    return bool(cur.rowcount)


def fail_running_jobs(conn: sqlite3.Connection) -> int:
    """启动清理: 所有 status=running 的 → failed (v1 不续传, 直接 fail 最安全)。

    返回清理的条数。
    """
    cur = conn.execute(
        "UPDATE crawl_jobs SET status='failed', updated_at=? "
        "WHERE status IN ('running', 'crawling')",
        (_now_iso(),),
    )
    return cur.rowcount or 0


def list_active_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT job_id, start_url, status, completed, total, updated_at "
        "FROM crawl_jobs ORDER BY updated_at DESC LIMIT 20"
    ).fetchall()
    return [dict(r) for r in rows]
