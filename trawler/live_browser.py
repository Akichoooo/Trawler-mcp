"""Live human-operated browser sessions.

This module owns long-lived visible browser sessions for MCP workflows where a
human needs to log in, pass verification, click through UI, scroll, or otherwise
shape the current page before Trawler extracts content from that exact state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from trawler import (
    account_profiles,
    account_vault,
    artifacts,
    audit,
    browser_adapter,
    config,
    db,
    link_map,
    proxy_pool,
    ssrf,
    urlnorm,
)
from trawler.errors import format_error, format_ok
from trawler.fetcher import hitl_rung
from trawler.fetcher.patchright_rung import PATCHRIGHT_AVAILABLE
from trawler.parser import extract as parser_extract
from trawler.parser import fit_markdown as parser_fit_markdown
from trawler.parser import selectors as parser_selectors
from trawler.urlnorm import domain_of

log = logging.getLogger("trawler.live_browser")

EXTRACT_MODES = {
    "page",
    "visible_text",
    "selector",
    "screenshot",
    "html",
    "element_snapshot",
    "picked_element",
    "picked_region",
    "page_clone",
    "accessibility_snapshot",
    "visible_blocks",
    "fit_markdown",
    "bundle",
}
ACCESS_MODES = {"standard", "user_authorized"}
BROWSER_ACTION_TYPES = {
    "click",
    "fill",
    "type",
    "press",
    "scroll",
    "wait",
    "wait_for_selector",
    "goto",
    "check",
    "uncheck",
    "select_option",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class LiveBrowserSession:
    session_id: str
    domain: str
    start_url: str
    current_url: str
    context: Any
    page: Any
    playwright: Any
    profile_dir: str
    account_id: str = "default"
    proxy_url: str = ""
    access_mode: str = "user_authorized"
    adapter_name: str = "local_persistent"
    route_guarded: bool = True
    browser_handle: browser_adapter.BrowserHandle | None = None
    op_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    created_at: str = field(default_factory=_now_iso)
    last_used_at: str = field(default_factory=_now_iso)


@dataclass
class LiveBrowserExtraction:
    legacy_text: str
    structured: dict[str, Any]
    screenshot: bytes | None = None


_LIVE_SESSIONS: dict[str, LiveBrowserSession] = {}
_LIVE_LOCK: asyncio.Lock | None = None


def _get_live_lock() -> asyncio.Lock:
    global _LIVE_LOCK
    if _LIVE_LOCK is None:
        _LIVE_LOCK = asyncio.Lock()
    return _LIVE_LOCK


def _session_public_payload(session: LiveBrowserSession, *, reused: bool = False) -> dict[str, Any]:
    return {
        "ok": True,
        "session_id": session.session_id,
        "domain": session.domain,
        "account_id": session.account_id,
        "start_url": session.start_url,
        "current_url": session.current_url,
        "access_mode": session.access_mode,
        "proxy_url_bound": bool(session.proxy_url),
        "created_at": session.created_at,
        "last_used_at": session.last_used_at,
        "adapter_name": session.adapter_name,
        "route_guarded": session.route_guarded,
        "reused": reused,
    }


def _error_payload(error_type: str, message: str, **details) -> dict[str, Any]:
    return {
        "ok": False,
        "errorType": error_type,
        "message": message,
        **{k: v for k, v in details.items() if v not in (None, "")},
    }


def _format_ok_json(payload: dict[str, Any]) -> str:
    return format_ok(json.dumps(payload, ensure_ascii=False))


def _audit_browser_action(
    session: LiveBrowserSession,
    action_kind: str,
    status: str,
    *,
    index: int,
) -> None:
    try:
        conn = db.connect()
        try:
            audit.write_audit(
                conn,
                tool="browser_action",
                url=session.current_url,
                status=status,
                rung_used=f"{index}:{action_kind}",
            )
        finally:
            conn.close()
    except Exception:
        pass


async def open_browser_session(
    url: str,
    *,
    account_id: str = "default",
    access_mode: str = "user_authorized",
    use_proxy: bool = False,
    wait_until: str = "domcontentloaded",
    timeout: int = 60,
) -> str:
    if access_mode not in ACCESS_MODES:
        return format_error("invalid-mode", f"Unsupported access_mode: {access_mode}")
    if wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
        return format_error("invalid-mode", f"Unsupported wait_until: {wait_until}")
    if not PATCHRIGHT_AVAILABLE:
        return format_error("internal-error", "patchright/playwright is not installed")
    if not hitl_rung.has_display():
        return format_error(
            "human-window-unavailable",
            "A visible desktop browser session is required for live browser access",
        )
    if access_mode == "user_authorized" and not account_vault.is_vault_enabled():
        return format_error(
            "human-window-unavailable",
            "TRAWLER_VAULT_KEY is required to persist authorized browser state",
        )

    canon = urlnorm.canonical_url(url)
    if not canon:
        return format_error("invalid-url", "Invalid URL provided")
    is_blocked, safe_ip = await ssrf.resolve_and_check_async(canon)
    if is_blocked:
        return ssrf.block_reason(canon)

    domain = domain_of(canon)
    try:
        resolved_account_id = account_profiles.resolve_account_id(domain, account_id)
    except ValueError as e:
        return format_error("invalid-mode", str(e))
    if access_mode == "user_authorized":
        account_profiles.register_profile(
            domain,
            account_id=resolved_account_id,
            make_default=resolved_account_id == "default",
        )
    proxy_url = proxy_pool.select_proxy(
        use_proxy,
        domain=domain,
        account_id=resolved_account_id,
    )
    async with _get_live_lock():
        for existing in _LIVE_SESSIONS.values():
            same_identity = (
                existing.domain == domain
                and existing.account_id == resolved_account_id
                and existing.adapter_name == "local_persistent"
                and existing.access_mode == access_mode
                and bool(existing.proxy_url) == bool(proxy_url)
            )
            if same_identity:
                await existing.page.goto(canon, wait_until=wait_until, timeout=timeout * 1000)
                existing.current_url = existing.page.url
                existing.last_used_at = _now_iso()
                return _format_ok_json(_session_public_payload(existing, reused=True))

        session_id = f"live-{secrets.token_hex(8)}"
        profile_dir = account_vault.profile_dir(domain, account_id=resolved_account_id)
        try:
            handle = await browser_adapter.open_local_persistent_browser(
                browser_adapter.LocalPersistentOptions(
                    profile_dir=profile_dir,
                    domain=domain,
                    url=canon,
                    safe_ip=safe_ip,
                    use_proxy=use_proxy,
                    proxy_url=proxy_url,
                    wait_until=wait_until,
                    timeout=timeout,
                )
            )
            if not handle.route_guarded and not config.ALLOW_UNGUARDED_BROWSER:
                await browser_adapter.close_browser_handle(handle)
                return format_error(
                    "permission-denied",
                    "Browser route SSRF guard is unavailable; "
                    "refusing to open an unguarded session.",
                )
            session = LiveBrowserSession(
                session_id=session_id,
                domain=domain,
                account_id=resolved_account_id,
                start_url=canon,
                current_url=handle.page.url,
                context=handle.context,
                page=handle.page,
                playwright=handle.playwright,
                profile_dir=profile_dir,
                proxy_url=proxy_url,
                access_mode=access_mode,
                adapter_name=handle.adapter_name,
                route_guarded=handle.route_guarded,
                browser_handle=handle,
            )
            _LIVE_SESSIONS[session_id] = session
            return _format_ok_json(_session_public_payload(session))
        except Exception as e:
            return format_error(
                "internal-error",
                f"Failed to open live browser: {type(e).__name__}: {e}",
            )


async def connect_browser_session(
    cdp_url: str,
    *,
    url: str = "",
    account_id: str = "external",
    access_mode: str = "user_authorized",
    wait_until: str = "domcontentloaded",
    timeout: int = 60,
) -> str:
    if access_mode not in ACCESS_MODES:
        return format_error("invalid-mode", f"Unsupported access_mode: {access_mode}")
    if wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
        return format_error("invalid-mode", f"Unsupported wait_until: {wait_until}")
    if not PATCHRIGHT_AVAILABLE:
        return format_error("internal-error", "patchright/playwright is not installed")
    try:
        resolved_account_id = account_vault.normalize_account_id(account_id or "external")
    except ValueError as e:
        return format_error("invalid-mode", str(e))

    canon = ""
    if url:
        canon = urlnorm.canonical_url(url)
        if not canon:
            return format_error("invalid-url", "Invalid URL provided")
        is_blocked, _safe_ip = await ssrf.resolve_and_check_async(canon)
        if is_blocked:
            return ssrf.block_reason(canon)

    async with _get_live_lock():
        session_id = f"live-{secrets.token_hex(8)}"
        handle = None
        try:
            handle = await browser_adapter.connect_cdp_browser(
                browser_adapter.CdpConnectOptions(
                    cdp_url=cdp_url,
                    url=canon,
                    wait_until=wait_until,
                    timeout=timeout,
                )
            )
            if not handle.route_guarded and not config.ALLOW_UNGUARDED_BROWSER:
                await browser_adapter.close_browser_handle(handle)
                return format_error(
                    "permission-denied",
                    "Browser route SSRF guard is unavailable; "
                    "refusing to connect an unguarded session.",
                )
            current_url = str(getattr(handle.page, "url", "") or "")
            current_canon = urlnorm.canonical_url(current_url)
            if not current_canon:
                await browser_adapter.close_browser_handle(handle)
                return format_error(
                    "invalid-url",
                    "The connected browser page is not an http/https page; "
                    "navigate to a web URL first.",
                )
            is_blocked, _safe_ip = await ssrf.resolve_and_check_async(current_canon)
            if is_blocked:
                await browser_adapter.close_browser_handle(handle)
                return ssrf.block_reason(current_canon)
            domain = domain_of(current_canon)
            if account_vault.is_vault_enabled():
                account_profiles.register_profile(
                    domain,
                    account_id=resolved_account_id,
                    label="Connected CDP browser",
                    notes="Imported from an existing user-controlled browser session.",
                    make_default=False,
                )
            session = LiveBrowserSession(
                session_id=session_id,
                domain=domain,
                account_id=resolved_account_id,
                start_url=current_canon,
                current_url=current_canon,
                context=handle.context,
                page=handle.page,
                playwright=handle.playwright,
                profile_dir="",
                access_mode=access_mode,
                adapter_name=handle.adapter_name,
                route_guarded=handle.route_guarded,
                browser_handle=handle,
            )
            _LIVE_SESSIONS[session_id] = session
            return _format_ok_json(_session_public_payload(session))
        except PermissionError as e:
            return format_error("permission-denied", str(e))
        except Exception as e:
            if handle is not None:
                try:
                    await browser_adapter.close_browser_handle(handle)
                except Exception:
                    pass
            return format_error(
                "internal-error",
                f"Failed to connect browser over CDP: {type(e).__name__}: {e}",
            )


def list_browser_sessions() -> str:
    rows = [_session_public_payload(session) for session in _LIVE_SESSIONS.values()]
    return _format_ok_json({"ok": True, "count": len(rows), "items": rows})


async def _persist_account_state(session: LiveBrowserSession) -> None:
    if not account_vault.is_vault_enabled():
        return
    try:
        try:
            state = await session.context.storage_state(indexed_db=True)
        except TypeError:
            state = await session.context.storage_state()
        account_vault.save_storage_state(
            session.domain,
            state,
            account_id=session.account_id,
        )
        account_profiles.touch_verified(session.domain, session.account_id)
    except Exception as e:
        log.warning("save live browser storage state failed for %s: %s", session.domain, e)
    try:
        cookies = await session.context.cookies([session.current_url])
        if cookies:
            account_vault.save_auto_cookies(
                session.domain,
                cookies,
                session_id=session.session_id,
                account_id=session.account_id,
            )
    except Exception as e:
        log.debug("save live browser cookies failed for %s: %s", session.domain, e)


async def _page_html(page) -> str:
    html = await page.content()
    return html[: config.HTML_TRUNCATE]


async def _visible_text(page) -> str:
    try:
        text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    return str(text or "").strip()


async def _element_snapshot(page, selector: str) -> dict[str, Any] | None:
    script = """
    (selector) => {
      const root = document.querySelector(selector);
      if (!root) return null;
      const styleKeys = [
        'display', 'position', 'boxSizing', 'width', 'height', 'margin', 'padding',
        'fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'color',
        'backgroundColor', 'border', 'borderRadius', 'boxShadow',
        'gap', 'gridTemplateColumns', 'alignItems', 'justifyContent'
      ];
      const pickStyle = (el) => {
        const style = window.getComputedStyle(el);
        const out = {};
        for (const key of styleKeys) out[key] = style[key] || '';
        return out;
      };
      const rectOf = (el) => {
        const rect = el.getBoundingClientRect();
        return {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          width: Math.round(rect.width),
          height: Math.round(rect.height)
        };
      };
      const childSummary = Array.from(root.children).slice(0, 24).map((el) => ({
        tag: el.tagName.toLowerCase(),
        id: el.id || '',
        className: String(el.className || ''),
        text: String(el.innerText || el.textContent || '').trim().slice(0, 300),
        rect: rectOf(el),
        styles: pickStyle(el)
      }));
      return {
        selector,
        tag: root.tagName.toLowerCase(),
        id: root.id || '',
        className: String(root.className || ''),
        text: String(root.innerText || root.textContent || '').trim().slice(0, 4000),
        outerHTML: root.outerHTML.slice(0, 20000),
        rect: rectOf(root),
        styles: pickStyle(root),
        children: childSummary
      };
    }
    """
    return await page.evaluate(script, selector)


async def _page_clone_snapshot(
    page,
    selector: str = "body",
    max_nodes: int = 180,
) -> dict[str, Any]:
    script = """
    ({selector, maxNodes}) => {
      const root = document.querySelector(selector || 'body') || document.body;
      const styleKeys = [
        'display', 'position', 'boxSizing', 'width', 'height', 'margin', 'padding',
        'fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'color',
        'backgroundColor', 'border', 'borderRadius', 'boxShadow',
        'gap', 'gridTemplateColumns', 'alignItems', 'justifyContent'
      ];
      const pickStyle = (el) => {
        const style = window.getComputedStyle(el);
        const out = {};
        for (const key of styleKeys) out[key] = style[key] || '';
        return out;
      };
      const rectOf = (el) => {
        const rect = el.getBoundingClientRect();
        return {
          x: Math.round(rect.x + window.scrollX),
          y: Math.round(rect.y + window.scrollY),
          width: Math.round(rect.width),
          height: Math.round(rect.height)
        };
      };
      let count = 0;
      const walk = (el, depth = 0) => {
        if (!el || count >= maxNodes || depth > 8) return null;
        count += 1;
        const children = Array.from(el.children || [])
          .slice(0, 32)
          .map((child) => walk(child, depth + 1))
          .filter(Boolean);
        return {
          tag: el.tagName.toLowerCase(),
          id: el.id || '',
          className: String(el.className || ''),
          text: String(el.innerText || el.textContent || '').trim().slice(0, 500),
          rect: rectOf(el),
          styles: pickStyle(el),
          children
        };
      };
      const tree = walk(root);
      return {
        selector: selector || 'body',
        url: location.href,
        title: document.title || '',
        viewport: {
          width: window.innerWidth,
          height: window.innerHeight,
          scrollX: window.scrollX,
          scrollY: window.scrollY
        },
        nodeCount: count,
        tree
      };
    }
    """
    return await page.evaluate(script, {"selector": selector or "body", "maxNodes": max_nodes})


async def _visible_blocks_snapshot(
    page,
    selector: str = "body",
    max_blocks: int = 80,
) -> dict[str, Any]:
    script = """
    ({selector, maxBlocks}) => {
      const root = document.querySelector(selector || 'body') || document.body;
      if (!root) return {selector: selector || 'body', blockCount: 0, blocks: []};
      const cssEscape = (value) => {
        if (window.CSS && CSS.escape) return CSS.escape(value);
        return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\\\$&');
      };
      const selectorFor = (el) => {
        if (el.id) return `#${cssEscape(el.id)}`;
        const parts = [];
        let node = el;
        while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 4) {
          let part = node.tagName.toLowerCase();
          const classes = Array.from(node.classList || [])
            .filter((c) => !/^css-/.test(c))
            .slice(0, 2);
          if (classes.length) part += classes.map((c) => `.${cssEscape(c)}`).join('');
          const parent = node.parentElement;
          if (parent) {
            const siblings = Array.from(parent.children).filter((s) => s.tagName === node.tagName);
            if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
          }
          parts.unshift(part);
          node = parent;
        }
        return parts.join(' > ');
      };
      const norm = (text) => String(text || '').replace(/\\s+/g, ' ').trim();
      const rectOf = (el) => {
        const rect = el.getBoundingClientRect();
        return {
          x: Math.round(rect.x + window.scrollX),
          y: Math.round(rect.y + window.scrollY),
          width: Math.round(rect.width),
          height: Math.round(rect.height)
        };
      };
      const badTags = new Set(['script', 'style', 'noscript', 'svg', 'path']);
      const containerTags = new Set(['body', 'html', 'main']);
      const candidates = Array.from(root.querySelectorAll([
        'article', '[role="article"]', 'a', 'button', 'li',
        '[class*="card"]', '[class*="note"]', '[class*="item"]',
        '[class*="feed"]', '[class*="post"]', '[class*="waterfall"]',
        'section', 'div'
      ].join(',')));
      const seen = new Set();
      const blocks = [];
      for (const el of candidates) {
        if (!el || badTags.has(el.tagName.toLowerCase())) continue;
        const tag = el.tagName.toLowerCase();
        const style = window.getComputedStyle(el);
        if (
          style.display === 'none' ||
          style.visibility === 'hidden' ||
          Number(style.opacity) === 0
        ) {
          continue;
        }
        const rect = el.getBoundingClientRect();
        if (rect.width < 40 || rect.height < 16 || rect.width * rect.height < 800) continue;
        if (containerTags.has(tag) && rect.width > window.innerWidth * 0.85) continue;
        const text = norm(el.innerText || el.textContent || '');
        if (text.length < 4 || text.length > 800) continue;
        const parentText = norm(el.parentElement ? el.parentElement.innerText : '');
        if (parentText && parentText === text && el.children.length === 1) continue;
        const key = text.slice(0, 240);
        if (seen.has(key)) continue;
        seen.add(key);
        blocks.push({
          tag,
          role: el.getAttribute('role') || '',
          selector: selectorFor(el),
          text: text.slice(0, 500),
          rect: rectOf(el)
        });
      }
      const compact = blocks.filter((block) => {
        if (block.text.length < 120) return true;
        let contained = 0;
        for (const other of blocks) {
          if (other === block) continue;
          if (other.text.length < 8 || other.text.length >= block.text.length * 0.8) continue;
          if (block.text.includes(other.text)) contained += 1;
          if (contained >= 2) return false;
        }
        return true;
      });
      compact.sort((a, b) => (a.rect.y - b.rect.y) || (a.rect.x - b.rect.x));
      const out = compact.slice(0, maxBlocks);
      return {
        selector: selector || 'body',
        url: location.href,
        title: document.title || '',
        viewport: {
          width: window.innerWidth,
          height: window.innerHeight,
          scrollX: window.scrollX,
          scrollY: window.scrollY
        },
        blockCount: out.length,
        candidateCount: blocks.length,
        truncated: compact.length > out.length,
        blocks: out
      };
    }
    """
    return await page.evaluate(
        script,
        {"selector": selector or "body", "maxBlocks": max(1, min(int(max_blocks), 300))},
    )


def _visible_blocks_markdown(snapshot: dict[str, Any], max_chars: int = 12000) -> str:
    blocks = snapshot.get("blocks") if isinstance(snapshot, dict) else []
    if not isinstance(blocks, list) or not blocks:
        return ""
    lines = ["## Visible Content Blocks"]
    used = len(lines[0])
    for idx, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            continue
        text = " ".join(str(block.get("text") or "").split())
        if not text:
            continue
        rect = block.get("rect") if isinstance(block.get("rect"), dict) else {}
        prefix = f"{idx}. "
        location = ""
        if rect:
            location = (
                f" [x={rect.get('x', 0)}, y={rect.get('y', 0)}, "
                f"w={rect.get('width', 0)}, h={rect.get('height', 0)}]"
            )
        line = f"{prefix}{text}{location}"
        if used + len(line) + 2 > max_chars:
            lines.append("[...truncated...]")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines).strip()


async def start_element_picker(session_id: str) -> str:
    session = _LIVE_SESSIONS.get(session_id)
    if session is None:
        return format_error("job-not-found", f"Live browser session not found: {session_id}")
    script = """
    () => {
      const styleKeys = [
        'display', 'position', 'boxSizing', 'width', 'height', 'margin', 'padding',
        'fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'color',
        'backgroundColor', 'border', 'borderRadius', 'boxShadow',
        'gap', 'gridTemplateColumns', 'alignItems', 'justifyContent'
      ];
      const cssEscape = (value) => {
        if (window.CSS && CSS.escape) return CSS.escape(value);
        return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\\\$&');
      };
      const selectorFor = (el) => {
        if (el.id) return `#${cssEscape(el.id)}`;
        const parts = [];
        let node = el;
        while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 5) {
          let part = node.tagName.toLowerCase();
          const classes = Array.from(node.classList || []).slice(0, 3);
          if (classes.length) part += classes.map((c) => `.${cssEscape(c)}`).join('');
          const parent = node.parentElement;
          if (parent) {
            const siblings = Array.from(parent.children).filter((s) => s.tagName === node.tagName);
            if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
          }
          parts.unshift(part);
          node = parent;
        }
        return parts.join(' > ');
      };
      const pickStyle = (el) => {
        const style = window.getComputedStyle(el);
        const out = {};
        for (const key of styleKeys) out[key] = style[key] || '';
        return out;
      };
      const rectOf = (el) => {
        const rect = el.getBoundingClientRect();
        return {
          x: Math.round(rect.x + window.scrollX),
          y: Math.round(rect.y + window.scrollY),
          width: Math.round(rect.width),
          height: Math.round(rect.height)
        };
      };
      const snapshotOf = (el) => ({
        selector: selectorFor(el),
        tag: el.tagName.toLowerCase(),
        id: el.id || '',
        className: String(el.className || ''),
        text: String(el.innerText || el.textContent || '').trim().slice(0, 4000),
        outerHTML: el.outerHTML.slice(0, 20000),
        rect: rectOf(el),
        styles: pickStyle(el)
      });
      window.__TRAWLER_PICKED_ELEMENT = null;
      const oldOverlay = document.getElementById('__trawler_element_picker_overlay');
      if (oldOverlay) oldOverlay.remove();
      const overlay = document.createElement('div');
      overlay.id = '__trawler_element_picker_overlay';
      overlay.style.cssText = [
        'position:fixed', 'z-index:2147483647', 'border:2px solid #21a0ff',
        'background:rgba(33,160,255,.08)', 'pointer-events:none', 'display:none'
      ].join(';');
      document.documentElement.appendChild(overlay);
      const cleanup = () => {
        document.removeEventListener('mousemove', onMove, true);
        document.removeEventListener('click', onClick, true);
        overlay.remove();
      };
      const onMove = (event) => {
        const el = document.elementFromPoint(event.clientX, event.clientY);
        if (!el || el === overlay || el.id === '__trawler_element_picker_overlay') return;
        const rect = el.getBoundingClientRect();
        overlay.style.display = 'block';
        overlay.style.left = `${Math.round(rect.left)}px`;
        overlay.style.top = `${Math.round(rect.top)}px`;
        overlay.style.width = `${Math.round(rect.width)}px`;
        overlay.style.height = `${Math.round(rect.height)}px`;
      };
      const onClick = (event) => {
        const el = document.elementFromPoint(event.clientX, event.clientY);
        if (!el || el === overlay) return;
        event.preventDefault();
        event.stopPropagation();
        window.__TRAWLER_PICKED_ELEMENT = snapshotOf(el);
        cleanup();
      };
      document.addEventListener('mousemove', onMove, true);
      document.addEventListener('click', onClick, true);
      return {ok: true, mode: 'element', message: 'click an element in the browser'};
    }
    """
    result = await session.page.evaluate(script)
    return _format_ok_json({"ok": True, "session_id": session_id, "picker": result})


async def start_region_picker(session_id: str) -> str:
    session = _LIVE_SESSIONS.get(session_id)
    if session is None:
        return format_error("job-not-found", f"Live browser session not found: {session_id}")
    script = """
    () => {
      window.__TRAWLER_PICKED_REGION = null;
      const oldOverlay = document.getElementById('__trawler_region_picker_overlay');
      if (oldOverlay) oldOverlay.remove();
      const overlay = document.createElement('div');
      overlay.id = '__trawler_region_picker_overlay';
      overlay.style.cssText = [
        'position:fixed', 'z-index:2147483647', 'border:2px solid #ff7a1a',
        'background:rgba(255,122,26,.12)', 'pointer-events:none', 'display:none'
      ].join(';');
      document.documentElement.appendChild(overlay);
      let start = null;
      const cleanup = () => {
        document.removeEventListener('mousedown', onDown, true);
        document.removeEventListener('mousemove', onMove, true);
        document.removeEventListener('mouseup', onUp, true);
        overlay.remove();
      };
      const rectFrom = (event) => {
        const left = Math.min(start.clientX, event.clientX);
        const top = Math.min(start.clientY, event.clientY);
        const width = Math.abs(event.clientX - start.clientX);
        const height = Math.abs(event.clientY - start.clientY);
        return {left, top, width, height};
      };
      const onDown = (event) => {
        start = {clientX: event.clientX, clientY: event.clientY};
        overlay.style.display = 'block';
        event.preventDefault();
        event.stopPropagation();
      };
      const onMove = (event) => {
        if (!start) return;
        const rect = rectFrom(event);
        overlay.style.left = `${Math.round(rect.left)}px`;
        overlay.style.top = `${Math.round(rect.top)}px`;
        overlay.style.width = `${Math.round(rect.width)}px`;
        overlay.style.height = `${Math.round(rect.height)}px`;
        event.preventDefault();
        event.stopPropagation();
      };
      const onUp = (event) => {
        if (!start) return;
        const rect = rectFrom(event);
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        const text = Array.from(document.elementsFromPoint(cx, cy))
          .slice(0, 8)
          .map((el) => String(el.innerText || el.textContent || '').trim())
          .filter(Boolean)
          .join('\\n')
          .slice(0, 4000);
        window.__TRAWLER_PICKED_REGION = {
          rect: {
            x: Math.round(rect.left + window.scrollX),
            y: Math.round(rect.top + window.scrollY),
            width: Math.round(rect.width),
            height: Math.round(rect.height)
          },
          viewportRect: {
            x: Math.round(rect.left),
            y: Math.round(rect.top),
            width: Math.round(rect.width),
            height: Math.round(rect.height)
          },
          text
        };
        event.preventDefault();
        event.stopPropagation();
        cleanup();
      };
      document.addEventListener('mousedown', onDown, true);
      document.addEventListener('mousemove', onMove, true);
      document.addEventListener('mouseup', onUp, true);
      return {ok: true, mode: 'region', message: 'drag a region in the browser'};
    }
    """
    result = await session.page.evaluate(script)
    return _format_ok_json({"ok": True, "session_id": session_id, "picker": result})


async def _picked_element(page) -> dict[str, Any] | None:
    return await page.evaluate("() => window.__TRAWLER_PICKED_ELEMENT || null")


async def _picked_region(page) -> dict[str, Any] | None:
    return await page.evaluate("() => window.__TRAWLER_PICKED_REGION || null")


def _action_type(action: dict[str, Any]) -> str:
    return str(action.get("type") or action.get("action") or "").strip().lower()


async def _ensure_public_http_url(url: str) -> str:
    canon = urlnorm.canonical_url(url)
    if not canon:
        raise ValueError("invalid URL")
    is_blocked, _safe_ip = await ssrf.resolve_and_check_async(canon)
    if is_blocked:
        raise PermissionError("Blocked non-public IP (SSRF guard).")
    return canon


async def _verify_current_page_url(session: LiveBrowserSession) -> None:
    current = str(getattr(session.page, "url", "") or "")
    canon = urlnorm.canonical_url(current)
    if not canon:
        return
    is_blocked, _safe_ip = await ssrf.resolve_and_check_async(canon)
    if is_blocked:
        raise PermissionError("Blocked non-public IP (SSRF guard).")
    session.current_url = canon


async def _run_one_browser_action(
    session: LiveBrowserSession,
    action: dict[str, Any],
    *,
    wait_until: str,
    timeout: int,
) -> dict[str, Any]:
    action_kind = _action_type(action)
    if action_kind not in BROWSER_ACTION_TYPES:
        raise ValueError(f"unsupported browser action: {action_kind or '<missing>'}")

    selector = str(action.get("selector") or "").strip()
    action_timeout = int(action.get("timeout") or timeout)
    timeout_ms = max(1, min(action_timeout, 300)) * 1000
    page = session.page

    if action_kind == "goto":
        target_url = await _ensure_public_http_url(str(action.get("url") or ""))
        await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
        await _verify_current_page_url(session)
        return {"type": action_kind, "url": target_url}

    if action_kind == "wait":
        seconds = float(action.get("seconds") or action.get("timeout") or 1)
        seconds = max(0.0, min(seconds, 30.0))
        await asyncio.sleep(seconds)
        return {"type": action_kind, "seconds": seconds}

    if action_kind == "scroll":
        x = int(action.get("x") or 0)
        y = int(action.get("y") or action.get("amount") or 0)
        if selector:
            await page.locator(selector).scroll_into_view_if_needed(timeout=timeout_ms)
        else:
            await page.evaluate("({x, y}) => window.scrollBy(x, y)", {"x": x, "y": y})
        return {"type": action_kind, "selector": selector, "x": x, "y": y}

    if action_kind == "wait_for_selector":
        if not selector:
            raise ValueError("wait_for_selector requires selector")
        if hasattr(page, "wait_for_selector"):
            await page.wait_for_selector(selector, timeout=timeout_ms)
        else:
            await page.locator(selector).wait_for(timeout=timeout_ms)
        return {"type": action_kind, "selector": selector}

    if not selector:
        raise ValueError(f"{action_kind} requires selector")

    locator = page.locator(selector)
    if action_kind == "click":
        await locator.click(timeout=timeout_ms)
    elif action_kind in {"fill", "type"}:
        text = str(action.get("text") or action.get("value") or "")
        if action_kind == "type" and hasattr(locator, "type"):
            await locator.type(text, timeout=timeout_ms)
        else:
            await locator.fill(text, timeout=timeout_ms)
        await _verify_current_page_url(session)
        return {"type": action_kind, "selector": selector, "text_chars": len(text)}
    elif action_kind == "press":
        key = str(action.get("key") or "")
        if not key:
            raise ValueError("press requires key")
        await locator.press(key, timeout=timeout_ms)
        await _verify_current_page_url(session)
        return {"type": action_kind, "selector": selector, "key": key}
    elif action_kind == "check":
        await locator.check(timeout=timeout_ms)
    elif action_kind == "uncheck":
        await locator.uncheck(timeout=timeout_ms)
    elif action_kind == "select_option":
        value = action.get("value")
        await locator.select_option(value, timeout=timeout_ms)
        await _verify_current_page_url(session)
        return {"type": action_kind, "selector": selector, "value": value}
    await _verify_current_page_url(session)
    return {"type": action_kind, "selector": selector}


async def _perform_browser_actions_locked(
    session: LiveBrowserSession,
    actions: list[dict[str, Any]] | None,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 30,
) -> dict[str, Any]:
    if not actions:
        return {"ok": True, "count": 0, "items": []}
    if wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
        return {
            "ok": False,
            "errorType": "invalid-mode",
            "message": f"Unsupported wait_until: {wait_until}",
        }

    items: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            return {
                "ok": False,
                "errorType": "invalid-mode",
                "message": "browser action must be an object",
                "action_index": index,
            }
        action_kind = _action_type(action)
        try:
            item = await _run_one_browser_action(
                session,
                action,
                wait_until=wait_until,
                timeout=timeout,
            )
            item["index"] = index
            items.append(item)
        except PermissionError as e:
            _audit_browser_action(session, action_kind, "blocked-ssrf", index=index)
            return {
                "ok": False,
                "errorType": "blocked-ssrf",
                "message": str(e),
                "action_index": index,
                "action_type": action_kind,
                "selector": action.get("selector") or "",
            }
        except Exception as e:
            _audit_browser_action(session, action_kind, "failed", index=index)
            return {
                "ok": False,
                "errorType": "invalid-mode",
                "message": f"browser action failed: {type(e).__name__}: {e}",
                "action_index": index,
                "action_type": action_kind,
                "selector": action.get("selector") or "",
            }
        session.current_url = str(getattr(session.page, "url", "") or session.current_url)
        session.last_used_at = _now_iso()
        _audit_browser_action(session, action_kind, "ok", index=index)
    return {"ok": True, "count": len(items), "items": items}


async def perform_browser_actions(
    session_id: str,
    actions: list[dict[str, Any]] | None,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 30,
) -> str:
    session = _LIVE_SESSIONS.get(session_id)
    if session is None:
        return format_error("job-not-found", f"Live browser session not found: {session_id}")
    async with session.op_lock:
        result = await _perform_browser_actions_locked(
            session,
            actions,
            wait_until=wait_until,
            timeout=timeout,
        )
        if result.get("ok"):
            await _persist_account_state(session)
            payload = {
                "ok": True,
                "session_id": session.session_id,
                "domain": session.domain,
                "account_id": session.account_id,
                "current_url": session.current_url,
                "actions": result,
                "audit": {
                    "tool": "browser_action",
                    "record_count": int(result.get("count") or 0),
                },
            }
            return _format_ok_json(payload)
        return format_error(
            str(result.get("errorType") or "invalid-mode"),
            str(result.get("message") or "browser action failed"),
            action_index=result.get("action_index"),
            action_type=result.get("action_type"),
            selector=result.get("selector"),
        )


def _ax_value(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value", "")
    if value is None:
        return ""
    return str(value).strip()


def _fit_ax_nodes(nodes: list[dict[str, Any]], max_nodes: int) -> dict[str, Any]:
    out: list[dict[str, str]] = []
    for node in nodes:
        if not isinstance(node, dict) or node.get("ignored") is True:
            continue
        role = _ax_value(node.get("role"))
        name = _ax_value(node.get("name"))
        description = _ax_value(node.get("description"))
        if not role and not name:
            continue
        item = {"role": role[:80], "name": name[:240]}
        if description:
            item["description"] = description[:240]
        out.append(item)
        if len(out) >= max_nodes:
            break
    return {
        "node_count": len(out),
        "truncated": len(out) >= max_nodes,
        "nodes": out,
    }


async def _accessibility_snapshot(
    session: LiveBrowserSession,
    *,
    selector: str = "",
    max_nodes: int = 120,
) -> dict[str, Any]:
    max_nodes = max(1, min(int(max_nodes), 500))
    try:
        cdp = await session.context.new_cdp_session(session.page)
        tree = await cdp.send("Accessibility.getFullAXTree")
        nodes = tree.get("nodes", []) if isinstance(tree, dict) else []
        payload = _fit_ax_nodes(nodes, max_nodes)
        payload["source"] = "cdp"
        payload["selector"] = selector
        return payload
    except Exception as e:
        cdp_error = str(e)

    script = """
    ({selector, maxNodes}) => {
      const root = document.querySelector(selector || 'body') || document.body;
      if (!root) return {source: 'dom_semantic', node_count: 0, truncated: false, nodes: []};
      const roleFor = (el) => el.getAttribute('role') || el.tagName.toLowerCase();
      const nameFor = (el) => (
        el.getAttribute('aria-label') ||
        el.getAttribute('alt') ||
        el.getAttribute('title') ||
        String(el.innerText || el.textContent || '').trim()
      );
      const candidates = [root, ...root.querySelectorAll(
        'a,button,input,select,textarea,[role],h1,h2,h3,h4,h5,h6,summary,label'
      )];
      const nodes = [];
      for (const el of candidates) {
        if (nodes.length >= maxNodes) break;
        const style = window.getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') continue;
        const role = roleFor(el);
        const name = nameFor(el).replace(/\\s+/g, ' ').slice(0, 240);
        if (!role && !name) continue;
        nodes.push({role: role.slice(0, 80), name});
      }
      return {
        source: 'dom_semantic',
        selector: selector || '',
        node_count: nodes.length,
        truncated: nodes.length >= maxNodes,
        nodes
      };
    }
    """
    try:
        result = await session.page.evaluate(
            script,
            {"selector": selector, "maxNodes": max_nodes},
        )
        if isinstance(result, dict):
            result["cdp_error"] = cdp_error
            return result
    except Exception as e:
        return {
            "source": "unavailable",
            "selector": selector,
            "node_count": 0,
            "truncated": False,
            "nodes": [],
            "cdp_error": cdp_error,
            "fallback_error": str(e),
        }
    return {
        "source": "unavailable",
        "selector": selector,
        "node_count": 0,
        "truncated": False,
        "nodes": [],
        "cdp_error": cdp_error,
    }


async def _actionable_elements_snapshot(
    page,
    *,
    selector: str = "body",
    max_elements: int = 80,
) -> dict[str, Any]:
    script = """
    ({selector, maxElements}) => {
      const root = document.querySelector(selector || 'body') || document.body;
      if (!root) {
        return {selector: selector || 'body', element_count: 0, truncated: false, elements: []};
      }
      const cssEscape = (value) => {
        if (window.CSS && CSS.escape) return CSS.escape(value);
        return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\\\$&');
      };
      const selectorFor = (el) => {
        if (el.id) return `#${cssEscape(el.id)}`;
        const dataKeys = ['data-testid', 'data-test', 'data-cy', 'name'];
        for (const key of dataKeys) {
          const value = el.getAttribute(key);
          if (value) {
            const escaped = String(value).replace(/"/g, '\\"');
            return `${el.tagName.toLowerCase()}[${key}="${escaped}"]`;
          }
        }
        const parts = [];
        let node = el;
        while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 5) {
          let part = node.tagName.toLowerCase();
          const classes = Array.from(node.classList || [])
            .filter((c) => !/^css-/.test(c) && !/^sc-/.test(c))
            .slice(0, 2);
          if (classes.length) part += classes.map((c) => `.${cssEscape(c)}`).join('');
          const parent = node.parentElement;
          if (parent) {
            const siblings = Array.from(parent.children).filter((s) => s.tagName === node.tagName);
            if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
          }
          parts.unshift(part);
          node = parent;
        }
        return parts.join(' > ');
      };
      const norm = (text) => String(text || '').replace(/\\s+/g, ' ').trim();
      const roleFor = (el) => {
        const explicit = el.getAttribute('role');
        if (explicit) return explicit;
        const tag = el.tagName.toLowerCase();
        if (tag === 'a') return 'link';
        if (tag === 'button') return 'button';
        if (tag === 'select') return 'combobox';
        if (tag === 'textarea') return 'textbox';
        if (tag === 'input') {
          const type = (el.getAttribute('type') || 'text').toLowerCase();
          if (type === 'checkbox') return 'checkbox';
          if (type === 'radio') return 'radio';
          if (['submit', 'button', 'reset'].includes(type)) return 'button';
          return 'textbox';
        }
        if (tag === 'summary') return 'button';
        return tag;
      };
      const nameFor = (el) => {
        const tag = el.tagName.toLowerCase();
        const type = (el.getAttribute('type') || '').toLowerCase();
        const safeAttrs = [
          'aria-label', 'alt', 'title', 'placeholder', 'name', 'data-testid', 'data-test'
        ];
        for (const key of safeAttrs) {
          const value = norm(el.getAttribute(key));
          if (value) return value.slice(0, 220);
        }
        if (tag === 'input' && ['password', 'text', 'email', 'search', 'tel'].includes(type)) {
          return '';
        }
        return norm(el.innerText || el.textContent).slice(0, 220);
      };
      const actionHints = (el) => {
        const tag = el.tagName.toLowerCase();
        const role = roleFor(el);
        const type = (el.getAttribute('type') || '').toLowerCase();
        const textLike = !['button', 'submit', 'reset', 'checkbox', 'radio'].includes(type);
        const out = [];
        if (role === 'link' || role === 'button' || tag === 'summary') out.push('click');
        if (tag === 'select') out.push('select_option');
        if (tag === 'textarea' || (tag === 'input' && textLike)) {
          out.push('fill', 'press');
        }
        if (tag === 'input' && ['checkbox', 'radio'].includes(type)) out.push('check');
        return out.length ? out : ['click'];
      };
      const visible = (el) => {
        const style = window.getComputedStyle(el);
        const hidden = (
          style.visibility === 'hidden' ||
          style.display === 'none' ||
          style.opacity === '0'
        );
        if (hidden) return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      };
      const candidates = [root, ...root.querySelectorAll(
        'a[href],button,input,textarea,select,summary,[role],[contenteditable="true"],[tabindex]'
      )];
      const elements = [];
      const seen = new Set();
      for (const el of candidates) {
        if (elements.length >= maxElements) break;
        if (!(el instanceof HTMLElement) || !visible(el)) continue;
        const tag = el.tagName.toLowerCase();
        const disabled = el.matches(':disabled,[aria-disabled="true"]');
        const rect = el.getBoundingClientRect();
        const stableSelector = selectorFor(el);
        if (!stableSelector || seen.has(stableSelector)) continue;
        seen.add(stableSelector);
        const item = {
          index: elements.length,
          selector: stableSelector,
          tag,
          role: roleFor(el).slice(0, 80),
          name: nameFor(el),
          text: norm(el.innerText || el.textContent).slice(0, 260),
          disabled,
          action_hints: disabled ? [] : actionHints(el),
          rect: {
            x: Math.round(rect.x + window.scrollX),
            y: Math.round(rect.y + window.scrollY),
            width: Math.round(rect.width),
            height: Math.round(rect.height)
          }
        };
        const href = el.getAttribute('href');
        if (href) item.href = new URL(href, location.href).href;
        const inputType = el.getAttribute('type');
        if (inputType) item.input_type = inputType.toLowerCase();
        elements.push(item);
      }
      return {
        selector: selector || 'body',
        url: location.href,
        title: document.title || '',
        viewport: {
          width: window.innerWidth,
          height: window.innerHeight,
          scrollX: window.scrollX,
          scrollY: window.scrollY
        },
        element_count: elements.length,
        truncated: elements.length >= maxElements,
        elements
      };
    }
    """
    max_elements = max(1, min(int(max_elements), 300))
    result = await page.evaluate(
        script,
        {"selector": selector or "body", "maxElements": max_elements},
    )
    return result if isinstance(result, dict) else {"element_count": 0, "elements": []}


async def observe_browser_session(
    session_id: str,
    *,
    selector: str = "body",
    max_elements: int = 80,
    include_accessibility: bool = True,
) -> str:
    session = _LIVE_SESSIONS.get(session_id)
    if session is None:
        return format_error("job-not-found", f"Live browser session not found: {session_id}")

    try:
        async with session.op_lock:
            await _verify_current_page_url(session)
            observation = await _actionable_elements_snapshot(
                session.page,
                selector=selector or "body",
                max_elements=max_elements,
            )
            if include_accessibility:
                observation["accessibility_snapshot"] = await _accessibility_snapshot(
                    session,
                    selector=selector,
                    max_nodes=min(max(int(max_elements), 1), 200),
                )
            session.current_url = str(getattr(session.page, "url", "") or session.current_url)
            session.last_used_at = _now_iso()
            return _format_ok_json(
                {
                    "ok": True,
                    "session_id": session.session_id,
                    "domain": session.domain,
                    "account_id": session.account_id,
                    "current_url": session.current_url,
                    "selector": selector or "body",
                    "observation": observation,
                }
            )
    except PermissionError as e:
        return format_error("blocked-ssrf", str(e))
    except Exception as e:
        return format_error(
            "internal-error",
            f"Live browser observe failed: {type(e).__name__}: {e}",
        )


async def _page_title(page) -> str:
    try:
        title = await page.title()
    except Exception:
        try:
            title = await page.evaluate("() => document.title || ''")
        except Exception:
            title = ""
    return str(title or "").strip()


def _markdown_from_html(html: str, url: str) -> str:
    markdown = parser_extract.extract(html, url)
    return markdown if parser_extract.is_extracted(markdown) else ""


async def _fit_current_page_markdown(
    session: LiveBrowserSession,
    html: str,
    *,
    max_chars: int = 20000,
) -> tuple[str, dict[str, Any]]:
    markdown = _markdown_from_html(html, session.current_url)
    if not markdown:
        markdown = await _visible_text(session.page)
    fitted = parser_fit_markdown.fit_markdown(
        markdown,
        max_chars=max_chars,
        base_url=session.current_url,
    )
    stats = fitted.as_dict()
    text = str(stats.pop("markdown"))
    return text, stats


async def extract_browser_session(
    session_id: str,
    *,
    extract_mode: str = "page",
    selector: str = "",
    actions: list[dict[str, Any]] | None = None,
    action_timeout: int = 30,
    wait_until: str = "domcontentloaded",
    max_markdown_chars: int = 20000,
    capture_artifact: bool = False,
    close_after: bool = False,
) -> LiveBrowserExtraction:
    if extract_mode not in EXTRACT_MODES:
        legacy = format_error("invalid-mode", f"Unsupported extract_mode: {extract_mode}")
        return LiveBrowserExtraction(
            legacy_text=legacy,
            structured=_error_payload("invalid-mode", legacy),
        )
    session = _LIVE_SESSIONS.get(session_id)
    if session is None:
        legacy = format_error("job-not-found", f"Live browser session not found: {session_id}")
        return LiveBrowserExtraction(
            legacy_text=legacy,
            structured=_error_payload("job-not-found", legacy),
        )
    if extract_mode in {"selector", "element_snapshot"} and not selector.strip():
        legacy = format_error("invalid-mode", f"extract_mode='{extract_mode}' requires selector")
        return LiveBrowserExtraction(
            legacy_text=legacy,
            structured=_error_payload("invalid-mode", legacy),
        )

    try:
        async with session.op_lock:
            screenshot: bytes | None = None
            artifact_id = ""
            html = ""
            text = ""
            metadata: dict[str, Any] = {}
            top_level: dict[str, Any] = {}

            actions_result = await _perform_browser_actions_locked(
                session,
                actions,
                wait_until=wait_until,
                timeout=action_timeout,
            )
            if not actions_result.get("ok"):
                error_type = str(actions_result.get("errorType") or "invalid-mode")
                message = str(actions_result.get("message") or "browser action failed")
                legacy = format_error(
                    error_type,
                    message,
                    action_index=actions_result.get("action_index"),
                    action_type=actions_result.get("action_type"),
                    selector=actions_result.get("selector"),
                )
                return LiveBrowserExtraction(
                    legacy_text=legacy,
                    structured=_error_payload(
                        error_type,
                        message,
                        session_id=session_id,
                        action_index=actions_result.get("action_index"),
                        action_type=actions_result.get("action_type"),
                        selector=actions_result.get("selector"),
                    ),
                )
            if actions_result.get("count"):
                metadata["actions"] = actions_result

            session.current_url = str(getattr(session.page, "url", "") or session.current_url)
            session.last_used_at = _now_iso()
            await _verify_current_page_url(session)

            if extract_mode == "visible_text":
                text = await _visible_text(session.page)
            elif extract_mode == "html":
                html = await _page_html(session.page)
                text = html
            elif extract_mode == "page_clone":
                clone = await _page_clone_snapshot(session.page, selector or "body")
                metadata["page_clone"] = clone
                text = json.dumps(clone, ensure_ascii=False, indent=2)
                capture_artifact = True
            elif extract_mode == "accessibility_snapshot":
                snapshot = await _accessibility_snapshot(session, selector=selector)
                metadata["accessibility_snapshot"] = snapshot
                text = json.dumps(snapshot, ensure_ascii=False, indent=2)
            elif extract_mode == "visible_blocks":
                snapshot = await _visible_blocks_snapshot(session.page, selector or "body")
                metadata["visible_blocks"] = snapshot
                text = _visible_blocks_markdown(snapshot)
                if not text:
                    text = json.dumps(snapshot, ensure_ascii=False, indent=2)
            elif extract_mode == "fit_markdown":
                html = await _page_html(session.page)
                if selector:
                    html, selector_report = parser_selectors.apply_selectors(html, [selector])
                    metadata["selector"] = selector
                    metadata["selector_report"] = selector_report
                text, fit_stats = await _fit_current_page_markdown(
                    session,
                    html,
                    max_chars=max_markdown_chars,
                )
                metadata["fit_markdown"] = fit_stats
                top_level["citations"] = fit_stats.get("citations", [])
            elif extract_mode == "bundle":
                html = await _page_html(session.page)
                parser_html = html
                if selector:
                    parser_html, selector_report = parser_selectors.apply_selectors(
                        html,
                        [selector],
                    )
                    metadata["selector"] = selector
                    metadata["selector_report"] = selector_report
                text, fit_stats = await _fit_current_page_markdown(
                    session,
                    parser_html,
                    max_chars=max_markdown_chars,
                )
                visible_text = await _visible_text(session.page)
                visible_blocks = await _visible_blocks_snapshot(session.page, selector or "body")
                visible_blocks_markdown = _visible_blocks_markdown(visible_blocks)
                if visible_blocks_markdown:
                    text = (
                        f"{text}\n\n{visible_blocks_markdown}"
                        if text
                        else visible_blocks_markdown
                    )
                accessibility = await _accessibility_snapshot(session, selector=selector)
                try:
                    page_clone = await _page_clone_snapshot(
                        session.page,
                        selector or "body",
                        max_nodes=80,
                    )
                except Exception as e:
                    page_clone = {"error": str(e)}
                try:
                    screenshot = await session.page.screenshot(full_page=True, timeout=10000)
                except Exception as e:
                    metadata["screenshot_error"] = str(e)
                links = link_map.extract_links(html, session.current_url, max_links=100)
                bundle = {
                    "current_url": session.current_url,
                    "title": await _page_title(session.page),
                    "html_chars": len(html),
                    "visible_text_excerpt": visible_text[:4000],
                    "visible_blocks": visible_blocks,
                    "visible_blocks_markdown": visible_blocks_markdown,
                    "visible_block_count": visible_blocks.get("blockCount", 0),
                    "fit_markdown": fit_stats,
                    "links": links,
                    "link_count": len(links),
                    "accessibility_snapshot": accessibility,
                    "page_clone": page_clone,
                    "screenshot_bytes": len(screenshot or b""),
                }
                metadata["bundle"] = bundle
                top_level["citations"] = fit_stats.get("citations", [])
                top_level["bundle"] = bundle
                capture_artifact = True
            elif extract_mode == "element_snapshot":
                snapshot = await _element_snapshot(session.page, selector)
                if not snapshot:
                    legacy = format_error(
                        "empty-content",
                        f"No element matched selector: {selector}",
                    )
                    return LiveBrowserExtraction(
                        legacy_text=legacy,
                        structured=_error_payload("empty-content", legacy, session_id=session_id),
                    )
                metadata["element_snapshot"] = snapshot
                text = json.dumps(snapshot, ensure_ascii=False, indent=2)
                try:
                    screenshot = await session.page.locator(selector).screenshot(timeout=10000)
                except Exception as e:
                    metadata["element_screenshot_error"] = str(e)
                capture_artifact = True
            elif extract_mode == "picked_element":
                snapshot = await _picked_element(session.page)
                if not snapshot:
                    legacy = format_error(
                        "empty-content",
                        "No picked element yet. Call start_element_picker, then click an element.",
                    )
                    return LiveBrowserExtraction(
                        legacy_text=legacy,
                        structured=_error_payload("empty-content", legacy, session_id=session_id),
                    )
                metadata["picked_element"] = snapshot
                text = json.dumps(snapshot, ensure_ascii=False, indent=2)
                picked_selector = str(snapshot.get("selector") or "")
                if picked_selector:
                    try:
                        screenshot = await session.page.locator(picked_selector).screenshot(
                            timeout=10000,
                        )
                    except Exception as e:
                        metadata["element_screenshot_error"] = str(e)
                capture_artifact = True
            elif extract_mode == "picked_region":
                region = await _picked_region(session.page)
                if not region:
                    legacy = format_error(
                        "empty-content",
                        "No picked region yet. Call start_region_picker, then drag a region.",
                    )
                    return LiveBrowserExtraction(
                        legacy_text=legacy,
                        structured=_error_payload("empty-content", legacy, session_id=session_id),
                    )
                metadata["picked_region"] = region
                text = json.dumps(region, ensure_ascii=False, indent=2)
                rect = region.get("rect") if isinstance(region, dict) else None
                if isinstance(rect, dict):
                    clip = {
                        "x": max(0, int(rect.get("x") or 0)),
                        "y": max(0, int(rect.get("y") or 0)),
                        "width": max(1, int(rect.get("width") or 1)),
                        "height": max(1, int(rect.get("height") or 1)),
                    }
                    try:
                        screenshot = await session.page.screenshot(clip=clip, timeout=10000)
                    except Exception as e:
                        metadata["region_screenshot_error"] = str(e)
                capture_artifact = True
            else:
                html = await _page_html(session.page)
                if extract_mode == "selector":
                    html, selector_report = parser_selectors.apply_selectors(html, [selector])
                    metadata["selector"] = selector
                    metadata["selector_report"] = selector_report
                if extract_mode == "screenshot":
                    screenshot = await session.page.screenshot(full_page=True, timeout=10000)
                    text = json.dumps(
                        {
                            "session_id": session.session_id,
                            "current_url": session.current_url,
                            "screenshot_bytes": len(screenshot),
                        },
                        ensure_ascii=False,
                    )
                    capture_artifact = True
                else:
                    text = _markdown_from_html(html, session.current_url)
                    if not text:
                        text = await _visible_text(session.page)

            if capture_artifact:
                if not html:
                    html = await _page_html(session.page)
                if screenshot is None:
                    try:
                        screenshot = await session.page.screenshot(full_page=True, timeout=10000)
                    except Exception as e:
                        metadata["screenshot_error"] = str(e)
                artifact_id = artifacts.save_artifact(
                    url=session.start_url,
                    reason=f"live-browser-{extract_mode}",
                    success=True,
                    final_url=session.current_url,
                    http_status=200,
                    gear_used="live_browser",
                    session_id=session.session_id,
                    html=html,
                    screenshot=screenshot,
                    extra=metadata,
                )

            await _persist_account_state(session)
            payload = {
                "ok": True,
                "session_id": session.session_id,
                "domain": session.domain,
                "account_id": session.account_id,
                "current_url": session.current_url,
                "extract_mode": extract_mode,
                "selector": selector,
                "artifact_id": artifact_id,
                "metadata": metadata,
                "closed": close_after,
                **top_level,
            }
            legacy = format_ok(text)
            return LiveBrowserExtraction(
                legacy_text=legacy,
                structured=payload,
                screenshot=screenshot,
            )
    except Exception as e:
        legacy = format_error(
            "internal-error",
            f"Live browser extraction failed: {type(e).__name__}: {e}",
        )
        return LiveBrowserExtraction(
            legacy_text=legacy,
            structured=_error_payload("internal-error", str(e), session_id=session_id),
        )
    finally:
        if close_after:
            await close_browser_session(session_id)


async def close_browser_session(session_id: str) -> str:
    session = _LIVE_SESSIONS.pop(session_id, None)
    if session is None:
        return format_error("job-not-found", f"Live browser session not found: {session_id}")
    await _persist_account_state(session)
    if session.browser_handle is not None:
        await browser_adapter.close_browser_handle(session.browser_handle)
    else:
        try:
            await session.context.close()
        finally:
            try:
                await session.playwright.stop()
            except Exception:
                pass
    return _format_ok_json(
        {
            "ok": True,
            "session_id": session_id,
            "domain": session.domain,
            "account_id": session.account_id,
            "closed": True,
        }
    )


async def close_all_browser_sessions() -> None:
    for session_id in list(_LIVE_SESSIONS):
        await close_browser_session(session_id)


def sync_close_all_browser_sessions() -> None:
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(close_all_browser_sessions())
        loop.close()
    except Exception:
        pass


import atexit  # noqa: E402

atexit.register(sync_close_all_browser_sessions)
