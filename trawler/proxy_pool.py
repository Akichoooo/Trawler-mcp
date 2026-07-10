"""Session-aware proxy selection."""

from __future__ import annotations

import hashlib
import re

from trawler import config

_SPLIT_RE = re.compile(r"[\s,;]+")


def configured_proxies() -> list[str]:
    pool = [p.strip() for p in _SPLIT_RE.split(config.PROXY_POOL or "") if p.strip()]
    if pool:
        return pool
    return [p for p in (config.HTTPS_PROXY, config.HTTP_PROXY) if p]


def select_proxy(
    use_proxy: bool,
    *,
    domain: str = "",
    account_id: str = "",
    session_id: str = "",
) -> str:
    if not use_proxy:
        return ""
    proxies = configured_proxies()
    if not proxies:
        return ""
    identity = session_id or account_id or domain or "default"
    idx = int(hashlib.sha1(identity.encode()).hexdigest()[:8], 16) % len(proxies)
    return proxies[idx]
