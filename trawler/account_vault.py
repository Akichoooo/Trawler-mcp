"""account_vault — 账号态读写分离。

读写分离防 Chrome profile 文件锁:
- HITL 登录: launch_persistent_context (独占写态, 串行)
- 正常爬取: new_context(storage_state=<json>) (并发读态, 无锁)

storage_state.json 原子写 (.tmp → os.replace) 防脏读。
profile 目录 LRU 清理见 lifecycle.py。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet

from trawler import config
from trawler.atomic import atomic_write

log = logging.getLogger("trawler.vault")

# 懒初始化: 模块级 import 时不强制要求 VAULT_KEY (否则容器未设 env 即崩, 且无法降级运行)。
# 首次真正读写加密态时才校验, 允许无 key 启动做无账号态的轻量爬取。
_fernet: Fernet | None = None
_VAULT_INIT_LOCK = __import__("threading").Lock()
_SAFE_PATH_KEY = re.compile(r"^[A-Za-z0-9_.-]+$")
_DEFAULT_ACCOUNT_KEYS = {"", "default"}


def _safe_path_key(value: str, label: str) -> str:
    key = str(value or "").strip().lower()
    if (
        not key
        or key in {".", ".."}
        or ".." in key
        or "/" in key
        or "\\" in key
        or ":" in key
        or not _SAFE_PATH_KEY.fullmatch(key)
    ):
        raise ValueError(f"invalid {label}")
    return key


def _safe_domain_key(domain: str) -> str:
    normalized = str(domain or "").strip().lower().strip(".")
    try:
        normalized = normalized.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise ValueError("invalid domain") from e
    return _safe_path_key(normalized, "domain")


def _safe_session_key(session_id: str) -> str:
    return _safe_path_key(session_id, "session_id")


def normalize_account_id(account_id: str | None = None) -> str:
    """Normalize an account profile id used for registry rows and vault paths."""
    key = str(account_id or "default").strip().lower()
    if key in _DEFAULT_ACCOUNT_KEYS:
        return "default"
    return _safe_path_key(key, "account_id")


def _get_fernet() -> Fernet:
    """懒初始化 Fernet。首次访问时从 env 读 key 并缓存。
    缺 key → RuntimeError (调用方决定降级: 跳过加密态, 走无账号路径)。"""
    global _fernet
    if _fernet is not None:
        return _fernet
    with _VAULT_INIT_LOCK:
        if _fernet is not None:
            return _fernet
        key = os.environ.get("TRAWLER_VAULT_KEY")
        if not key:
            raise RuntimeError(
                "TRAWLER_VAULT_KEY environment variable required for encrypting cookies. "
                "Set it to a base64 fernet key (Fernet.generate_key())."
            )
        _fernet = Fernet(key.encode())
    return _fernet


def is_vault_enabled() -> bool:
    """是否配置了加密 key (供 crawl_url 决定是否走账号路径)。"""
    return bool(os.environ.get("TRAWLER_VAULT_KEY"))


def domain_dir(domain: str) -> Path:
    """某域的 vault 目录。"""
    root = config.VAULT_DIR.resolve()
    target = (root / _safe_domain_key(domain)).resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise PermissionError(f"path outside VAULT_DIR: {domain}") from e
    return target


def account_dir(domain: str, account_id: str | None = None) -> Path:
    """Vault directory for one account profile.

    The default account keeps the historical domain-level layout for backwards
    compatibility. Named accounts use accounts/<account_id>/ to avoid mixing
    browser profiles, storage_state, or cookie jars.
    """
    base = domain_dir(domain)
    account_key = normalize_account_id(account_id)
    if account_key == "default":
        return base
    target = (base / "accounts" / account_key).resolve()
    try:
        target.relative_to(base)
    except ValueError as e:
        raise PermissionError(f"path outside domain vault dir: {domain}/{account_id}") from e
    return target


def profile_dir(domain: str, account_id: str | None = None) -> str:
    """HITL 写态: 持久化 Chrome profile 目录。"""
    p = account_dir(domain, account_id=account_id) / "profile"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def storage_state_path(domain: str, account_id: str | None = None) -> Path:
    """并发读态: storage_state.json.enc 路径。"""
    return account_dir(domain, account_id=account_id) / "storage_state.json.enc"


def auto_cookies_path(
    domain: str,
    session_id: str | None = None,
    account_id: str | None = None,
) -> Path:
    """Encrypted browser-to-HTTP cookie jar path."""
    root = account_dir(domain, account_id=account_id)
    if session_id:
        session_key = _safe_session_key(session_id)
        return root / "sessions" / session_key / "auto_cookies.json.enc"
    return root / "auto_cookies.json.enc"


def get_storage_state(domain: str, account_id: str | None = None) -> str | None:
    """读 storage_state (并发读态)。不存在 → None。

    EAFP 先试加密版, 防 legacy_path TOCTOU (HITL 登录成功瞬间写加密版, 老逻辑读到明文 legacy)。
    读时不加锁 (JSON 文件, 读瞬时副本; 写方用原子 rename 保证读到的总是完整文件)。
    未配置 VAULT_KEY 时返回 None (允许无账号态轻量爬取, 不阻断主流程)。
    """
    if not is_vault_enabled():
        if not config.ALLOW_LEGACY_PLAINTEXT_VAULT:
            return None
        # 仍降级试 legacy 明文 (老版本兼容)
        legacy_path = account_dir(domain, account_id=account_id) / "storage_state.json"
        if legacy_path.exists():
            try:
                return legacy_path.read_text(encoding="utf-8")
            except OSError:
                pass
        return None
    path = storage_state_path(domain, account_id=account_id)
    try:
        encrypted_data = path.read_bytes()
        decrypted = _get_fernet().decrypt(encrypted_data)
        return decrypted.decode("utf-8")
    except FileNotFoundError:
        if not config.ALLOW_LEGACY_PLAINTEXT_VAULT:
            return None
        # 加密版不存在, 降级试 legacy 明文 (老版本兼容)
        legacy_path = account_dir(domain, account_id=account_id) / "storage_state.json"
        if legacy_path.exists():
            try:
                return legacy_path.read_text(encoding="utf-8")
            except OSError:
                pass
        return None
    except Exception as e:
        log.warning("read/decrypt storage_state failed for %s: %s", domain, e)
        return None


def save_storage_state(domain: str, state: dict, account_id: str | None = None) -> None:
    """写 storage_state (原子写: .tmp → os.replace)。

    由 HITL rung 调用 (人过 CAPTCHA 后导出)。
    """
    path = storage_state_path(domain, account_id=account_id)
    raw_json = json.dumps(state, ensure_ascii=False)
    encrypted = _get_fernet().encrypt(raw_json.encode("utf-8"))

    # 因为 atomic_write 现在不支持直接写 bytes，我们可以 decode 成 str (base64)
    # Fernet 加密后已经是 URL-safe base64
    atomic_write(path, encrypted.decode("utf-8"))


def has_account(domain: str, account_id: str | None = None) -> bool:
    """该域是否已有账号态。

    判 storage_state.json 是否存在 (KB 级, 真账号态)。
    profile 目录不算 (HITL 可能创建了空目录但没登录成功)。
    """
    return storage_state_path(domain, account_id=account_id).exists()


def invalidate_storage_state(domain: str, account_id: str | None = None) -> None:
    """失效时删旧 state，防脏态被复用。"""
    path = storage_state_path(domain, account_id=account_id)
    if path.exists():
        try:
            path.unlink()
        except OSError as e:
            log.warning("failed to invalidate storage_state for %s: %s", domain, e)


def _load_auto_cookie_jar(
    domain: str,
    session_id: str | None = None,
    account_id: str | None = None,
) -> list[dict]:
    path = auto_cookies_path(domain, session_id=session_id, account_id=account_id)
    if is_vault_enabled():
        try:
            encrypted_data = path.read_bytes()
            raw_json = _get_fernet().decrypt(encrypted_data).decode("utf-8")
            data = json.loads(raw_json)
            if isinstance(data, list):
                return [cookie for cookie in data if isinstance(cookie, dict)]
        except FileNotFoundError:
            if session_id:
                return _load_auto_cookie_jar(domain, account_id=account_id)
        except Exception as e:
            log.warning("read/decrypt auto cookies failed for %s: %s", domain, e)

    legacy_path = account_dir(domain, account_id=account_id) / "auto_cookies.json"
    if not session_id and config.ALLOW_LEGACY_PLAINTEXT_VAULT and legacy_path.exists():
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return [
                    {"name": name, "value": value, "domain": domain, "path": "/"}
                    for name, value in data.items()
                    if isinstance(name, str) and isinstance(value, str)
                ]
            if isinstance(data, list):
                return [cookie for cookie in data if isinstance(cookie, dict)]
        except Exception:
            pass
    return []


def _cookie_key(cookie: dict, default_domain: str) -> tuple[str, str, str]:
    return (
        str(cookie.get("name") or ""),
        str(cookie.get("domain") or default_domain),
        str(cookie.get("path") or "/"),
    )


def _normalize_cookie(cookie: dict, default_domain: str) -> dict | None:
    name = cookie.get("name")
    value = cookie.get("value")
    if not name or value is None:
        return None
    normalized = {
        "name": str(name),
        "value": str(value),
        "domain": str(cookie.get("domain") or default_domain),
        "path": str(cookie.get("path") or "/"),
    }
    for key in ("expires", "httpOnly", "secure", "sameSite"):
        if key in cookie:
            normalized[key] = cookie[key]
    return normalized


def _is_cookie_fresh(cookie: dict) -> bool:
    expires = cookie.get("expires")
    if expires in (None, "", -1):
        return True
    try:
        return float(expires) > __import__("time").time()
    except (TypeError, ValueError):
        return True


def save_auto_cookies(
    domain: str,
    cookies: list[dict],
    session_id: str | None = None,
    account_id: str | None = None,
) -> None:
    """自动保存回流 Cookie (如 cf_clearance 与公共 Cookie)。"""
    if not is_vault_enabled():
        log.warning("skip auto cookie persistence for %s: TRAWLER_VAULT_KEY is not set", domain)
        return

    existing = {
        _cookie_key(cookie, domain): cookie
        for cookie in _load_auto_cookie_jar(domain, session_id=session_id, account_id=account_id)
        if _is_cookie_fresh(cookie)
    }
    for cookie in cookies:
        normalized = _normalize_cookie(cookie, domain)
        if normalized:
            existing[_cookie_key(normalized, domain)] = normalized

    raw_json = json.dumps(list(existing.values()), ensure_ascii=False)
    encrypted = _get_fernet().encrypt(raw_json.encode("utf-8"))
    atomic_write(
        auto_cookies_path(domain, session_id=session_id, account_id=account_id),
        encrypted.decode("utf-8"),
    )

    legacy_path = account_dir(domain, account_id=account_id) / "auto_cookies.json"
    if legacy_path.exists():
        try:
            legacy_path.unlink()
        except OSError:
            pass


def get_auto_cookies(
    domain: str,
    session_id: str | None = None,
    account_id: str | None = None,
) -> dict[str, str]:
    """获取某域已保存的回流 Cookie。"""
    result: dict[str, str] = {}
    for cookie in _load_auto_cookie_jar(
        domain,
        session_id=session_id,
        account_id=account_id,
    ):
        if not _is_cookie_fresh(cookie):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result[name] = value
    return result
