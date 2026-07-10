"""SSRF 守卫 — 拦截内网/环回/链路本地/云元数据。

Fetcher 最前端。命中包含本地网络 (127.x, 10.x, 192.168.x 等) 及云环境 metadata server (169.254.x)。
通过环境变量 `TRAWLER_ALLOW_LOCAL=1` 可 opt-in 放行测试。

注意: DNS 解析用 socket.getaddrinfo (同步阻塞)。在 async 上下文里应优先用
is_blocked_async (它把 getaddrinfo 丢到线程池)。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from trawler import config

log = logging.getLogger("trawler.ssrf")


def _resolve_ip(hostname: str) -> str | None:
    try:
        # 强制走底层 socket 解析，拿第一条 IPv4
        return socket.gethostbyname(hostname)
    except Exception:
        return None


def resolve_and_check(url: str) -> tuple[bool, str | None]:
    """同步解析 IP 并检查是否被屏蔽。返回 (is_blocked, safe_ip)。"""
    if config.ALLOW_LOCAL:
        return False, None
    try:
        parts = urlsplit(url)
    except ValueError:
        return True, None
    # 协议白名单: 只允许 http/https (防 gopher/ftp/dict/file SSRF)
    if parts.scheme.lower() not in ("http", "https"):
        return True, None
    host = parts.hostname
    if not host:
        return True, None
    # 字面量 IP 直接判
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(ip):
            return True, None
        return False, str(ip)
    except ValueError:
        pass
    # 域名 → DNS 解析 (同步, 阻塞)
    try:
        infos = socket.getaddrinfo(host, None)
        # 找第一个安全的 IPv4
        safe_ip = None
        saw_allowed_fake_ip = False
        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(addr)
                if _is_allowed_fake_dns_ip(ip):
                    saw_allowed_fake_ip = True
                    continue
                if _is_blocked_ip(ip):
                    return True, None # 只要发现内网 IP 就直接判定 SSRF
                if ip.version == 4 and not safe_ip:
                    safe_ip = str(ip)
            except ValueError:
                continue
        if safe_ip:
            return False, safe_ip
        if saw_allowed_fake_ip:
            return False, None
        return False, None
    except socket.gaierror:
        return False, None

def is_blocked(url: str) -> bool:
    """旧的同步检查"""
    blocked, _ = resolve_and_check(url)
    return blocked


# 模块级线程池 (替代函数属性懒初始化的竞态): import 时建好, 避免首次并发多协程争抢
# hasattr + 设值非原子导致建多个 executor。max_workers=10 足够 DNS 解析。
import atexit  # noqa: E402
import concurrent.futures  # noqa: E402

_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="ssrf-dns")
atexit.register(_DNS_EXECUTOR.shutdown, wait=False)

# DNS 结果缓存: hostname → (cached_ts, is_blocked, safe_ip)。TTL 60s 防 DNS 抖动重复解析。
# 读多写少, 用 dict 原子性 (CPython GIL 保证单次 get/set 原子, 复合操作靠 best-effort)。
_DNS_CACHE: dict[str, tuple[float, bool, str | None]] = {}
_DNS_CACHE_TTL = 60.0


async def resolve_and_check_async(url: str) -> tuple[bool, str | None]:
    """异步版本 — DNS 解析丢模块级线程池 + 3s 硬超时。返回 (is_blocked, safe_ip)。

    超时不再保守拦截 (旧逻辑 True/None 会误杀公网慢 DNS), 改为放行让 fetcher 自然失败。
    带 60s 结果缓存防同域重复解析。用 time.monotonic 算 TTL (防 NTP 跳变失真)。
    """
    if config.ALLOW_LOCAL:
        return False, None
    import time as _time
    loop = asyncio.get_running_loop()
    # 先查缓存 (60s TTL), 防 100 并发同域重复 DNS
    try:
        parts = urlsplit(url)
        hostname = parts.hostname or ""
    except ValueError:
        return True, None
    now = _time.monotonic()
    cached = _DNS_CACHE.get(hostname)
    if cached:
        cached_ts, is_blocked, safe_ip = cached
        if is_blocked and now - cached_ts < _DNS_CACHE_TTL:
            return is_blocked, safe_ip
        if not is_blocked:
            _DNS_CACHE.pop(hostname, None)
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(_DNS_EXECUTOR, resolve_and_check, url),
            timeout=3.0,
        )
        # 缓存结果 (无论 blocked 与否, 减少 DNS 压力)
        if result[0]:
            _DNS_CACHE[hostname] = (_time.monotonic(), result[0], result[1])
        # 周期清理: 每 100 次解析清一次过期条目 (防 dict 无限增长)
        if len(_DNS_CACHE) > 1000:
            stale = [
                k for k, v in _DNS_CACHE.items()
                if _time.monotonic() - v[0] > _DNS_CACHE_TTL * 2
            ]
            for k in stale:
                _DNS_CACHE.pop(k, None)
        return result
    except TimeoutError:
        if config.SSRF_DNS_TIMEOUT_FAIL_CLOSED:
            log.warning("DNS timeout for %s, blocking because SSRF fail-closed is enabled", url)
            return True, None
        log.warning("DNS timeout for %s, allowing because SSRF fail-closed is disabled", url)
        return False, None

async def is_blocked_async(url: str) -> bool:
    """旧的异步检查"""
    blocked, _ = await resolve_and_check_async(url)
    return blocked


# 链路本地 + 云元数据段 (除标准私网外, 重点防 169.254.169.254)
_BLOCKED_PREFIXES = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
]


def _fake_ip_networks() -> list[ipaddress._BaseNetwork]:
    networks: list[ipaddress._BaseNetwork] = []
    for raw in str(getattr(config, "SSRF_FAKE_IP_CIDRS", "") or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            networks.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            log.warning("invalid fake-ip CIDR ignored: %s", raw)
    return networks


def _is_configured_fake_dns_ip(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in network for network in _fake_ip_networks())


def _is_allowed_fake_dns_ip(ip) -> bool:
    if not getattr(config, "SSRF_ALLOW_FAKE_IP_DNS", False):
        return False
    return _is_configured_fake_dns_ip(ip)


def _is_blocked_ip(ip) -> bool:
    # 拆解 IPv4-mapped IPv6 (::ffff:a.b.c.d) 后再检查 IPv4 黑名单
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        if _is_blocked_ip(ip.ipv4_mapped):
            return True
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return True
    for net in _BLOCKED_PREFIXES:
        if ip in net:
            return True
    return False


def block_reason(url: str) -> str:
    from trawler.errors import format_error
    suggested_action = (
        "abort; only set TRAWLER_ALLOW_LOCAL=1 for explicitly approved local/internal crawling"
    )
    if _looks_like_fake_ip_dns_url(url):
        suggested_action = (
            "if this is a public domain behind proxy/TUN fake-ip DNS, set "
            "TRAWLER_SSRF_ALLOW_FAKE_IP_DNS=1 and configure TRAWLER_SSRF_FAKE_IP_CIDRS; "
            "otherwise abort"
        )
    return format_error(
        "blocked-ssrf",
        "Blocked non-public IP (SSRF guard).",
        suggestedAction=suggested_action,
    )


def _looks_like_fake_ip_dns_url(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    except Exception:
        return False

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            if _is_configured_fake_dns_ip(ipaddress.ip_address(info[4][0])):
                return True
        except (ValueError, IndexError):
            continue
    return False
