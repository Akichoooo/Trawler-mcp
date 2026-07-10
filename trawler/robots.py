"""robots — RFC 9309 robots.txt 合规层。

2026 关键背景: 35% top 10k 站点已加 AI-bot Disallow (GPTBot/ClaudeBot/PerplexityBot/CCBot)。
Trawler 作为 MCP 爬虫底座, 默认尊重 robots.txt (除非 force_refresh + 用户明示)。

设计:
- 进程内缓存 (domain → RobotFileParser), TTL 12h (robots.txt 改动不频繁)
- 抓取 robots.txt 用 httpx (轻量, 不走 patchright), 5s 超时
- 拿不到 robots.txt → 默认允许 (RFC 9309: 无 robots.txt = 全允许)
- 识别 AI-bot 专用指令: GPTBot/ClaudeBot/PerpendicularBot/CCBot 的 Disallow
- 逐请求决策记录到 audit (留痕, 便于事后审查合规性)

不阻塞主流程: 抓 robots.txt 失败 → 默认 ALLOW (保守放行, 让 fetcher 自然处理)。
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from trawler import config

log = logging.getLogger("trawler.robots")

# 进程内缓存: domain → (parser, fetched_ts)。TTL 12h。
_ROBOTS_CACHE: dict[str, tuple[RobotFileParser | None, float]] = {}
_ROBOTS_TTL = 12 * 3600.0

# Trawler 默认上报的 User-Agent (robots.txt 匹配用)
# 注意: 不冒充 Googlebot (法律风险), 用自己的标识。
DEFAULT_UA = "Trawler-MCP/0.1"

# 2026 常见 AI-bot Disallow 名单 (用于检测站点是否显式禁止 AI 爬取)
AI_BOT_UAS = (
    "GPTBot",
    "ClaudeBot",
    "Claude-Web",
    "CCBot",
    "PerplexityBot",
    "Google-Extended",
    "Bytespider",
    "FacebookBot",
)


def _origin_for_parts(parts) -> str:
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if not netloc:
        return ""
    return urlunsplit((scheme, netloc, "", "", ""))


def _robots_url_for(origin: str) -> str:
    """Build the origin-scoped robots.txt URL."""
    if "://" not in origin:
        origin = f"https://{origin}"
    return f"{origin.rstrip('/')}/robots.txt"


def _is_allowed_cached(origin: str, path: str, ua: str = DEFAULT_UA) -> bool | None:
    """查缓存。返回 True/False, 缓存未命中或过期 → None。"""
    cached = _ROBOTS_CACHE.get(origin)
    if cached is None:
        return None
    parser, fetched_ts = cached
    if time.monotonic() - fetched_ts > _ROBOTS_TTL:
        return None  # 过期
    if parser is None:
        return True  # 无 robots.txt = 全允许 (RFC 9309)
    return parser.can_fetch(ua, path)


def _parse_robots_txt(origin: str, text: str) -> RobotFileParser:
    """解析 robots.txt 文本。返回 RobotFileParser (已 parse)。"""
    rp = RobotFileParser()
    rp.set_url(_robots_url_for(origin))
    rp.parse(text.splitlines())
    return rp


def _limited_response_text(resp) -> str:
    content = resp.content or b""
    if len(content) > config.ROBOTS_MAX_BYTES:
        log.warning(
            "robots.txt too large for %s, truncating to %d bytes",
            resp.url,
            config.ROBOTS_MAX_BYTES,
        )
        content = content[: config.ROBOTS_MAX_BYTES]
    encoding = resp.encoding or "utf-8"
    return content.decode(encoding, errors="replace")


async def _fetch_robots_txt(origin: str, use_proxy: bool = False) -> str | None:
    """抓 robots.txt。返回: 文本 / None (404=无 robots) / "__FETCH_FAILED__" (403/5xx 临时不可达)。

    SSRF 防御: 禁用自动重定向, 逐跳 SSRF 检查 (robots.txt 也可能 302 到内网)。
    """
    import httpx
    # 禁用重定向: 防止 robots.txt 302 → 内网 (SSRF)
    client_kwargs: dict = {"timeout": 5.0, "follow_redirects": False}
    if use_proxy:
        proxy = config.HTTPS_PROXY or config.HTTP_PROXY
        if proxy:
            client_kwargs["proxy"] = proxy
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            robots_url = _robots_url_for(origin)
            from trawler import ssrf

            if await ssrf.is_blocked_async(robots_url):
                return "__FETCH_FAILED__"
            resp = await client.get(robots_url)
            if resp.status_code == 200:
                return _limited_response_text(resp)
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                return None  # RFC 9309: unavailable 4xx = no robots rules
            if resp.status_code in (301, 302, 303, 307, 308):
                # 逐跳跟随, 每跳 SSRF 检查
                from urllib.parse import urljoin

                current = str(resp.url)
                redirects = 0
                while resp.status_code in (301, 302, 303, 307, 308) and redirects < 5:
                    loc = resp.headers.get("location", "")
                    if not loc:
                        break
                    next_url = urljoin(current, loc)
                    if await ssrf.is_blocked_async(next_url):
                        log.warning("robots.txt redirect SSRF blocked: %s → %s", current, next_url)
                        return "__FETCH_FAILED__"
                    current = next_url
                    resp = await client.get(current, follow_redirects=False)
                    redirects += 1
                if resp.status_code == 200:
                    return _limited_response_text(resp)
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    return None
            return "__FETCH_FAILED__"
    except Exception as e:
        log.debug("fetch robots.txt for %s failed: %s", origin, e)
        return "__FETCH_FAILED__"


async def is_allowed(url: str, *, use_proxy: bool = False, ua: str = DEFAULT_UA) -> bool:
    """检查 URL 是否被 robots.txt 允许。

    默认 True (允许) — 无 robots.txt 或抓取失败时保守放行。
    缓存命中且 parser 判定 Disallow → False。
    force_refresh 路径不走此检查 (上游 crawl_url 已决定)。
    """
    try:
        parts = urlsplit(url)
        domain = (parts.hostname or "").lower()
        origin = _origin_for_parts(parts)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
    except Exception:
        return True  # URL 异常, 不阻断

    if not domain or not origin:
        return True

    # 查缓存
    cached_result = _is_allowed_cached(origin, path, ua)
    if cached_result is not None:
        return cached_result

    # 缓存未命中/过期: 抓 robots.txt
    text = await _fetch_robots_txt(origin, use_proxy=use_proxy)
    if text is None:
        # 404 = 无 robots.txt (RFC 9309 全允许), 缓存 12h
        _ROBOTS_CACHE[origin] = (None, time.monotonic())
        return True
    if text == "__FETCH_FAILED__":
        # 5xx/network/too many redirects = temporarily unavailable. Default fail-closed.
        _ROBOTS_CACHE.pop(origin, None)
        return not config.ROBOTS_FAIL_CLOSED

    parser = _parse_robots_txt(origin, text)
    _ROBOTS_CACHE[origin] = (parser, time.monotonic())
    return parser.can_fetch(ua, path)


def is_ai_bot_disallowed(domain: str) -> bool:
    """检测该域是否对 AI-bot (GPTBot/ClaudeBot 等) 显式 Disallow。

    仅供审计/日志用: Trawler 自身默认 UA 是 Trawler-MCP, 不冒充 AI-bot,
    但若站点对 * 通配 Disallow, Trawler 也应尊重。
    返回 True 表示站点禁止 AI 爬取 (信息性, 不阻断)。
    """
    cached = _ROBOTS_CACHE.get(domain) or _ROBOTS_CACHE.get(f"https://{domain}")
    if cached is None:
        return False
    parser, _ = cached
    if parser is None:
        return False
    # 检查任意 AI-bot UA 是否被 Disallow: /
    for ai_ua in AI_BOT_UAS:
        if not parser.can_fetch(ai_ua, "/"):
            return True
    return False


def clear_cache(domain: str | None = None) -> None:
    """清缓存 (测试用, 或站点 robots.txt 更新后强制刷新)。"""
    if domain is None:
        _ROBOTS_CACHE.clear()
    else:
        _ROBOTS_CACHE.pop(domain, None)
        if "://" not in domain:
            _ROBOTS_CACHE.pop(f"https://{domain}", None)
            _ROBOTS_CACHE.pop(f"http://{domain}", None)
