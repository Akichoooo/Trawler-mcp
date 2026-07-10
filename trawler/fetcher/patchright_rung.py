"""patchright_rung — rung1, 主力 fetcher。

移植自 fish playwright_gear, 改造:
- patchright headless, channel="chrome", no_viewport (官方建议)
- storage_state 读态注入 (并发安全, 非整个 profile)
- 不自加 UA (patchright 官方建议, 自加反暴露)
- 拿到响应过 3 层检测 + 短路决策
- 不再 tenacity 自动重试 (改阶梯短路)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from trawler import artifacts, config

log = logging.getLogger("trawler.fetcher.patchright")

# patchright 优先, 不可用降级 playwright (反检测能力下降但能跑)
try:
    from patchright.async_api import Browser, BrowserContext, async_playwright
    _ANTI_DETECT = True
except ImportError:
    try:
        from playwright.async_api import Browser, BrowserContext, async_playwright  # type: ignore
        _ANTI_DETECT = False
        log.warning("patchright not installed, falling back to playwright (no anti-detect)")
    except ImportError:
        async_playwright = None  # type: ignore
        Browser = None  # type: ignore
        BrowserContext = None  # type: ignore
        _ANTI_DETECT = False

PATCHRIGHT_AVAILABLE = async_playwright is not None

# browserforge: 生成统计分布一致的指纹组 (UA + screen + WebGL + canvas + headers)。
# 关键: 不用 UA 列表 (矛盾会暴露), 而是用 browserforge 生成整套一致性组。
# 可选依赖 (heavy extra), 不可用则 patchright 用默认指纹 (反检测能力下降但不崩)。
try:
    from browserforge.fingerprint import FingerprintGenerator
    _BROWSERFORGE_AVAILABLE = True
    _fp_gen = FingerprintGenerator()
except ImportError:
    _BROWSERFORGE_AVAILABLE = False
    _fp_gen = None  # type: ignore


def _resolve_proxy(use_proxy: bool, proxy_url: str | None = None) -> str | None:
    """解析代理 URL。use_proxy=True 且 config.HTTP_PROXY/HTTPS_PROXY 有值才用。"""
    if not use_proxy:
        return None
    if proxy_url:
        return proxy_url
    # 优先 HTTPS_PROXY (爬取多为 https), 其次 HTTP_PROXY
    return config.HTTPS_PROXY or config.HTTP_PROXY or None

_GLOBAL_PLAYWRIGHT = None
_BROWSER_POOL = {} # (domain, safe_ip) -> Browser
_BROWSER_POOL_KEYS = [] # Keep track of order for LRU

_CONTEXT_POOL = {} # key -> BrowserContext
_CONTEXT_POOL_KEYS = [] # Keep track of order for LRU
_CONTEXT_ROUTE_KEYS = set()
_FINGERPRINT_POOL: dict[tuple[str, str, bool], tuple[str, str, str]] = {}

_POOL_LOCK = None

# 模块级 DNS 缓存 (替代函数内每次 new_context 重建的形同虚设版本):
# hostname → (cached_ts, is_blocked, safe_ip)。TTL 5min。
# route_handler 并发读写的脏读会致 SSRF 绕过, 故加 asyncio.Lock 保护复合操作。
_DNS_CACHE: dict[str, tuple[float, bool, str | None]] = {}
_DNS_CACHE_TTL = float(getattr(config, "BROWSER_ROUTE_DNS_CACHE_TTL", 15.0))
_DNS_CACHE_LOCK: asyncio.Lock | None = None

_BROWSER_SEMAPHORE: asyncio.Semaphore | None = None


def _get_browser_semaphore() -> asyncio.Semaphore:
    global _BROWSER_SEMAPHORE
    if _BROWSER_SEMAPHORE is None:
        _BROWSER_SEMAPHORE = asyncio.Semaphore(config.MAX_BROWSER_CONCURRENCY)
    return _BROWSER_SEMAPHORE


def _check_memory_available_mb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 * 1024)
    except Exception:
        return 4096.0



def _get_dns_cache_lock() -> asyncio.Lock:
    global _DNS_CACHE_LOCK
    if _DNS_CACHE_LOCK is None:
        _DNS_CACHE_LOCK = asyncio.Lock()
    return _DNS_CACHE_LOCK


async def _check_hostname_blocked(url: str) -> bool:
    """带缓存的 hostname SSRF 检查。返回 True = 阻断。

    模块级缓存 + asyncio.Lock 防 route_handler 并发脏读:
    A 协程解析出 blocked=True 但写入前 B 协程读到旧值 False → SSRF 绕过。
    """
    import time
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""
    now = time.monotonic()
    # 快速路径: 不持锁读 (CPython dict.get 原子), TTL 内直接返回
    cached = _DNS_CACHE.get(hostname)
    if cached:
        cached_ts, is_blocked, _ = cached
        if is_blocked and now - cached_ts < _DNS_CACHE_TTL:
            return is_blocked
    # 慢路径: 持锁解析并写缓存, 防并发重复解析 + 脏读
    async with _get_dns_cache_lock():
        # double-check (持锁后再查一次, 可能已被其他协程填好); 重新取 now 防锁竞争延迟
        now = time.monotonic()
        cached = _DNS_CACHE.get(hostname)
        if cached:
            cached_ts, is_blocked, _ = cached
            if is_blocked and now - cached_ts < _DNS_CACHE_TTL:
                return is_blocked
        from trawler import ssrf
        is_blocked, safe_ip = await ssrf.resolve_and_check_async(url)
        now_mono = time.monotonic()
        if is_blocked:
            _DNS_CACHE[hostname] = (now_mono, is_blocked, safe_ip)
        # Eviction: 清理过期条目防无界增长。若清理后仍超标则采用 FIFO 强行淘汰最老的前 200 条
        if len(_DNS_CACHE) > 1000:
            stale = [k for k, v in _DNS_CACHE.items() if now_mono - v[0] > _DNS_CACHE_TTL * 2]
            for k in stale:
                _DNS_CACHE.pop(k, None)
            if len(_DNS_CACHE) > 1000:
                # CPython 3.7+ 保证 dict 的插入顺序，直接切片拿到最老的 keys
                oldest_keys = list(_DNS_CACHE.keys())[:200]
                for k in oldest_keys:
                    _DNS_CACHE.pop(k, None)
        return is_blocked


def _get_pool_lock() -> asyncio.Lock:
    global _POOL_LOCK
    if _POOL_LOCK is None:
        _POOL_LOCK = asyncio.Lock()
    return _POOL_LOCK

async def _get_browser_from_pool(domain: str | None, safe_ip: str | None) -> Browser:
    global _GLOBAL_PLAYWRIGHT, _BROWSER_POOL, _BROWSER_POOL_KEYS
    async with _get_pool_lock():
        if _GLOBAL_PLAYWRIGHT is None:
            _GLOBAL_PLAYWRIGHT = await async_playwright().start()

        key = (domain, safe_ip)
        if key in _BROWSER_POOL:
            # Move to end (MRU)
            _BROWSER_POOL_KEYS.remove(key)
            _BROWSER_POOL_KEYS.append(key)
            return _BROWSER_POOL[key]

        # Evict oldest if pool is full
        if len(_BROWSER_POOL) >= 5:
            oldest_key = _BROWSER_POOL_KEYS.pop(0)
            oldest_browser = _BROWSER_POOL.pop(oldest_key)
            try:
                await oldest_browser.close()
            except Exception:
                pass
            # 注销 stale 引用, 防 _active_browsers 累积已 close 的 Browser (日志误报 + 内存泄漏)
            try:
                from trawler import signals
                signals.unregister_browser(oldest_browser)
            except Exception:
                pass

        # Launch new browser with host-resolver-rules if safe_ip is provided
        # 注入 WebRTC 禁用策略防刺探
        args = ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
        if safe_ip and domain:
            args.append(f"--host-resolver-rules=MAP {domain} {safe_ip}")

        launch_kwargs = {"headless": True, "args": args}
        try:
            browser = await _GLOBAL_PLAYWRIGHT.chromium.launch(channel="chrome", **launch_kwargs)
        except Exception:
            browser = await _GLOBAL_PLAYWRIGHT.chromium.launch(**launch_kwargs)

        try:
            from trawler import signals
            signals.register_browser(browser)
        except Exception:
            pass

        _BROWSER_POOL[key] = browser
        _BROWSER_POOL_KEYS.append(key)
        return browser

async def shutdown_browser() -> None:
    global _GLOBAL_PLAYWRIGHT, _BROWSER_POOL, _BROWSER_POOL_KEYS, _CONTEXT_ROUTE_KEYS
    async with _get_pool_lock():
        for context in _CONTEXT_POOL.values():
            try:
                await context.close()
            except Exception:
                pass
        _CONTEXT_POOL.clear()
        _CONTEXT_POOL_KEYS.clear()
        _CONTEXT_ROUTE_KEYS.clear()

        for browser in _BROWSER_POOL.values():
            try:
                await browser.close()
            except Exception:
                pass
            try:
                from trawler import signals
                signals.unregister_browser(browser)
            except Exception:
                pass
        _BROWSER_POOL.clear()
        _BROWSER_POOL_KEYS.clear()
        if _GLOBAL_PLAYWRIGHT:
            try:
                await _GLOBAL_PLAYWRIGHT.stop()
            except Exception:
                pass
            _GLOBAL_PLAYWRIGHT = None

def sync_shutdown_browser() -> None:
    """供 atexit 或信号处理同步调用的清理函数。"""
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        loop.run_until_complete(shutdown_browser())
        loop.close()
    except Exception:
        pass

import atexit  # noqa: E402

atexit.register(sync_shutdown_browser)

@dataclass
class FetchResult:
    """单档 fetcher 的结果。"""
    html: str = ""
    http_status: int = 0
    final_url: str = ""
    ok: bool = False
    error: str = ""
    artifact_id: str = ""
    console_messages: list[dict[str, Any]] = field(default_factory=list)
    request_failures: list[dict[str, Any]] = field(default_factory=list)


def _build_fingerprint_init_script(fp) -> str:
    """从 browserforge Fingerprint 构造 init script, 注入一致性指纹。

    覆盖: navigator.platform, hardwareConcurrency, deviceMemory, screen 尺寸,
    WebGL vendor/renderer, navigator.plugins (关键: headless Chrome 默认空 plugins 是暴露点)。

    注意: patchright 已处理 navigator.webdriver, 这里补 browserforge 的分布一致性。
    """
    import json
    nav = fp.navigator
    screen = fp.screen
    # 安全取值: 全部用 json.dumps 包装, 防 browserforge 返回 None 时 f-string 生成无效 JS "None"
    platform = json.dumps(getattr(nav, "platform", "Win32"))
    hardware_concurrency = json.dumps(getattr(nav, "hardwareConcurrency", 8) or 8)
    device_memory = json.dumps(getattr(nav, "deviceMemory", 8) or 8)
    screen_w = json.dumps(getattr(screen, "width", 1920) if screen else 1920)
    screen_h = json.dumps(getattr(screen, "height", 1080) if screen else 1080)
    avail_w = json.dumps(getattr(screen, "availWidth", 1920) if screen else 1920)
    avail_h = json.dumps(getattr(screen, "availHeight", 1040) if screen else 1040)
    color_depth = json.dumps(getattr(screen, "colorDepth", 24) if screen else 24)
    # WebGL vendor/renderer (browserforge 生成一致组, 不矛盾)
    webgl_vendor = "Google Inc. (NVIDIA)"  # 默认安全值
    webgl_renderer = "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)"
    if hasattr(fp, "webGl"):
        webgl_vendor = getattr(fp.webGl, "vendor", webgl_vendor) or webgl_vendor
        webgl_renderer = getattr(fp.webGl, "renderer", webgl_renderer) or webgl_renderer
    return f"""
