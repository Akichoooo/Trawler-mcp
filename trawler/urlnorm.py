"""URL 规范化 — 进去先规范, 防同页多 ID。

strip fragment / 统一尾斜杠 / 去 UTM 参数 / 小写 scheme+host。
canonical_url 用于 sha1 去重; raw_id 用它派生。
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# UTM 跟踪参数 (去)
_UTM_KEYS = re.compile(r"^(utm_|fbclid|gclid|ref|source|mc_eid|vero_id|yclid)", re.IGNORECASE)

# scheme + host 大小写不敏感, 统一小写
_TRAILING_SLASH_RE = re.compile(r"/+$")


def canonical_url(url: str) -> str:
    """规范化 URL 用于去重。

    1. strip fragment (#...)
    2. scheme + host 小写
    3. 去 UTM 等跟踪参数
    4. path 去尾斜杠 (但根 / 保留)
    5. query 按键排序
    """
    if not url:
        return ""
    url = url.strip()
    # Bare domain URL (e.g. "example.com/path") -> urlsplit yields empty netloc,
    # producing invalid "https:example.com/path". Prepend protocol when the
    # first segment looks like a domain (contains a dot).
    if "://" not in url and not url.startswith("//") and "." in url.split("/", 1)[0]:
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    # 去默认端口
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]

    # path 去尾斜杠 (根 / 保留)
    path = parts.path or "/"
    if len(path) > 1:
        path = _TRAILING_SLASH_RE.sub("", path) or "/"

    # query: 去 UTM, 排序
    if parts.query:
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if not _UTM_KEYS.match(k)]
        query = urlencode(kept)
    else:
        query = ""

    # fragment 丢弃
    return urlunsplit((scheme, netloc, path, query, ""))


def is_public_url(url: str) -> bool:
    """是否公网 URL (给 Jina 前置检查用)。保守判定: scheme http/https 且 host 非空。"""
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global and not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    except ValueError:
        pass
    return "." in host


def domain_of(url: str) -> str:
    """提取根域名 (去 www. 前缀, 小写)。供手册/账号库按域归类。"""
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host
