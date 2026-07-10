"""hitl_rung — rung4, 人工兜底。

环境检测:
- 无 DISPLAY (无头 server) → 返回 HITL_REQUIRED_BUT_HEADLESS (不弹浏览器)
- 有 GUI → launch_persistent_context (headless=False) 弹可见浏览器, 人过 CAPTCHA
- 或接 CapSolver (TRAWLER_CAPSOLVER_KEY opt-in, 责任转移用户)

profile 读写分离:
- HITL 用 launch_persistent_context (独占写态, 串行)
- 抓到后导出 storage_state.json (供并发读态用)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from trawler import config
from trawler.fetcher.patchright_rung import PATCHRIGHT_AVAILABLE, FetchResult

log = logging.getLogger("trawler.fetcher.hitl")

# 域级串行锁: launch_persistent_context 对 profile_dir 加 Chromium SingletonLock,
# 两个并发 HITL 同域 → 第二个启动失败或挂死。每域一把锁串行化。
_hitl_locks: dict[str, asyncio.Lock] = {}


def _get_hitl_lock(domain: str) -> asyncio.Lock:
    """延迟初始化域级 HITL 锁 (模块级 asyncio.Lock 会绑错 loop)。"""
    if domain not in _hitl_locks:
        if len(_hitl_locks) > 1000:
            _hitl_locks.clear()
        _hitl_locks[domain] = asyncio.Lock()
    return _hitl_locks[domain]


def has_display() -> bool:
    """检测是否有 GUI 显示环境。"""
    if sys.platform == "win32":
        # 排除 Windows 服务；桌面 Codex/IDE 进程有时没有 SESSIONNAME，但仍可交互。
        session = os.environ.get("SESSIONNAME", "").lower()
        if session == "services":
            return False
        if session:
            return True
        try:
            import ctypes

            user32 = ctypes.windll.user32
            desktop = user32.OpenInputDesktop(0, False, 0x0001)
            if desktop:
                user32.CloseDesktop(desktop)
                return True
        except Exception:
            pass
        try:
            import psutil

            current_user = os.environ.get("USERNAME", "").lower()
            for process in psutil.process_iter(["name", "username"]):
                name = str(process.info.get("name") or "").lower()
                username = str(process.info.get("username") or "").lower()
                if name == "explorer.exe" and (not current_user or current_user in username):
                    return True
        except Exception:
            pass
        return False
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def is_capsolver_enabled() -> bool:
    return bool(config.CAPSOLVER_KEY)


async def fetch(
    url: str,
    *,
    domain: str,
    profile_dir: str,
    timeout: int | None = None,
    use_proxy: bool = False,
    safe_ip: str | None = None,
    session_id: str | None = None,
    proxy_url: str = "",
    account_id: str = "",
    capture_artifact: bool = False,
) -> FetchResult:
    """HITL 抓取。无 GUI → 失败返回。有 GUI → 弹浏览器等人过。

    成功后通过 account_vault.save_storage_state 写入 domain_dir/storage_state.json
    (而非 profile_dir 下, 避免路径错位读不回)。
    """
    if not PATCHRIGHT_AVAILABLE:
        return FetchResult(ok=False, error="patchright not installed for HITL")

    if not has_display():
        return FetchResult(
            ok=False,
            error="HITL_REQUIRED_BUT_HEADLESS",
        )

    # CapSolver 路径 (opt-in) — v1 留接口, 实现可后续补
    if is_capsolver_enabled():
        pass # v1 不实现自动过码, 留给 v2。仍走人工弹窗。

    timeout = timeout or config.HITL_TIMEOUT
    result = FetchResult()

    # 前置检查: HITL 需要持久化 storage_state, 无 VAULT_KEY 则无法保存登录态。
    # 旧逻辑: save_storage_state 抛 RuntimeError 覆盖 result.ok=True → 人工过码成功但标记失败。
    # 新逻辑: 无 vault 直接拒绝启动 HITL, 给明确错误。
    from trawler.account_vault import is_vault_enabled
    if not is_vault_enabled():
        return FetchResult(
            ok=False,
            error="HITL requires TRAWLER_VAULT_KEY to persist session. Set it before using HITL.",
        )

    try:
        from patchright.async_api import async_playwright  # type: ignore
    except ImportError:
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError:
            return FetchResult(ok=False, error="playwright not installed")

    # 域级串行: 防 launch_persistent_context 撞 SingletonLock (两个并发 HITL 同域会挂死)
    async with _get_hitl_lock(domain):
        try:
            async with async_playwright() as p:
                # 独占写态: launch_persistent_context (已被 _get_hitl_lock 串行化)
                args = ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
                if safe_ip:
                    args.append(f"--host-resolver-rules=MAP {domain} {safe_ip}")
                ctx_kwargs: dict = {"user_data_dir": profile_dir, "headless": False, "no_viewport": True, "service_workers": "block", "args": args}
                if use_proxy:
                    proxy = proxy_url or config.HTTPS_PROXY or config.HTTP_PROXY
                    if proxy:
                        ctx_kwargs["proxy"] = {"server": proxy}
                try:
                    context = await p.chromium.launch_persistent_context(channel="chrome", **ctx_kwargs)
                except Exception:
                    context = await p.chromium.launch_persistent_context(**ctx_kwargs)

                # 复用 patchright_rung 模块级 DNS 缓存 (asyncio.Lock + TTL 5min, 防并发脏读 SSRF 绕过)
                if safe_ip and domain:
                    import time as _time

                    from trawler.fetcher.patchright_rung import _DNS_CACHE, _get_dns_cache_lock
                    async with _get_dns_cache_lock():
                        _DNS_CACHE[domain] = (_time.monotonic(), False, safe_ip)

                # Rebinding 双向收口: 拦截所有外部 HTTP/HTTPS/WebSocket 请求，防范所有类型的 SSRF/Rebinding 绕过
                async def route_handler(route):
                    req = route.request
                    url = req.url
                    if url.startswith(("http://", "https://", "ws://", "wss://")):
                        from trawler.fetcher.patchright_rung import _check_hostname_blocked
                        if await _check_hostname_blocked(url):
                            log.warning("SSRF blocked in HITL route interceptor: %s", url)
                            await route.abort("blockedbyclient")
                            return

                    await route.continue_()

                await context.route("**/*", route_handler)

                # #9 注册到 signals — 注册 context 本身 (persistent context 的 .browser 返回 None,
                # 注册 context 让 signals._extract_process 走 context 分支拿底层进程)
                try:
                    from trawler import signals
                    signals.register_browser(context)
                except Exception:
                    pass

                try:
                    page = await context.new_page()
                    await page.goto(url, wait_until="domcontentloaded")

                    log.info("HITL browser opened for %s — waiting for human (timeout=%ds)", url, timeout)
                    # 等人过 CAPTCHA。轮询页面直到不再有挑战特征, 或超时
                    deadline = asyncio.get_event_loop().time() + timeout
                    passed = False
                    while asyncio.get_event_loop().time() < deadline:
                        await asyncio.sleep(3)
                        html = await page.content()
                        from trawler.fetcher import challenge_detect
                        if not challenge_detect.is_challenge_page(html):
                            # 挑战消失 = 人过了
                            passed = True
                            break

                    if not passed:
                        # #5 超时: 人没过, 必须返回失败 (不能 ok=True)
                        result.ok = False
                        result.error = "HITL_TIMEOUT: human did not solve challenge in time"
                        # 不导出 storage_state (没登录成功)
                        return result  # finally 会关 context

                    html = await page.content()
                    result.html = html
                    result.http_status = 200
                    result.final_url = page.url
                    result.ok = True
                    if capture_artifact:
                        screenshot = None
                        try:
                            screenshot = await page.screenshot(full_page=True, timeout=5000)
                        except Exception as screenshot_err:
                            log.debug("HITL screenshot failed for %s: %s", url, screenshot_err)
                        try:
                            from trawler import artifacts

                            result.artifact_id = artifacts.save_artifact(
                                url=url,
                                reason="hitl-success",
                                success=True,
                                final_url=page.url,
                                http_status=200,
                                gear_used="hitl",
                                session_id=session_id or "",
                                html=html,
                                screenshot=screenshot,
                            )
                        except Exception as artifact_err:
                            log.debug("HITL artifact capture failed for %s: %s", url, artifact_err)
                    # 导出 storage_state 通过 account_vault 写入正确路径
                    try:
                        state = await context.storage_state(indexed_db=True)
                    except TypeError:
                        state = await context.storage_state()
                    try:
                        from trawler.account_vault import save_storage_state
                        save_storage_state(domain, state, account_id=account_id or None)
                    except Exception as save_err:
                        log.warning("save_storage_state failed for %s (HTML still returned): %s", domain, save_err)
                    try:
                        cookies = await context.cookies([url])
                        if cookies:
                            from trawler.account_vault import save_auto_cookies

                            save_auto_cookies(
                                domain,
                                cookies,
                                session_id=session_id,
                                account_id=account_id or None,
                            )
                    except Exception as cookie_err:
                        log.debug("save HITL cookies failed for %s: %s", domain, cookie_err)

                finally:
                    await context.close()
                    # #4 注销浏览器句柄 (防 _active_browsers 无限增长)
                    try:
                        from trawler import signals
                        signals.unregister_browser(context)
                    except Exception:
                        pass
        except Exception as e:
            result.ok = False
            result.error = str(e)
            log.warning("HITL failed for %s: %s", url, e)

    return result
