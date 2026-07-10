"""Account Profile Registry.

The registry stores account metadata only. Browser storage/cookies stay in
account_vault and are encrypted there when TRAWLER_VAULT_KEY is configured.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trawler import account_vault, db

STATUSES = {"active", "expired", "needs_login", "blocked"}
LOGIN_METHODS = {"manual_qr", "manual_password", "imported_state"}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_domain(domain: str) -> str:
    return account_vault.domain_dir(domain).name


def normalize_account_id(account_id: str | None = None) -> str:
    return account_vault.normalize_account_id(account_id)


def _normalize_status(status: str) -> str:
    value = str(status or "active").strip().lower()
    if value not in STATUSES:
        raise ValueError(f"invalid account profile status: {status}")
    return value


def _normalize_login_method(login_method: str) -> str:
    value = str(login_method or "manual_qr").strip().lower()
    if value not in LOGIN_METHODS:
        raise ValueError(f"invalid account profile login_method: {login_method}")
    return value


def _risk_flags_json(risk_flags: list[str] | None = None) -> str:
    flags = [str(flag).strip() for flag in (risk_flags or []) if str(flag).strip()]
    return json.dumps(flags, ensure_ascii=False)


def _parse_risk_flags(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


@dataclass(frozen=True)
class AccountProfilePaths:
    profile_dir: str
    storage_state_path: str
    cookie_jar_path: str

    def as_dict(self) -> dict[str, str]:
        return {
            "profile_dir": self.profile_dir,
            "storage_state_path": self.storage_state_path,
            "cookie_jar_path": self.cookie_jar_path,
        }


@dataclass(frozen=True)
class AccountProfile:
    domain: str
    account_id: str
    label: str = ""
    status: str = "active"
    login_method: str = "manual_qr"
    profile_dir: str = ""
    storage_state_path: str = ""
    cookie_jar_path: str = ""
    last_verified_at: str = ""
    expires_at: str = ""
    notes: str = ""
    risk_flags: list[str] | None = None
    is_default: bool = False
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "account_id": self.account_id,
            "label": self.label,
            "status": self.status,
            "login_method": self.login_method,
            "profile_dir": self.profile_dir,
            "storage_state_path": self.storage_state_path,
            "cookie_jar_path": self.cookie_jar_path,
            "last_verified_at": self.last_verified_at,
            "expires_at": self.expires_at,
            "notes": self.notes,
            "risk_flags": list(self.risk_flags or []),
            "is_default": self.is_default,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def profile_paths(domain: str, account_id: str | None = None) -> AccountProfilePaths:
    normalized_domain = normalize_domain(domain)
    normalized_account = normalize_account_id(account_id)
    profile_path = account_vault.profile_dir(normalized_domain, account_id=normalized_account)
    return AccountProfilePaths(
        profile_dir=profile_path,
        storage_state_path=str(
            account_vault.storage_state_path(
                normalized_domain,
                account_id=normalized_account,
            )
        ),
        cookie_jar_path=str(
            account_vault.auto_cookies_path(
                normalized_domain,
                account_id=normalized_account,
            )
        ),
    )


def _row_to_profile(row) -> AccountProfile:
    return AccountProfile(
        domain=row["domain"],
        account_id=row["account_id"],
        label=row["label"] or "",
        status=row["status"] or "active",
        login_method=row["login_method"] or "manual_qr",
        profile_dir=row["profile_dir"] or "",
        storage_state_path=row["storage_state_path"] or "",
        cookie_jar_path=row["cookie_jar_path"] or "",
        last_verified_at=row["last_verified_at"] or "",
        expires_at=row["expires_at"] or "",
        notes=row["notes"] or "",
        risk_flags=_parse_risk_flags(row["risk_flags_json"] or "[]"),
        is_default=bool(row["is_default"]),
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


def register_profile(
    domain: str,
    *,
    account_id: str | None = "default",
    label: str = "",
    login_method: str = "manual_qr",
    notes: str = "",
    risk_flags: list[str] | None = None,
    make_default: bool = False,
) -> AccountProfile:
    normalized_domain = normalize_domain(domain)
    normalized_account = normalize_account_id(account_id)
    normalized_method = _normalize_login_method(login_method)
    paths = profile_paths(normalized_domain, normalized_account)
    now = _now_iso()
    conn = db.connect()
    try:
        with db.tx(conn):
            if make_default:
                conn.execute(
                    "UPDATE account_profiles SET is_default = 0, updated_at = ? "
                    "WHERE domain = ?",
                    (now, normalized_domain),
                )
            existing = conn.execute(
                "SELECT created_at, is_default FROM account_profiles "
                "WHERE domain = ? AND account_id = ?",
                (normalized_domain, normalized_account),
            ).fetchone()
            is_default = 1 if make_default else int(existing["is_default"]) if existing else 0
            if existing is None and normalized_account == "default":
                is_default = 1 if not make_default else is_default
            conn.execute(
                "INSERT INTO account_profiles "
                "(domain, account_id, label, status, login_method, profile_dir, "
                "storage_state_path, cookie_jar_path, notes, risk_flags_json, "
                "is_default, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(domain, account_id) DO UPDATE SET "
                "label = excluded.label, "
                "login_method = excluded.login_method, "
                "profile_dir = excluded.profile_dir, "
                "storage_state_path = excluded.storage_state_path, "
                "cookie_jar_path = excluded.cookie_jar_path, "
                "notes = CASE WHEN excluded.notes != '' "
                "THEN excluded.notes ELSE account_profiles.notes END, "
                "risk_flags_json = CASE WHEN excluded.risk_flags_json != '[]' "
                "THEN excluded.risk_flags_json ELSE account_profiles.risk_flags_json END, "
                "is_default = excluded.is_default, "
                "updated_at = excluded.updated_at",
                (
                    normalized_domain,
                    normalized_account,
                    str(label or ""),
                    normalized_method,
                    paths.profile_dir,
                    paths.storage_state_path,
                    paths.cookie_jar_path,
                    str(notes or ""),
                    _risk_flags_json(risk_flags),
                    is_default,
                    existing["created_at"] if existing else now,
                    now,
                ),
            )
        return get_profile(normalized_domain, normalized_account)  # type: ignore[return-value]
    finally:
        conn.close()


def list_profiles(domain: str = "") -> list[AccountProfile]:
    conn = db.connect()
    try:
        if domain:
            normalized_domain = normalize_domain(domain)
            rows = conn.execute(
                "SELECT * FROM account_profiles WHERE domain = ? "
                "ORDER BY is_default DESC, updated_at DESC, account_id ASC",
                (normalized_domain,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM account_profiles "
                "ORDER BY domain ASC, is_default DESC, updated_at DESC, account_id ASC"
            ).fetchall()
        return [_row_to_profile(row) for row in rows]
    finally:
        conn.close()


def get_profile(domain: str, account_id: str | None = "default") -> AccountProfile | None:
    normalized_domain = normalize_domain(domain)
    normalized_account = normalize_account_id(account_id)
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM account_profiles WHERE domain = ? AND account_id = ?",
            (normalized_domain, normalized_account),
        ).fetchone()
        return _row_to_profile(row) if row is not None else None
    finally:
        conn.close()


def default_account_id(domain: str) -> str:
    normalized_domain = normalize_domain(domain)
    profiles = list_profiles(normalized_domain)
    for profile in profiles:
        if profile.is_default and is_usable_for_automation(profile):
            return profile.account_id
    for profile in profiles:
        if is_usable_for_automation(profile):
            return profile.account_id
    return "default"


def resolve_account_id(domain: str, account_id: str | None = None) -> str:
    raw = str(account_id or "").strip()
    if raw:
        return normalize_account_id(raw)
    return default_account_id(domain)


def is_expired(profile: AccountProfile, *, now: datetime | None = None) -> bool:
    if profile.status in {"expired", "needs_login", "blocked"}:
        return True
    expires_at = _parse_iso(profile.expires_at)
    if expires_at is None:
        return False
    return expires_at <= (now or datetime.now(UTC))


def is_usable_for_automation(profile: AccountProfile | None) -> bool:
    if profile is None:
        return False
    return profile.status == "active" and not is_expired(profile)


def status_reason(profile: AccountProfile | None) -> str:
    if profile is None:
        return "no_profile"
    if profile.status == "blocked":
        return "blocked"
    if profile.status == "needs_login":
        return "needs_login"
    if profile.status == "expired" or is_expired(profile):
        return "expired"
    if profile.status != "active":
        return profile.status
    return "active"


def mark_profile_status(
    domain: str,
    account_id: str | None,
    status: str,
    *,
    notes: str = "",
    expires_at: str = "",
    risk_flags: list[str] | None = None,
) -> AccountProfile:
    normalized_domain = normalize_domain(domain)
    normalized_account = normalize_account_id(account_id)
    normalized_status = _normalize_status(status)
    profile = get_profile(normalized_domain, normalized_account)
    if profile is None:
        profile = register_profile(
            normalized_domain,
            account_id=normalized_account,
            notes=notes,
        )
    now = _now_iso()
    conn = db.connect()
    try:
        with db.tx(conn):
            conn.execute(
                "UPDATE account_profiles SET status = ?, notes = ?, expires_at = ?, "
                "risk_flags_json = ?, updated_at = ? "
                "WHERE domain = ? AND account_id = ?",
                (
                    normalized_status,
                    str(notes if notes else profile.notes),
                    str(expires_at if expires_at else profile.expires_at),
                    _risk_flags_json(risk_flags if risk_flags is not None else profile.risk_flags),
                    now,
                    normalized_domain,
                    normalized_account,
                ),
            )
        return get_profile(normalized_domain, normalized_account)  # type: ignore[return-value]
    finally:
        conn.close()


def touch_verified(domain: str, account_id: str | None = None) -> AccountProfile:
    normalized_domain = normalize_domain(domain)
    normalized_account = normalize_account_id(account_id)
    if get_profile(normalized_domain, normalized_account) is None:
        register_profile(
            normalized_domain,
            account_id=normalized_account,
            make_default=normalized_account == "default",
        )
    now = _now_iso()
    conn = db.connect()
    try:
        with db.tx(conn):
            conn.execute(
                "UPDATE account_profiles SET status = 'active', last_verified_at = ?, "
                "updated_at = ? WHERE domain = ? AND account_id = ?",
                (now, now, normalized_domain, normalized_account),
            )
        return get_profile(normalized_domain, normalized_account)  # type: ignore[return-value]
    finally:
        conn.close()


def registry_payload(domain: str = "") -> dict[str, Any]:
    items = [profile.as_dict() for profile in list_profiles(domain)]
    return {"ok": True, "count": len(items), "items": items}


def path_exists(path: str) -> bool:
    return Path(path).exists()
