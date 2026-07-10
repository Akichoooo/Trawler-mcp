"""seen_urls 去重 — 全 SHA1 + TTL。

幂等: 同 URL 在 TTL 内重抓 → 返回旧 raw_id, 不重复爬。
force_refresh=True 跳过。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

from trawler import config
from trawler.urlnorm import canonical_url  # 见 urlnorm.py


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _now_ts() -> int:
    return int(datetime.now(UTC).timestamp())


def url_id(url: str) -> str:
    """raw_id = sha1(规范化url)[:ID_LEN]。全 SHA1 防碰撞。"""
    canon = canonical_url(url)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[: config.ID_LEN]


def lookup(conn: sqlite3.Connection, url: str, *, ttl: int | None = None) -> str | None:
    """命中且在 TTL 内 → 返回旧 raw_id。否则 None。

    ttl=None 走默认 TTL (按域名分类)。
    """
    sha = url_id(url)
    row = conn.execute(
        "SELECT raw_id, crawled_at FROM seen_urls WHERE sha1_full = ?",
        (sha,),
    ).fetchone()
    if row is None or not row["raw_id"]:
        return None
    # TTL 判定
    if ttl is None:
        ttl = _ttl_for(url)
    crawled_ts = _parse_iso_to_ts(row["crawled_at"])
    if crawled_ts is None:
        return None
    if _now_ts() - crawled_ts > ttl:
        return None  # 过期
    return row["raw_id"]


def record(conn: sqlite3.Connection, url: str, raw_id: str, content_hash: str = "") -> None:
    """记录/更新 seen。原子 upsert。"""
    sha = url_id(url)
    conn.execute(
        "INSERT INTO seen_urls (sha1_full, url, raw_id, crawled_at, content_hash) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(sha1_full) DO UPDATE SET "
        "  raw_id=excluded.raw_id, crawled_at=excluded.crawled_at, "
        "  content_hash=excluded.content_hash, url=excluded.url",
        (sha, url, raw_id, _now_iso(), content_hash),
    )


def _ttl_for(url: str) -> int:
    """按域名分类 TTL。新闻站 1h, 维基 7d, 默认 6h。"""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if any(k in host for k in ("news", "blog", "medium.com")):
        return config.CACHE_TTL_NEWS
    if "wikipedia.org" in host or "wiki" in host:
        return config.CACHE_TTL_WIKI
    return config.CACHE_TTL_DEFAULT


def _parse_iso_to_ts(iso: str) -> int | None:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except (ValueError, TypeError):
        return None


def content_hash_changed(conn: sqlite3.Connection, url: str, new_hash: str) -> bool:
    """检测页面正文是否变化 (用于增量刷新决策)。"""
    sha = url_id(url)
    row = conn.execute(
        "SELECT content_hash FROM seen_urls WHERE sha1_full = ?", (sha,)
    ).fetchone()
    if row is None or not row["content_hash"]:
        return True  # 没记录过 = 视为变了
    return row["content_hash"] != new_hash
