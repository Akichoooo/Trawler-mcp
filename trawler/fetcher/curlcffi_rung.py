"""curlcffi_rung — rung0, 轻量 TLS 伪装抓取。

在 patchright 之前的最低成本档: 用 curl_cffi 模拟 Chrome 的 JA3/JA4 TLS 指纹
+ HTTP/2 帧指纹, 拿到 HTML 后走 detect → parser。

适用: 非 JS 站点 (文档/维基/博客/RSS) → 秒级返回, 不启 Chromium。
不适用: SPA / 强反爬挑战页 (交给 patchright / HITL)。

降级: curl_cffi 未装 → 标记不可用, 跳到 patchright。
重试: 仅对瞬时错误 (429+Retry-After / 408 / 连接超时) 用 tenacity 指数退避,
  不破坏阶梯短路设计 (blocked/empty 不重试)。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass

from trawler import config
from trawler.fetcher.patchright_rung import FetchResult

log = logging.getLogger("trawler.fetcher.curlcffi")

# curl_cffi 优先; 不可用 → 标记, rung0 直接跳过到 patchright
try:
    from curl_cffi.requests import AsyncSession
    CURLCFFI_AVAILABLE = True
except ImportError:
    AsyncSession = None  # type: ignore
    CURLCFFI_AVAILABLE = False
    log.info("curl_cffi not installed, rung0 (curlcffi) disabled — install [core] deps")

# tenacity: 瞬时错误重试 (不破坏阶梯短路 — blocked/empty 不重试, 只重试网络瞬时错误)
try:
    from tenacity import (
        AsyncRetrying,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential_jitter,
    )
    TENACITY_AVAILABLE = True
except ImportError:
    TENACITY_AVAILABLE = False


class _TransientError(Exception):
    """瞬时错误: 值得重试 (429/408/连接超时)。区别于 blocked/empty (不重试)。"""
    def __init__(self, status: int, retry_after: float = 0.0):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"transient HTTP {status}, retry_after={retry_after}")


# impersonate 目标: 锁定一个稳定的 Chrome 版本 profile。
# curl_cffi 内置 chrome99-146 的 TLS+HTTP/2 指纹库; 131 是 2026 Q1 稳定版, JA4 匹配度高。
# 注意: profile 必须与 UA 一致, 否则 JA4 与 UA 矛盾反而暴露。这里 curl_cffi 自动一致。
_DEFAULT_IMPERSONATE = "chrome131"
# TLS 指纹池: chrome100~146 覆盖 2024-2026 主流版本, 按 session_id 粘性轮换。
# 粘性保证同会话指纹一致 (避免同会话 JA4 跳变暴露), 跨会话轮换分散特征。
_IMPERSONATE_POOL = [
    "chrome100", "chrome101", "chrome104", "chrome107", "chrome110",
    "chrome116", "chrome119", "chrome120", "chrome123", "chrome124",
    "chrome131", "chrome133", "chrome136", "chrome140", "chrome146",
]

_session_pool: dict[str, AsyncSession] = {}
_session_pool_keys: list[str] = []  # LRU 顺序追踪 (最老在前)
_SESSION_POOL_MAX = 10  # 池上限, 防无界内存增长 (违反硬约束的 bug 修复)
_session_lock = asyncio.Lock()

async def _get_session(
    impersonate: str,
    proxy: str | None,
    session_id: str | None = None,
) -> AsyncSession:
    """获取或创建一个缓存的 AsyncSession，按 impersonate/proxy/session_id 隔离。

    LRU 淘汰: 池满时关闭并移除最久未用的 session, 防无界内存增长。
    MRU 提升: 命中时移到末尾, 保持热会话存活。
    """
    if not CURLCFFI_AVAILABLE:
        raise RuntimeError("curl_cffi not installed")

    key = f"{impersonate}|{proxy or ''}|{session_id or ''}"
    async with _session_lock:
        if key in _session_pool:
            # MRU: 移到末尾
            _session_pool_keys.remove(key)
            _session_pool_keys.append(key)
            return _session_pool[key]
        # LRU 淘汰: 池满时关最老的
        if len(_session_pool) >= _SESSION_POOL_MAX:
            oldest_key = _session_pool_keys.pop(0)
            oldest_session = _session_pool.pop(oldest_key, None)
            if oldest_session is not None:
                try:
                    await oldest_session.close()
                except Exception:
                    pass
        _session_pool[key] = AsyncSession(impersonate=impersonate, verify=True)
        _session_pool_keys.append(key)
        return _session_pool[key]


async def shutdown_sessions() -> None:
    """关闭所有缓存的 curl_cffi session (供 signals/lifecycle 调用)。"""
    global _session_pool, _session_pool_keys
    async with _session_lock:
        for session in _session_pool.values():
            try:
                await session.close()
            except Exception:
                pass
        _session_pool.clear()
        _session_pool_keys.clear()


@dataclass
class _FetchConfig:
    impersonate: str = _DEFAULT_IMPERSONATE
    timeout: int = 15
    verify: bool = True


def _resolve_impersonate(session_id: str = "") -> str:
    """解析 impersonate target。

    优先级: env TRAWLER_CURLCFFI_IMPERSONATE (单值, 应急) > 指纹池轮换 > 默认 chrome131。
    池轮换: TRAWLER_CURLCFFI_FINGERPRINT_POOL=true 启用, 按 session_id 粘性选择。
    粘性保证同会话指纹一致 (JA4 不跳变), 跨会话分散特征。
    """
    import os
    explicit = os.getenv("TRAWLER_CURLCFFI_IMPERSONATE")
    if explicit:
        return explicit
    if os.getenv("TRAWLER_CURLCFFI_FINGERPRINT_POOL", "").lower() not in ("1", "true", "yes", "on"):
        return _DEFAULT_IMPERSONATE
    if not session_id:
        import random
        return random.choice(_IMPERSONATE_POOL)
    idx = int(hashlib.sha1(session_id.encode()).hexdigest()[:8], 16) % len(_IMPERSONATE_POOL)
    return _IMPERSONATE_POOL[idx]


async def fetch(
    url: str,
    *,
    use_proxy: bool = False,
    timeout: int | None = None,
    safe_ip: str | None = None,
    session_id: str | None = None,
    proxy_url: str = "",
    account_id: str = "",
) -> FetchResult:
    """curl_cffi 抓取。返回 FetchResult (含 html + http_status + final_url)。

    成功: result.ok=True, html 含原始 HTML (交 detect → parser)。
    瞬时错误: tenacity 重试 3 次 (429/408/连接超时)。
    blocked/empty: 不重试, 返回 ok=False 交上层阶梯降级。
    SSRF: 上层 crawl_url 已在最前 resolve_and_check, 这里不再重复 (route 级拦截在 patchright)。
    """
    result = FetchResult()

    if not CURLCFFI_AVAILABLE:
        result.ok = False
        result.error = "curl_cffi not installed"
        return result

    timeout_val = timeout or config.JINA_TIMEOUT  # 复用 15s 默认 (curl_cffi 比 patchright 快得多)
    impersonate = _resolve_impersonate(session_id or "")

    proxy = None
    if use_proxy:
        proxy = proxy_url or config.HTTPS_PROXY or config.HTTP_PROXY or None

    async def _do_fetch() -> FetchResult:
        try:
            session = await _get_session(impersonate, proxy, session_id)
            # SSRF 防御: 禁用自动重定向, 手动逐跳检查。
            # allow_redirects=True 会在 SSRF 二次检查前跟随到内网 (如 169.254.169.254 元数据端点),
            # 请求已发出 + 响应已下载 → SSRF 漏洞。逐跳检查在请求发出前拦截。
            # 自动加载 account_vault 回流 Cookies (如 cf_clearance)
            from urllib.parse import urlparse
            domain = urlparse(url).hostname or ""
            from trawler import account_vault
            auto_cookies = account_vault.get_auto_cookies(
                domain,
                session_id=session_id,
                account_id=account_id,
            )
            if auto_cookies:
                for ck, cv in auto_cookies.items():
                    try:
                        session.cookies.set(ck, cv, domain=domain)
                    except Exception:
                        pass

            def _is_literal_ip(host: str) -> bool:
                import ipaddress

                try:
                    ipaddress.ip_address(host)
                    return True
                except ValueError:
                    return False

            async def _safe_ip_for_request(target_url: str, pinned_ip: str | None) -> str | None:
                from trawler import ssrf

                if pinned_ip:
                    return pinned_ip
                blocked, checked_ip = await ssrf.resolve_and_check_async(target_url)
                if blocked:
                    result.ok = False
                    result.error = f"SSRF blocked: redirect to {target_url}"
                    log.warning("curl_cffi SSRF blocked before request: %s", target_url)
                    return None
                return checked_ip

            def _request_kwargs(target_url: str, pinned_ip: str | None) -> dict:
                kwargs: dict = {"timeout": timeout_val, "allow_redirects": False}
                if proxy:
                    kwargs["proxies"] = {"https": proxy, "http": proxy}
                kwargs["headers"] = {"Accept-Encoding": "gzip, deflate, br, zstd"}
                if pinned_ip:
                    from urllib.parse import urlsplit as _urlsplit

                    parts = _urlsplit(target_url)
                    host = parts.hostname or ""
                    port = parts.port or (443 if parts.scheme == "https" else 80)
                    kwargs["resolve"] = [f"{host}:{port}:{pinned_ip}"]
                return kwargs

            current_url = url
            current_safe_ip = await _safe_ip_for_request(current_url, safe_ip)
            if result.error:
                return result
            if proxy:
                from urllib.parse import urlsplit as _urlsplit

                host = _urlsplit(current_url).hostname or ""
                if not current_safe_ip and host and not _is_literal_ip(host):
                    result.ok = False
                    result.error = f"SSRF blocked: unresolved proxy target {current_url}"
                    return result
            resp = await session.get(current_url, **_request_kwargs(current_url, current_safe_ip))
            # 手动跟随重定向, 每跳发请求前做 SSRF 检查
            redirects_followed = 0
            while resp.status_code in (301, 302, 303, 307, 308) and redirects_followed < 5:
                location = resp.headers.get("Location") or resp.headers.get("location") or ""
                if not location:
                    break
                from urllib.parse import urljoin
                next_url = urljoin(current_url, location)
                # 逐跳 SSRF 检查: 请求发出前验证目标并为该跳重建 DNS pin.
                next_safe_ip = await _safe_ip_for_request(next_url, None)
                if result.error:
                    log.warning("curl_cffi SSRF blocked redirect: %s → %s", current_url, next_url)
                    return result
                if proxy:
                    from urllib.parse import urlsplit as _urlsplit

                    host = _urlsplit(next_url).hostname or ""
                    if not next_safe_ip and host and not _is_literal_ip(host):
                        result.ok = False
                        result.error = f"SSRF blocked: unresolved proxy redirect target {next_url}"
                        return result
                current_url = next_url
                current_safe_ip = next_safe_ip
                resp = await session.get(
                    current_url,
                    **_request_kwargs(current_url, current_safe_ip),
                )
                redirects_followed += 1
        except Exception as e:
            # 网络瞬时错误 (连接超时/DNS/重置) → 抛 _TransientError 触发 tenacity
            raise _TransientError(0) from e

        status = resp.status_code
        # 429/503 带 Retry-After → 瞬时, 重试
        if status in (429, 503):
            retry_after = _parse_retry_after(resp.headers.get("Retry-After") or resp.headers.get("retry-after"))
            raise _TransientError(status, retry_after)
        # 408 Request Timeout → 瞬时
        if status == 408:
            raise _TransientError(status)

        # 403/blocked/empty → 不重试, 交阶梯降级
        result.html = resp.text or ""
        result.http_status = status
        result.final_url = str(resp.url) if hasattr(resp, "url") else current_url
        result.ok = True
        return result

    # tenacity 包裹瞬时错误重试 (不破坏阶梯: blocked/empty 直接返回不进重试)
    if TENACITY_AVAILABLE:
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential_jitter(initial=1.0, max=10.0),
                retry=retry_if_exception_type(_TransientError),
                reraise=True,
            ):
                with attempt:
                    return await _do_fetch()
        except _TransientError as e:
            result.ok = False
            if e.status in (429, 503):
                # 触发域级 backoff (交上层 _domain_rate_limit_wait)
                from trawler.errors import RateLimitError
                raise RateLimitError(e.retry_after or 2.0)
            result.error = f"transient errors exhausted: {e}"
            return result
    else:
        # 无 tenacity: 单次不重试 (降级, 不破坏功能)
        try:
            return await _do_fetch()
        except _TransientError as e:
            result.ok = False
            if e.status in (429, 503):
                from trawler.errors import RateLimitError
                raise RateLimitError(e.retry_after or 2.0)
            result.error = f"transient error: {e}"
            return result

    return result


def _parse_retry_after(retry_after: str | None, default: float = 2.0, *, max_backoff: float = 300.0) -> float:
    """解析 Retry-After header (秒数 or HTTP-date)。复用 errors.parse_retry_after 逻辑。"""
    if not retry_after:
        return default
    try:
        return float(retry_after)
    except ValueError:
        import time
        from email.utils import parsedate_to_datetime
        try:
            dt = parsedate_to_datetime(retry_after)
            delay = dt.timestamp() - time.time()
            return max(0.0, min(delay, max_backoff))
        except Exception:
            return default
