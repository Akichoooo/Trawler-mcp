"""窄版自审 — 记录 MCP 被怎么调了。

不记身份 IP (那是 agent 平台的事)。只记: ts/tool/url/caller进程级/status/rung/cost。
cost_tokens 永远 0 (MCP 零 LLM, 原则3)。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime

from trawler import config


def write_audit(
    conn: sqlite3.Connection,
    *,
    tool: str,
    url: str,
    status: str,
    rung_used: str = "",
    caller: str | None = None,
) -> None:
    """写一条审计记录。不抛错 (审计失败不能影响主流程)。"""
    if caller is None:
        caller = f"pid:{os.getpid()}"
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        conn.execute(
            "INSERT INTO audit_log (ts, tool, url, caller, status, rung_used, cost_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (ts, tool, url, caller, status, rung_used),
        )
    except sqlite3.Error:
        # 审计是 best-effort, 绝不因它崩主流程
        pass


def recent_errors(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """get_engine_status 用: 取最近 N 条失败记录。"""
    if limit is None:
        limit = config.AUDIT_RECENT_LIMIT
    rows = conn.execute(
        "SELECT ts, tool, url, status, rung_used FROM audit_log "
        "WHERE status != 'ok' ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
