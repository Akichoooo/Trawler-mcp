"""domain_rules 手册 — domain → 上次成功档 + 置信度 + 衰减。

零 LLM 进化引擎: 用前自测 + 失败降级 + 成功回写 + 置信度衰减。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC

from trawler import config


@dataclass
class DomainRule:
    domain: str
    gear: str = ""                # 上次成功的 fetcher rung
    selectors: str = ""
    wait_strategy: str = ""
    needs_account: bool = False
    needs_proxy: bool = False
    success_count: int = 0
    fail_count: int = 0
    last_success_at: str = ""
    last_failed_at: str = ""
    last_error: str = ""
    confidence: float = 0.0
    stale: bool = False
    unreachable_until: int = 0


def get(conn: sqlite3.Connection, domain: str) -> DomainRule | None:
    row = conn.execute(
        "SELECT * FROM domain_rules WHERE domain = ?", (domain,)
    ).fetchone()
    if row is None:
        return None
    return DomainRule(
        domain=row["domain"],
        gear=row["gear"] or "",
        selectors=row["selectors"] or "",
        wait_strategy=row["wait_strategy"] or "",
        needs_account=bool(row["needs_account"]),
        needs_proxy=bool(row["needs_proxy"]),
        success_count=row["success_count"],
        fail_count=row["fail_count"],
        last_success_at=row["last_success_at"] or "",
        last_failed_at=row["last_failed_at"] or "",
        last_error=row["last_error"] or "",
        confidence=row["confidence"],
        stale=bool(row["stale"]),
        unreachable_until=row["unreachable_until"] or 0,
    )


def is_unreachable(conn: sqlite3.Connection, domain: str) -> bool:
    """在 unreachable TTL 内 → True (省资源, 不重试)。"""
    row = conn.execute(
        "SELECT unreachable_until FROM domain_rules WHERE domain = ?", (domain,)
    ).fetchone()
    if row is None:
        return False
    return int(row["unreachable_until"] or 0) > time.time()


def should_use_cached(conn: sqlite3.Connection, domain: str) -> str | None:
    """用前自测: 返回可信的 cached gear, 或 None (走默认阶梯)。

    判据: 规则存在 + 非 stale + 置信度 ≥ MIN + 非 unreachable。
    """
    rule = get(conn, domain)
    if rule is None:
        return None
    if rule.stale:
        return None
    if rule.unreachable_until and rule.unreachable_until > time.time():
        return None
    if rule.confidence < config.CONFIDENCE_MIN:
        return None
    if not rule.gear:
        return None
    return rule.gear


def record_success(
    conn: sqlite3.Connection,
    domain: str,
    *,
    gear: str,
    needs_account: bool | None = None,
    needs_proxy: bool = False,
    selectors: str = "",
    wait_strategy: str = "",
) -> None:
    """成功回写: 更新计数 + 置信度 + 清除 stale/unreachable。

    needs_account=None (默认): 保留已有值 (不被成功回写覆盖)。
    needs_account=True/False: 显式设置。
    这样 HITL 标记的 needs_account=1 不会被后续 patchright 成功的 record_success 覆盖成 0。
    """
    from datetime import datetime
    now = datetime.now(UTC).isoformat(timespec="seconds")
    existing = get(conn, domain)
    sc = (existing.success_count if existing else 0) + 1
    fc = existing.fail_count if existing else 0
    conf = _confidence(sc, fc)
    # needs_account: 显式传值用传值, 否则保留已有 (default 0)
    if needs_account is None:
        needs_account_val = bool(existing.needs_account) if existing else False
    else:
        needs_account_val = needs_account
    conn.execute(
        """
        INSERT INTO domain_rules (domain, gear, selectors, wait_strategy,
            needs_account, needs_proxy, success_count, fail_count,
            last_success_at, confidence, stale, unreachable_until)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
        ON CONFLICT(domain) DO UPDATE SET
            gear=excluded.gear, selectors=excluded.selectors,
            wait_strategy=excluded.wait_strategy,
            needs_account=excluded.needs_account, needs_proxy=excluded.needs_proxy,
            success_count=excluded.success_count, fail_count=excluded.fail_count,
            last_success_at=excluded.last_success_at,
            confidence=excluded.confidence,
            stale=0, unreachable_until=0
        """,
        (domain, gear, selectors, wait_strategy,
         int(needs_account_val), int(needs_proxy), sc, fc, now, conf),
    )


def record_failure(
    conn: sqlite3.Connection,
    domain: str,
    *,
    error: str = "",
    mark_unreachable: bool = False,
) -> None:
    """失败回写: 更新 fail_count + 置信度, 可选标 unreachable。"""
    from datetime import datetime
    now = datetime.now(UTC).isoformat(timespec="seconds")
    existing = get(conn, domain)
    sc = existing.success_count if existing else 0
    fc = (existing.fail_count if existing else 0) + 1
    conf = _confidence(sc, fc)
    stale = 1 if conf < config.CONFIDENCE_MIN else 0
    unreachable = 0
    if mark_unreachable:
        unreachable = int(time.time()) + config.UNREACHABLE_TTL
    elif existing and existing.unreachable_until:
        unreachable = existing.unreachable_until

    conn.execute(
        """
        INSERT INTO domain_rules (domain, success_count, fail_count,
            last_failed_at, last_error, confidence, stale, unreachable_until)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(domain) DO UPDATE SET
            fail_count=excluded.fail_count,
            last_failed_at=excluded.last_failed_at,
            last_error=excluded.last_error,
            confidence=excluded.confidence,
            stale=excluded.stale,
            unreachable_until=excluded.unreachable_until
        """,
        (domain, sc, fc, now, error, conf, stale, unreachable),
    )


def _confidence(success: int, fail: int) -> float:
    """置信度。小样本平滑: 加 1 假成功 1 假失败 (拉普拉斯平滑)。"""
    return (success + 1) / (success + fail + 2)


def mark_needs_account(conn: sqlite3.Connection, domain: str) -> None:
    """HITL 触发过 → 标记该域需账号 (下次优先走 HITL)。"""
    conn.execute(
        """
        INSERT INTO domain_rules (domain, needs_account)
        VALUES (?, 1)
        ON CONFLICT(domain) DO UPDATE SET needs_account=1
        """,
        (domain,),
    )
