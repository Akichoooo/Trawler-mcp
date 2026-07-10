"""Browser identity sessions.

A BrowserSession binds the pieces that should move together when crawling:
domain, account state, proxy, cookies, and fingerprint identity. The first
version keeps one active deterministic session per identity key; future proxy
rotation can add multiple candidates behind this module without changing
fetcher call sites.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _session_id_for(
    domain: str,
    *,
    account_id: str = "",
    proxy_url: str = "",
    storage_state_bound: bool = False,
) -> str:
    key = "\x1f".join(
        [
            domain.lower(),
            account_id,
            proxy_url,
            "account" if storage_state_bound else "anonymous",
        ]
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _next_session_id(conn: sqlite3.Connection, base_session_id: str) -> str:
    row = conn.execute(
        "SELECT 1 FROM browser_sessions WHERE session_id = ?",
        (base_session_id,),
    ).fetchone()
    if row is None:
        return base_session_id

    for generation in range(2, 10_000):
        candidate = f"{base_session_id}-{generation}"
        row = conn.execute(
            "SELECT 1 FROM browser_sessions WHERE session_id = ?",
            (candidate,),
        ).fetchone()
        if row is None:
            return candidate
    raise RuntimeError("could not allocate browser session id")


@dataclass(frozen=True)
class BrowserSession:
    session_id: str
    domain: str
    account_id: str = ""
    proxy_url: str = ""
    storage_state_bound: bool = False
    fingerprint_key: str = ""
    success_count: int = 0
    error_score: int = 0
    use_count: int = 0
    status: str = "active"
    last_error: str = ""
    retired_at: str | None = None


def _row_to_session(row: sqlite3.Row) -> BrowserSession:
    data: dict[str, Any] = dict(row)
    return BrowserSession(
        session_id=data["session_id"],
        domain=data["domain"],
        account_id=data.get("account_id") or "",
        proxy_url=data.get("proxy_url") or "",
        storage_state_bound=bool(data.get("storage_state_bound")),
        fingerprint_key=data.get("fingerprint_key") or data["session_id"],
        success_count=int(data.get("success_count") or 0),
        error_score=int(data.get("error_score") or 0),
        use_count=int(data.get("use_count") or 0),
        status=data.get("status") or "active",
        last_error=data.get("last_error") or "",
        retired_at=data.get("retired_at"),
    )


def select_session(
    conn: sqlite3.Connection,
    domain: str,
    *,
    account_id: str = "",
    proxy_url: str = "",
    storage_state_bound: bool = False,
) -> BrowserSession:
    """Return an active session for this identity, creating it if needed."""
    normalized_domain = domain.lower()
    base_session_id = _session_id_for(
        normalized_domain,
        account_id=account_id,
        proxy_url=proxy_url,
        storage_state_bound=storage_state_bound,
    )
    now = _now_iso()
    row = conn.execute(
        "SELECT * FROM browser_sessions "
        "WHERE domain = ? AND account_id = ? AND proxy_url = ? "
        "AND storage_state_bound = ? AND status = 'active' "
        "ORDER BY error_score ASC, updated_at DESC LIMIT 1",
        (normalized_domain, account_id, proxy_url, int(storage_state_bound)),
    ).fetchone()
    if row is None:
        session_id = _next_session_id(conn, base_session_id)
        conn.execute(
            "INSERT INTO browser_sessions "
            "(session_id, domain, account_id, proxy_url, storage_state_bound, "
            "fingerprint_key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                normalized_domain,
                account_id,
                proxy_url,
                int(storage_state_bound),
                session_id,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM browser_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    else:
        session_id = row["session_id"]

    conn.execute(
        "UPDATE browser_sessions SET use_count = use_count + 1, updated_at = ? "
        "WHERE session_id = ?",
        (now, session_id),
    )
    row = conn.execute(
        "SELECT * FROM browser_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return _row_to_session(row)


def get_session(conn: sqlite3.Connection, session_id: str) -> BrowserSession | None:
    row = conn.execute(
        "SELECT * FROM browser_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return _row_to_session(row) if row is not None else None


def mark_good(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "UPDATE browser_sessions SET success_count = success_count + 1, "
        "error_score = CASE WHEN error_score > 0 THEN error_score - 1 ELSE 0 END, "
        "last_error = '', updated_at = ? WHERE session_id = ?",
        (_now_iso(), session_id),
    )


def mark_bad(
    conn: sqlite3.Connection,
    session_id: str,
    error_type: str,
    *,
    retire: bool = False,
) -> None:
    now = _now_iso()
    if retire:
        conn.execute(
            "UPDATE browser_sessions SET error_score = error_score + 3, status = 'retired', "
            "last_error = ?, retired_at = ?, updated_at = ? WHERE session_id = ?",
            (error_type, now, now, session_id),
        )
        return
    conn.execute(
        "UPDATE browser_sessions SET error_score = error_score + 1, "
        "last_error = ?, updated_at = ? WHERE session_id = ?",
        (error_type, now, session_id),
    )


def retire_session(conn: sqlite3.Connection, session_id: str, reason: str = "") -> None:
    mark_bad(conn, session_id, reason or "retired", retire=True)