(() => {{
  Object.defineProperty(navigator, 'platform', {{ get: () => {platform} }});
  Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {hardware_concurrency} }});
  Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {device_memory} }});
  Object.defineProperty(screen, 'width', {{ get: () => {screen_w} }});
  Object.defineProperty(screen, 'height', {{ get: () => {screen_h} }});
  Object.defineProperty(screen, 'availWidth', {{ get: () => {avail_w} }});
  Object.defineProperty(screen, 'availHeight', {{ get: () => {avail_h} }});
  Object.defineProperty(screen, 'colorDepth', {{ get: () => {color_depth} }});
  // WebGL 指纹一致性
  try {{
    const _getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {{
      if (p === 37445) return {json.dumps(webgl_vendor)};
      if (p === 37446) return {json.dumps(webgl_renderer)};
      return _getParameter.call(this, p);
    }};
  }} catch(e) {{}}
  // navigator.plugins: headless 默认空数组是暴露点, 注入假 plugins
  try {{
    const fakePlugins = [
      {{name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}},
      {{name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}},
      {{name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}},
      {{name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}},
      {{name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}}
    ];
    Object.defineProperty(navigator, 'plugins', {{
      get: () => fakePlugins,
      configurable: true
    }});
  }} catch(e) {{}}
}})();
"""


def _get_sticky_fingerprint(identity_key: tuple[str, str, bool]) -> tuple[str, str, str] | None:
    """Return a stable browserforge fingerprint payload for a browser identity."""
    if not _BROWSERFORGE_AVAILABLE:
        return None
    cached = _FINGERPRINT_POOL.get(identity_key)
    if cached is not None:
        return cached
    try:
        fp = _fp_gen.generate()
        payload = (
            fp.navigator.userAgent,
            fp.navigator.language or "en-US",
            _build_fingerprint_init_script(fp),
        )
        _FINGERPRINT_POOL[identity_key] = payload
        if len(_FINGERPRINT_POOL) > 1000:
            oldest_key = next(iter(_FINGERPRINT_POOL))
            _FINGERPRINT_POOL.pop(oldest_key, None)
        return payload
    except Exception as e:
        log.debug("browserforge fingerprint generation failed: %s", e)
        return None


async def _inject_bezier_movement(page) -> None:
    """人类行为拟真: 基于贝塞尔曲线的光标漂移与肌肉抖动"""
    import random
    try:
        # Get viewport size, default to 1920x1080 if not available
        vp = page.viewport_size or {"width": 1920, "height": 1080}
        width, height = vp["width"], vp["height"]
        
        start_x, start_y = random.randint(0, width), random.randint(0, height)
        end_x, end_y = random.randint(0, width), random.randint(0, height)
        
        # Simple bezier curve simulation using random intermediate points
        ctrl_x = random.randint(min(start_x, end_x), max(start_x, end_x) + 1)
        ctrl_y = random.randint(min(start_y, end_y), max(start_y, end_y) + 1)
        
        await page.mouse.move(start_x, start_y)
        
        # Simulate 10 steps along the quadratic bezier curve
        steps = 10
        for i in range(1, steps + 1):
            t = i / steps
            # Quadratic bezier formula
            x = (1 - t)**2 * start_x + 2 * (1 - t) * t * ctrl_x + t**2 * end_x
            y = (1 - t)**2 * start_y + 2 * (1 - t) * t * ctrl_y + t**2 * end_y
            
            # Add small muscle tremors
            tremor_x = random.uniform(-2, 2)
            tremor_y = random.uniform(-2, 2)
            
            await page.mouse.move(x + tremor_x, y + tremor_y, steps=2)
            await asyncio.sleep(random.uniform(0.01, 0.05))
            
        # Optional: Scroll a bit
        await page.mouse.wheel(0, random.randint(100, 500))
        await asyncio.sleep(random.uniform(0.1, 0.3))
    except Exception as e:
        log.debug("Bezier movement simulation failed: %s", e)


async def _safe_page_content(page) -> str:
    try:
        return await page.content()
    except Exception:
        return ""


async def _safe_page_screenshot(page) -> bytes | None:
    try:
        return await page.screenshot(full_page=True, timeout=5000)
    except Exception as e:
        log.debug("artifact screenshot failed: %s", e)
        return None


async def fetch(
    url: str,
    *,
    storage_state_path: str | None = None,
    timeout: int | None = None,
    bypass_l3: bool = False,
    use_proxy: bool = False,
    wait_strategy: str = "domcontentloaded",
    safe_ip: str | None = None,
    session_id: str | None = None,
    proxy_url: str = "",
    account_id: str = "",
    capture_artifact: bool = False,
    wait_for_selector: str = "",
) -> FetchResult:
    """patchright headless 抓取单页。返回 FetchResult (含 detect 决策)。"""
    if not PATCHRIGHT_AVAILABLE:
        return FetchResult(ok=False, error="patchright/playwright not installed (install [heavy] extra)")

    # 内存保护: 可用内存低于阀值跳过 patchright_rung 降级到 Jina
    avail_mem = _check_memory_available_mb()
    if avail_mem < config.MEM_SAFETY_THRESHOLD_MB:
        log.warning("Memory usage too high (%.1f MB avail < %d MB limit), skipping patchright_rung", avail_mem, config.MEM_SAFETY_THRESHOLD_MB)
        return FetchResult(ok=False, error=f"Host RAM low: {avail_mem:.1f}MB available")

    timeout = timeout or config.PATCHRIGHT_TIMEOUT
    result = FetchResult()
    console_messages: list[dict[str, Any]] = []
    request_failures: list[dict[str, Any]] = []
    page = None

    async with _get_browser_semaphore():
        try:
            domain = urlparse(url).hostname or ""
            browser = await _get_browser_from_pool(domain, safe_ip)
            
            ctx_kwargs = {"no_viewport": True, "service_workers": "block"}
            if storage_state_path:
                ctx_kwargs["storage_state"] = storage_state_path

            proxy_server = _resolve_proxy(use_proxy, proxy_url)
            if proxy_server:
                ctx_kwargs["proxy"] = {"server": proxy_server}

            init_script = None
            session_identity = session_id or domain
            fingerprint_key = (session_identity, proxy_server or "", bool(storage_state_path))
            fingerprint = _get_sticky_fingerprint(fingerprint_key)
            if fingerprint is not None:
                user_agent, locale, init_script = fingerprint
                ctx_kwargs["user_agent"] = user_agent
                ctx_kwargs["locale"] = locale

            ctx_key = (id(browser), proxy_server, storage_state_path, fingerprint_key)
            
            async with _get_pool_lock():
                if ctx_key in _CONTEXT_POOL:
                    _CONTEXT_POOL_KEYS.remove(ctx_key)
                    _CONTEXT_POOL_KEYS.append(ctx_key)
                    context = _CONTEXT_POOL[ctx_key]
                    if not storage_state_path:
                        try:
                            await context.clear_cookies()
                        except Exception:
                            pass
                    is_new_context = False
                else:
                    if len(_CONTEXT_POOL) >= 10:
                        old_key = _CONTEXT_POOL_KEYS.pop(0)
                        old_ctx = _CONTEXT_POOL.pop(old_key)
                        _CONTEXT_ROUTE_KEYS.discard(old_key)
                        try:
                            await old_ctx.close()
                        except Exception:
                            pass
                    context = await browser.new_context(**ctx_kwargs)
                    _CONTEXT_POOL[ctx_key] = context
                    _CONTEXT_POOL_KEYS.append(ctx_key)
                    is_new_context = True

            if is_new_context and init_script:
                try:
                    await context.add_init_script(init_script)
                except Exception as e:
                    log.debug("fingerprint init script injection failed: %s", e)

            async def route_handler(route):
                req = route.request
                url_str = req.url
                if url_str.startswith(("http://", "https://", "ws://", "wss://")):
                    if await _check_hostname_blocked(url_str):
                        log.warning("SSRF blocked in route interceptor: %s", url_str)
                        await route.abort("blockedbyclient")
                        return

                await route.continue_()

            async with _get_pool_lock():
                should_register_route = ctx_key not in _CONTEXT_ROUTE_KEYS
                if should_register_route:
                    _CONTEXT_ROUTE_KEYS.add(ctx_key)
            if should_register_route:
                await context.route("**/*", route_handler)

            try:
                page = await context.new_page()

                def on_console(msg) -> None:
                    if len(console_messages) >= 50:
                        return
                    try:
                        console_messages.append({
                            "type": getattr(msg, "type", ""),
                            "text": (getattr(msg, "text", "") or "")[:2000],
                            "location": getattr(msg, "location", None) or {},
                        })
                    except Exception:
                        pass

                def on_request_failed(req) -> None:
                    if len(request_failures) >= 50:
                        return
                    try:
                        failure = getattr(req, "failure", None)
                        if callable(failure):
                            failure = failure()
                        request_failures.append({
                            "url": getattr(req, "url", ""),
                            "method": getattr(req, "method", ""),
                            "resource_type": getattr(req, "resource_type", ""),
                            "failure": failure or "",
                        })
                    except Exception:
                        pass

                page.on("console", on_console)
                page.on("requestfailed", on_request_failed)
                resp = await page.goto(url, wait_until=wait_strategy, timeout=timeout * 1000)
                
                await _inject_bezier_movement(page)
                
                # 优先用传入的动态内容选择器 (site_rule.wait_for_selector),
                # 没有则退回通用选择器。timeout 提到 8s 给动态内容更多渲染时间。
                selector_to_wait = wait_for_selector or "main, article, [role='main']"
                try:
                    await page.wait_for_selector(selector_to_wait, timeout=8000, state="attached")
                except Exception:
                    pass
                    
                html = await page.content()
                status = resp.status if resp else 200
                
                if status in (429, 503):
                    retry_after = resp.headers.get("retry-after") if resp else None
                    from trawler.errors import RateLimitError, parse_retry_after
                    delay = parse_retry_after(retry_after)
                    raise RateLimitError(delay)
                    
                # 提取 Cookies (含 cf_clearance) 存入 account_vault + curl_cffi session
                try:
                    cookies = await context.cookies([url])
                    if cookies:
                        from trawler import account_vault
                        account_vault.save_auto_cookies(
                            domain,
                            cookies,
                            session_id=session_id,
                            account_id=account_id,
                        )
                    cf_clearance = next((c["value"] for c in cookies if c["name"] == "cf_clearance"), None)
                    if cf_clearance:
                        from trawler.fetcher.curlcffi_rung import _DEFAULT_IMPERSONATE, _get_session
                        c_session = await _get_session(_DEFAULT_IMPERSONATE, proxy_server, session_id)
                        c_session.cookies.set("cf_clearance", cf_clearance, domain=domain)
                        log.info("Successfully extracted cf_clearance and injected to vault + curl_cffi session")
                except Exception as ce:
                    log.debug("Failed to extract cf_clearance: %s", ce)

                final_url = page.url
                result.html = html
                result.http_status = status
                result.final_url = final_url
                result.ok = True
                result.console_messages = list(console_messages)
                result.request_failures = list(request_failures)

                from trawler.fetcher import challenge_detect

                is_challenge = challenge_detect.is_challenge_page(html, status)
                reason = "patchright-challenge-page" if is_challenge else "patchright-success"
                if (
                    capture_artifact
                    or is_challenge
                    or artifacts.should_capture(success=True, url=url, reason=reason)
                ):
                    screenshot = await _safe_page_screenshot(page)
                    result.artifact_id = artifacts.save_artifact(
                        url=url,
                        reason=reason,
                        success=not is_challenge,
                        final_url=final_url,
                        http_status=status,
                        gear_used="patchright_headless",
                        session_id=session_id or "",
                        html=html,
                        screenshot=screenshot,
                        console_messages=console_messages,
                        request_failures=request_failures,
                        extra={"anti_detect": _ANTI_DETECT, "challenge_detected": is_challenge},
                    )
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass
        except Exception as e:
            from trawler.errors import RateLimitError
            if isinstance(e, RateLimitError):
                raise
            result.ok = False
            result.error = str(e)
            result.console_messages = list(console_messages)
            result.request_failures = list(request_failures)
            if page is not None:
                html = await _safe_page_content(page)
                screenshot = await _safe_page_screenshot(page)
                result.artifact_id = artifacts.save_artifact(
                    url=url,
                    reason=f"patchright-exception:{type(e).__name__}",
                    success=False,
                    final_url=getattr(page, "url", "") or url,
                    http_status=result.http_status,
                    gear_used="patchright_headless",
                    session_id=session_id or "",
                    html=html,
                    screenshot=screenshot,
                    console_messages=console_messages,
                    request_failures=request_failures,
                    extra={"anti_detect": _ANTI_DETECT, "error": str(e)[:2000]},
                )
            log.warning("patchright fetch failed for %s: %s", url, e)

    return result
