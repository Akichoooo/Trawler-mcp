"""SQLite WAL 存储 — 单库四表, 替代所有 JSON。

解决并发撕裂/事务/多进程锁。连接时强制 WAL + busy_timeout。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from trawler import config

_PRAGMAS = (
    # WAL 模式: 读写不互斥
    "PRAGMA journal_mode=WAL;",
    # 多进程 stdio 并发: 等 5s 而非立刻报 locked
    "PRAGMA busy_timeout=5000;",
    # 外键约束开
    "PRAGMA foreign_keys=ON;",
    # 普通 DB 调优
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA temp_store=MEMORY;",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS domain_rules (
    domain              TEXT PRIMARY KEY,
    gear                TEXT,
    selectors           TEXT,
    wait_strategy       TEXT,
    needs_account       INTEGER DEFAULT 0,
    needs_proxy         INTEGER DEFAULT 0,
    success_count       INTEGER DEFAULT 0,
    fail_count          INTEGER DEFAULT 0,
    last_success_at     TEXT,
    last_failed_at      TEXT,
    last_error          TEXT,
    confidence          REAL DEFAULT 0.0,
    stale               INTEGER DEFAULT 0,
    unreachable_until   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seen_urls (
    sha1_full   TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    raw_id      TEXT,
    crawled_at  TEXT NOT NULL,
    content_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_raw ON seen_urls(raw_id);
CREATE INDEX IF NOT EXISTS idx_seen_crawled ON seen_urls(crawled_at DESC);

CREATE TABLE IF NOT EXISTS crawl_jobs (
    job_id      TEXT PRIMARY KEY,
    start_url   TEXT NOT NULL,
    status      TEXT NOT NULL,
    visited_json TEXT,
    queue_json  TEXT,
    updated_at  TEXT NOT NULL,
    total       INTEGER DEFAULT 0,
    completed   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS frontier_requests (
    job_id           TEXT NOT NULL,
    url              TEXT NOT NULL,
    status           TEXT NOT NULL,
    priority         INTEGER DEFAULT 0,
    depth            INTEGER DEFAULT 0,
    parent_url       TEXT DEFAULT '',
    retry_count      INTEGER DEFAULT 0,
    next_fetch_at    REAL DEFAULT 0,
    lease_expires_at REAL DEFAULT 0,
    raw_id           TEXT DEFAULT '',
    content_hash     TEXT DEFAULT '',
    last_error       TEXT DEFAULT '',
    discovered_at    TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (job_id, url),
    FOREIGN KEY (job_id) REFERENCES crawl_jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_frontier_next
    ON frontier_requests(job_id, status, priority DESC, depth ASC, discovered_at ASC);
CREATE INDEX IF NOT EXISTS idx_frontier_status
    ON frontier_requests(job_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    ts          TEXT NOT NULL,
    tool        TEXT NOT NULL,
    url         TEXT,
    caller      TEXT,
    status      TEXT,
    rung_used   TEXT,
    cost_tokens INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS browser_sessions (
    session_id           TEXT PRIMARY KEY,
    domain               TEXT NOT NULL,
    account_id           TEXT DEFAULT '',
    proxy_url            TEXT DEFAULT '',
    storage_state_bound  INTEGER DEFAULT 0,
    fingerprint_key      TEXT NOT NULL,
    success_count        INTEGER DEFAULT 0,
    error_score          INTEGER DEFAULT 0,
    use_count            INTEGER DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'active',
    last_error           TEXT DEFAULT '',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    retired_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_browser_sessions_domain_status
    ON browser_sessions(domain, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS account_profiles (
    domain              TEXT NOT NULL,
    account_id          TEXT NOT NULL,
    label               TEXT DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'active',
    login_method        TEXT DEFAULT 'manual_qr',
    profile_dir         TEXT DEFAULT '',
    storage_state_path  TEXT DEFAULT '',
    cookie_jar_path     TEXT DEFAULT '',
    last_verified_at    TEXT DEFAULT '',
    expires_at          TEXT DEFAULT '',
    notes               TEXT DEFAULT '',
    risk_flags_json     TEXT DEFAULT '[]',
    is_default          INTEGER DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (domain, account_id)
);
CREATE INDEX IF NOT EXISTS idx_account_profiles_domain_status
    ON account_profiles(domain, status, updated_at DESC);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """建连接 + 设 PRAGMA。每次都跑一遍 PRAGMA (幂等)。

    写操作由 crawl_url._db_write 用 asyncio.Lock 串行化 (单事件循环内),
    避免并发同连接写竞态。不跨线程, 保留默认 check_same_thread。
    """
    conn = sqlite3.connect(db_path or str(config.DB_PATH), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn


def init_db(db_path: str | None = None) -> None:
    """建表。幂等。启动时调一次。"""
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """增量 migration (SQLite ALTER TABLE 不支持 IF NOT EXISTS, 需检查列存在)。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(domain_rules)").fetchall()}
    if "circuit_state" not in cols:
        conn.execute("ALTER TABLE domain_rules ADD COLUMN circuit_state TEXT DEFAULT 'closed'")
    if "circuit_opened_at" not in cols:
        conn.execute("ALTER TABLE domain_rules ADD COLUMN circuit_opened_at INTEGER DEFAULT 0")
    if "consecutive_failures" not in cols:
        conn.execute("ALTER TABLE domain_rules ADD COLUMN consecutive_failures INTEGER DEFAULT 0")


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务上下文。提交/回滚自动。使用 BEGIN IMMEDIATE 强锁，防写升级死锁。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass  # 如果 BEGIN IMMEDIATE 锁争抢失败，则事务未建立，忽略 rollback 报错
        raise


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
