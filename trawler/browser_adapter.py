"""Browser launch/connect adapters for live MCP workflows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from trawler import config, signals
from trawler.fetcher.patchright_rung import _check_hostname_blocked

log = logging.getLogger("trawler.browser_adapter")


@dataclass
class BrowserHandle:
    """Concrete browser resources behind the LiveBrowserSession seam."""

    adapter_name: str
    context: Any
    page: Any
    playwright: Any
    profile_dir: str = ""
    browser: Any = None
    route_guarded: bool = False
    owns_context: bool = True
    owns_page: bool = False
    owns_browser: bool = False


@dataclass
class LocalPersistentOptions:
    profile_dir: str
    domain: str
    url: str
    safe_ip: str | None = None
    use_proxy: bool = False
    proxy_url: str = ""
    wait_until: str = "domcontentloaded"
    timeout: int = 60


@dataclass
class CdpConnectOptions:
    cdp_url: str
    url: str = ""
    wait_until: str = "domcontentloaded"
    timeout: int = 60


async def start_async_playwright():
    try:
        from patchright.async_api import async_playwright  # type: ignore
    except ImportError:
        from playwright.async_api import async_playwright  # type: ignore

    return await async_playwright().start()


def is_allowed_cdp_endpoint(cdp_url: str) -> bool:
    """Allow local CDP endpoints by default; remote CDP requires explicit opt-in."""
    try:
        parts = urlsplit(cdp_url)
    except ValueError:
        return False
    if parts.scheme.lower() not in {"http", "https", "ws", "wss"}:
        return False
    host = (parts.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    return bool(getattr(config, "ALLOW_REMOTE_CDP", False))


async def install_ssrf_route_guard(context: Any) -> bool:
    """Install per-request SSRF blocking when the browser context supports routing."""

    async def route_handler(route):
        req_url = route.request.url
        if req_url.startswith(("http://", "https://", "ws://", "wss://")):
            if await _check_hostname_blocked(req_url):
                log.warning("SSRF blocked in live browser route interceptor: %s", req_url)
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    try:
        await context.route("**/*", route_handler)
        return True
    except Exception as e:
        log.warning("browser route guard unavailable for this context: %s", e)
        return False


async def open_local_persistent_browser(options: LocalPersistentOptions) -> BrowserHandle:
    playwright = await start_async_playwright()
    context = None
    try:
        args = ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
        if options.safe_ip:
            args.append(f"--host-resolver-rules=MAP {options.domain} {options.safe_ip}")
        ctx_kwargs: dict[str, Any] = {
            "user_data_dir": options.profile_dir,
            "headless": False,
            "no_viewport": True,
            "service_workers": "block",
            "args": args,
        }
        if options.use_proxy:
            proxy = options.proxy_url or config.HTTPS_PROXY or config.HTTP_PROXY
            if proxy:
                ctx_kwargs["proxy"] = {"server": proxy}
        try:
            context = await playwright.chromium.launch_persistent_context(
                channel="chrome",
                **ctx_kwargs,
            )
        except Exception:
            context = await playwright.chromium.launch_persistent_context(**ctx_kwargs)

        route_guarded = await install_ssrf_route_guard(context)
        try:
            signals.register_browser(context)
        except Exception:
            pass
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(
            options.url,
            wait_until=options.wait_until,
            timeout=options.timeout * 1000,
        )
        return BrowserHandle(
            adapter_name="local_persistent",
            context=context,
            page=page,
            playwright=playwright,
            profile_dir=options.profile_dir,
            route_guarded=route_guarded,
            owns_context=True,
        )
    except Exception:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        try:
            await playwright.stop()
        except Exception:
            pass
        raise


async def connect_cdp_browser(options: CdpConnectOptions) -> BrowserHandle:
    if not is_allowed_cdp_endpoint(options.cdp_url):
        raise PermissionError(
            "CDP endpoint must be localhost/127.0.0.1 unless TRAWLER_ALLOW_REMOTE_CDP=1"
        )

    playwright = await start_async_playwright()
    browser = None
    context = None
    page = None
    owns_context = False
    owns_page = False
    try:
        browser = await playwright.chromium.connect_over_cdp(
            options.cdp_url,
            timeout=options.timeout * 1000,
        )
        if browser.contexts:
            context = browser.contexts[0]
        else:
            context = await browser.new_context(service_workers="block")
            owns_context = True
        route_guarded = await install_ssrf_route_guard(context)
        try:
            signals.register_browser(context)
        except Exception:
            pass
        if context.pages:
            page = context.pages[0]
        else:
            page = await context.new_page()
            owns_page = True
        if options.url:
            await page.goto(
                options.url,
                wait_until=options.wait_until,
                timeout=options.timeout * 1000,
            )
        return BrowserHandle(
            adapter_name="cdp",
            context=context,
            page=page,
            playwright=playwright,
            profile_dir="",
            browser=browser,
            route_guarded=route_guarded,
            owns_context=owns_context,
            owns_page=owns_page,
            owns_browser=False,
        )
    except Exception:
        if owns_page and page is not None:
            try:
                await page.close()
            except Exception:
                pass
        if owns_context and context is not None:
            try:
                await context.close()
            except Exception:
                pass
        try:
            await playwright.stop()
        except Exception:
            pass
        raise


async def close_browser_handle(handle: BrowserHandle) -> None:
    try:
        signals.unregister_browser(handle.context)
    except Exception:
        pass

    if handle.owns_page:
        try:
            await handle.page.close()
        except Exception:
            pass
    if handle.owns_context:
        try:
            await handle.context.close()
        except Exception:
            pass
    elif handle.owns_browser and handle.browser is not None:
        try:
            await handle.browser.close()
        except Exception:
            pass
    try:
        await handle.playwright.stop()
    except Exception:
        pass
