"""MCP server — 注册 7 工具 + 2 Resources。

FastMCP stdio 模式。启动时 init_db + lifecycle cleanup + signal handlers。
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Annotated

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from trawler import (
    account_profiles,
    config,
    db,
    lifecycle,
    policy,
    retrieval_readiness,
    signals,
    site_rules,
    structured,
)
from trawler.artifacts import artifact_dir_size as _artifact_dir_size
from trawler.artifacts import artifact_summary as _artifact_summary
from trawler.artifacts import cleanup_artifacts as _cleanup_artifacts
from trawler.artifacts import list_artifacts as _list_artifacts
from trawler.artifacts import read_artifact as _read_artifact
from trawler.artifacts import read_artifact_screenshot as _read_artifact_screenshot
from trawler.crawl_site import (
    cancel_job,
    crawl_site,
    get_job_errors,
    get_job_results,
    get_job_status,
    map_site,
    wait_for_job,
)
from trawler.crawl_url import crawl_url as _crawl_url
from trawler.errors import format_error, format_ok, is_ok, unwrap_ok
from trawler.live_browser import close_browser_session as _close_browser_session
from trawler.live_browser import connect_browser_session as _connect_browser_session
from trawler.live_browser import extract_browser_session as _extract_browser_session
from trawler.live_browser import list_browser_sessions as _list_browser_sessions
from trawler.live_browser import observe_browser_session as _observe_browser_session
from trawler.live_browser import open_browser_session as _open_browser_session
from trawler.live_browser import perform_browser_actions as _perform_browser_actions
from trawler.live_browser import start_element_picker as _start_element_picker
from trawler.live_browser import start_region_picker as _start_region_picker
from trawler.page_retrieval import retrieve_page as _retrieve_page
from trawler.raw_store import get_raw as _get_raw
from trawler.raw_store import list_raw as _list_raw
from trawler.raw_store import read_metadata as _read_raw_metadata
from trawler.raw_store import strip_frontmatter as _strip_frontmatter
from trawler.seen import url_id as _url_id
from trawler.site_index import discover_site_index as _discover_site_index

log = logging.getLogger("trawler.server")

mcp = FastMCP(
    name="trawler",
    instructions=(
        "Trawler — web scraping MCP. Fetches clean markdown from web pages. "
        "Use retrieve_page for single-page user tasks, crawl_url for legacy single-page fetches, "
        "and crawl_site for multi-page crawling. "
        "Results are saved to raw/ and can be read via get_raw or the raw:// resource. "
        "Check the recent://scrapes resource first to see if a page was already fetched. "
        "Legacy tools return strings. Structured variants keep the same text and add structuredContent. "
        "'__TRAWLER_OK__:' prefix = success, '__TRAWLER_ERROR__:{json}' = failure."
    ),
)


def _policy_payload(
    *,
    same_domain_only: bool,
    max_depth: int,
    include_paths: list[str] | None,
    exclude_paths: list[str] | None,
    include_subdomains: bool,
    ignore_query_parameters: bool,
) -> dict:
    return {
        "same_domain_only": same_domain_only,
        "max_depth": max_depth,
        "include_paths": include_paths or [],
        "exclude_paths": exclude_paths or [],
        "include_subdomains": include_subdomains,
        "ignore_query_parameters": ignore_query_parameters,
    }


def _policy_denied_text(decision: policy.PolicyDecision) -> str:
    return format_error(
        "permission-denied",
        f"Policy denied tool call: {decision.tool}",
        policy_decision=decision.as_dict(),
    )


def _policy_denied_result(decision: policy.PolicyDecision) -> CallToolResult:
    legacy_text = _policy_denied_text(decision)
    return CallToolResult(
        content=[TextContent(type="text", text=legacy_text)],
        structuredContent={
            "ok": False,
            "error": structured.error_payload(legacy_text),
            "policy_decision": decision.as_dict(),
        },
        isError=False,
    )


def _policy_check(tool: str, **kwargs) -> policy.PolicyDecision:
    return policy.decide(tool, **kwargs)

# ── Tools ─────────────────────────────────────────────────────────


@mcp.tool(
    description="Retrieve one page for an agent task. "
    "Use access_mode='standard' for normal fetches or access_mode='user_authorized' "
    "when the user is directing browser-equivalent access to a page they can view. "
    "In user_authorized mode Trawler skips the robots precheck for this single page, "
    "prefers a real browser path, does not use external Jina reader fallback, reuses encrypted "
    "account state, and opens a visible human-assist browser when login or verification is needed. "
    "extract_mode can be 'page', 'visible_text', 'selector', or 'screenshot'. "
    "For selector mode, provide selector. For screenshot mode, the result may include MCP image content."
)
async def retrieve_page(
    url: Annotated[str, Field(description="Full URL to retrieve (http/https)")],
    access_mode: Annotated[str, Field(description="'standard' or 'user_authorized'", default="standard")] = "standard",
    account_id: Annotated[str, Field(description="Optional account profile id for user_authorized retrieval", default="")] = "",
    human_assist: Annotated[str, Field(description="'auto', 'required', or 'off'", default="auto")] = "auto",
    extract_mode: Annotated[str, Field(description="'page', 'visible_text', 'selector', or 'screenshot'", default="page")] = "page",
    selector: Annotated[str, Field(description="CSS selector used when extract_mode='selector'", default="")] = "",
    use_proxy: Annotated[bool, Field(description="Use configured proxy", default=False)] = False,
    cache_mode: Annotated[str, Field(description="Cache mode: enabled, read_only, write_only, bypass, or disabled", default="enabled")] = "enabled",
    timeout: Annotated[int, Field(description="Max seconds to wait; use a larger value for human assist", default=120)] = 120,
) -> CallToolResult:
    decision = _policy_check(
        "retrieve_page",
        target_url=url,
        access_mode=access_mode,
        uses_live_browser=access_mode == "user_authorized" or human_assist != "off",
        capture_artifact=extract_mode == "screenshot",
    )
    if not decision.allowed:
        return _policy_denied_result(decision)
    from trawler.otel import span_context
    from trawler.tracing import telemetry_context
    with telemetry_context("retrieve_page", agent_id=account_id or ""), \
         span_context("retrieve_page", url=url, access_mode=access_mode):
        result = await _retrieve_page(
            url,
            access_mode=access_mode,
            account_id=account_id,
            human_assist=human_assist,
            extract_mode=extract_mode,
            selector=selector,
            use_proxy=use_proxy,
            cache_mode=cache_mode,
            timeout=timeout,
        )
        content: list[TextContent | ImageContent] = [
            TextContent(type="text", text=result.legacy_text)
        ]
        if result.screenshot is not None:
            content.append(
                ImageContent(
                    type="image",
                    data=base64.b64encode(result.screenshot).decode("ascii"),
                    mimeType="image/png",
                )
            )
        return CallToolResult(
            content=content,
            structuredContent=result.structured,
            isError=False,
        )


@mcp.tool(
    description="Return the Site Intelligence Profile (SIP) for a domain. "
    "Use this before difficult browser retrieval to learn page traits, recommended "
    "extract modes, human-assist expectations, known limits, and when the profile was observed."
)
async def get_site_profile(
    domain: Annotated[str, Field(description="Domain to inspect, e.g. xiaohongshu.com")],
) -> str:
    payload = site_rules.site_profile_payload(domain)
    return format_ok(json.dumps(payload, ensure_ascii=False, indent=2))


@mcp.tool(
    description="Return a retrieval readiness report for a URL or domain. "
    "Combines Site Intelligence Profile, account profiles, encrypted vault presence, "
    "and a recommended next MCP call before touching a difficult page."
)
async def get_retrieval_readiness(
    target: Annotated[str, Field(description="URL or domain to inspect")],
    account_id: Annotated[str, Field(description="Optional account profile id", default="")] = "",
    access_mode: Annotated[str, Field(description="'standard' or 'user_authorized'", default="user_authorized")] = "user_authorized",
) -> str:
    if access_mode not in {"standard", "user_authorized"}:
        return format_error("invalid-mode", f"Unsupported access_mode: {access_mode}")
    try:
        payload = retrieval_readiness.readiness_payload(
            target,
            account_id=account_id,
            access_mode=access_mode,
        )
    except ValueError as e:
        return format_error("invalid-mode", str(e))
    return format_ok(json.dumps(payload, ensure_ascii=False, indent=2))


@mcp.tool(
    description="Return the current policy broker decision for a planned tool call. "
    "Use this before high-risk calls when an agent needs to know whether live browser, "
    "CDP, crawl_site, artifact body reads, or a target domain are allowed."
)
async def get_policy_decision(
    tool: Annotated[str, Field(description="Planned MCP tool name, e.g. open_browser_session")],
    target_url: Annotated[str, Field(description="Optional target URL", default="")] = "",
    domain: Annotated[str, Field(description="Optional target domain when no URL is available", default="")] = "",
    access_mode: Annotated[str, Field(description="Optional access mode, e.g. standard or user_authorized", default="")] = "",
    requested_pages: Annotated[int, Field(description="Requested page count for crawl jobs; 0 means unspecified", default=0)] = 0,
    uses_live_browser: Annotated[bool, Field(description="Whether the planned call needs a live browser", default=False)] = False,
    uses_cdp: Annotated[bool, Field(description="Whether the planned call connects to CDP", default=False)] = False,
    reads_artifact_body: Annotated[bool, Field(description="Whether the planned call reads page.html or another body artifact", default=False)] = False,
    capture_artifact: Annotated[bool, Field(description="Whether the planned call captures debug artifacts", default=False)] = False,
) -> str:
    decision = _policy_check(
        tool,
        target_url=target_url,
        domain=domain,
        access_mode=access_mode,
        requested_pages=requested_pages or None,
        uses_live_browser=uses_live_browser,
        uses_cdp=uses_cdp,
        reads_artifact_body=reads_artifact_body,
        capture_artifact=capture_artifact,
    )
    return format_ok(json.dumps(decision.as_dict(), ensure_ascii=False, indent=2))


@mcp.tool(
    description="Register or update an account profile for a domain. "
    "This stores metadata and encrypted-state paths only; it never stores plaintext passwords. "
    "Use open_browser_session with the same account_id to let the human log in."
)
async def register_account_profile(
    domain: Annotated[str, Field(description="Domain, e.g. xiaohongshu.com")],
    account_id: Annotated[str, Field(description="Stable local account id, e.g. default, work, personal", default="default")] = "default",
    label: Annotated[str, Field(description="Human-readable account label", default="")] = "",
    login_method: Annotated[str, Field(description="'manual_qr', 'manual_password', or 'imported_state'", default="manual_qr")] = "manual_qr",
    notes: Annotated[str, Field(description="Operator notes; do not put passwords here", default="")] = "",
    make_default: Annotated[bool, Field(description="Make this the default profile for the domain", default=True)] = True,
) -> str:
    try:
        profile = account_profiles.register_profile(
            domain,
            account_id=account_id,
            label=label,
            login_method=login_method,
            notes=notes,
            make_default=make_default,
        )
    except ValueError as e:
        return format_error("invalid-mode", str(e))
    return format_ok(json.dumps({"ok": True, "profile": profile.as_dict()}, ensure_ascii=False, indent=2))


@mcp.tool(
    description="List account profiles. Results contain metadata and encrypted-state paths, not secrets."
)
async def list_account_profiles(
    domain: Annotated[str, Field(description="Optional domain filter", default="")] = "",
) -> str:
    try:
        payload = account_profiles.registry_payload(domain)
    except ValueError as e:
        return format_error("invalid-mode", str(e))
    return format_ok(json.dumps(payload, ensure_ascii=False, indent=2))


@mcp.tool(
    description="Mark an account profile status as active, expired, needs_login, or blocked. "
    "Use this when login expires, a site blocks an account, or a manual login has been refreshed."
)
async def mark_account_profile(
    domain: Annotated[str, Field(description="Domain for the account profile")],
    account_id: Annotated[str, Field(description="Account id to update", default="default")] = "default",
    status: Annotated[str, Field(description="'active', 'expired', 'needs_login', or 'blocked'", default="active")] = "active",
    notes: Annotated[str, Field(description="Operator notes; do not put passwords here", default="")] = "",
    expires_at: Annotated[str, Field(description="Optional ISO timestamp when this login expires", default="")] = "",
    risk_flags: Annotated[list[str] | None, Field(description="Optional risk flags", default=None)] = None,
) -> str:
    try:
        profile = account_profiles.mark_profile_status(
            domain,
            account_id,
            status,
            notes=notes,
            expires_at=expires_at,
            risk_flags=risk_flags,
        )
    except ValueError as e:
        return format_error("invalid-mode", str(e))
    return format_ok(json.dumps({"ok": True, "profile": profile.as_dict()}, ensure_ascii=False, indent=2))


@mcp.tool(
    description="Open a visible, human-operated browser session and keep it alive. "
    "Use this when a user needs to log in, pass verification, click through a UI, "
    "scroll, or otherwise prepare the exact page state before extraction. "
    "Returns a session_id. Then call extract_browser_session with that session_id."
)
async def open_browser_session(
    url: Annotated[str, Field(description="Full URL to open in the visible browser")],
    account_id: Annotated[str, Field(description="Account profile id to bind this browser state to", default="default")] = "default",
    access_mode: Annotated[str, Field(description="'standard' or 'user_authorized'", default="user_authorized")] = "user_authorized",
    use_proxy: Annotated[bool, Field(description="Use configured proxy", default=False)] = False,
    wait_until: Annotated[str, Field(description="Playwright wait state: commit, domcontentloaded, load, or networkidle", default="domcontentloaded")] = "domcontentloaded",
    timeout: Annotated[int, Field(description="Navigation timeout in seconds", default=60)] = 60,
) -> str:
    decision = _policy_check(
        "open_browser_session",
        target_url=url,
        access_mode=access_mode,
        uses_live_browser=True,
    )
    if not decision.allowed:
        return _policy_denied_text(decision)
    return await _open_browser_session(
        url,
        account_id=account_id,
        access_mode=access_mode,
        use_proxy=use_proxy,
        wait_until=wait_until,
        timeout=timeout,
    )


@mcp.tool(
    description="Connect to an existing Chromium/Chrome browser over CDP and keep a live MCP session. "
    "By default cdp_url must be localhost/127.0.0.1; remote CDP requires TRAWLER_ALLOW_REMOTE_CDP=1. "
    "If url is provided, Trawler navigates there after SSRF checks. Use this when the user already has "
    "a real browser profile/session open and wants Trawler to extract from that state."
)
async def connect_browser_session(
    cdp_url: Annotated[str, Field(description="CDP endpoint, usually http://127.0.0.1:9222")],
    url: Annotated[str, Field(description="Optional http/https URL to navigate after connecting", default="")] = "",
    account_id: Annotated[str, Field(description="Account profile id for metadata/persistence; default is external", default="external")] = "external",
    access_mode: Annotated[str, Field(description="'standard' or 'user_authorized'", default="user_authorized")] = "user_authorized",
    wait_until: Annotated[str, Field(description="Playwright wait state: commit, domcontentloaded, load, or networkidle", default="domcontentloaded")] = "domcontentloaded",
    timeout: Annotated[int, Field(description="Connection/navigation timeout in seconds", default=60)] = 60,
) -> str:
    decision = _policy_check(
        "connect_browser_session",
        target_url=url,
        access_mode=access_mode,
        uses_live_browser=True,
        uses_cdp=True,
    )
    if not decision.allowed:
        return _policy_denied_text(decision)
    return await _connect_browser_session(
        cdp_url,
        url=url,
        account_id=account_id,
        access_mode=access_mode,
        wait_until=wait_until,
        timeout=timeout,
    )


@mcp.tool(
    description="List currently open live browser sessions."
)
async def list_browser_sessions() -> str:
    return _list_browser_sessions()


@mcp.tool(
    description="Run user-directed actions in a live browser session before extraction. "
    "Actions are objects with type/action such as click, fill, type, press, scroll, wait, "
    "wait_for_selector, goto, check, uncheck, or select_option. Selector-based actions use CSS selectors. "
    "goto URLs are canonicalized and checked by the SSRF guard."
)
async def run_browser_actions(
    session_id: Annotated[str, Field(description="session_id returned by open_browser_session/connect_browser_session")],
    actions: Annotated[list[dict] | None, Field(description="Ordered browser action objects", default=None)] = None,
    wait_until: Annotated[str, Field(description="Navigation wait state for goto actions", default="domcontentloaded")] = "domcontentloaded",
    timeout: Annotated[int, Field(description="Default per-action timeout in seconds", default=30)] = 30,
) -> str:
    decision = _policy_check("run_browser_actions", uses_live_browser=True)
    if not decision.allowed:
        return _policy_denied_text(decision)
    return await _perform_browser_actions(
        session_id,
        actions,
        wait_until=wait_until,
        timeout=timeout,
    )


@mcp.tool(
    description="Observe the current live browser page without executing actions. "
    "Returns bounded actionable elements with stable selectors, roles, names, rectangles, "
    "and action hints; input values are intentionally not returned. Use this before "
    "run_browser_actions or extract_browser_session when an agent needs a safe page map."
)
async def observe_browser_session(
    session_id: Annotated[str, Field(description="session_id returned by open_browser_session/connect_browser_session")],
    selector: Annotated[str, Field(description="CSS selector that scopes observation", default="body")] = "body",
    max_elements: Annotated[int, Field(description="Max actionable elements to return, capped at 300", default=80)] = 80,
    include_accessibility: Annotated[bool, Field(description="Include an accessibility snapshot summary", default=True)] = True,
) -> str:
    decision = _policy_check("observe_browser_session", uses_live_browser=True)
    if not decision.allowed:
        return _policy_denied_text(decision)
    return await _observe_browser_session(
        session_id,
        selector=selector,
        max_elements=max_elements,
        include_accessibility=include_accessibility,
    )


@mcp.tool(
    description="Inject an element picker overlay into a live browser session. "
    "After this call, the human clicks an element in the visible browser. "
    "Then call extract_browser_session with extract_mode='picked_element'."
)
async def start_element_picker(
    session_id: Annotated[str, Field(description="session_id returned by open_browser_session")],
) -> str:
    decision = _policy_check("start_element_picker", uses_live_browser=True)
    if not decision.allowed:
        return _policy_denied_text(decision)
    return await _start_element_picker(session_id)


@mcp.tool(
    description="Inject a region picker overlay into a live browser session. "
    "After this call, the human drags a rectangle in the visible browser. "
    "Then call extract_browser_session with extract_mode='picked_region'."
)
async def start_region_picker(
    session_id: Annotated[str, Field(description="session_id returned by open_browser_session")],
) -> str:
    decision = _policy_check("start_region_picker", uses_live_browser=True)
    if not decision.allowed:
        return _policy_denied_text(decision)
    return await _start_region_picker(session_id)


@mcp.tool(
    description="Extract content from the current state of a live browser session. "
    "Use extract_mode='page' for markdown, 'visible_text' for rendered body text, "
    "'selector' for a CSS-selected element, 'screenshot' for image content, 'html' "
    "for the current DOM HTML, 'element_snapshot' for selector HTML plus key computed CSS, "
    "'picked_element'/'picked_region' after using the picker tools, or 'page_clone' "
    "for a bounded DOM tree with key computed CSS. Use 'accessibility_snapshot' for an AX/semantic tree, "
    "'visible_blocks' for card/list style rendered content, 'fit_markdown' for bounded markdown "
    "plus citations, or 'bundle' for a combined extraction bundle. "
    "Optionally pass actions to click/fill/scroll/goto before extraction. "
    "Set capture_artifact=true to save HTML/screenshot evidence. "
    "Set close_after=true when the human/browser task is finished."
)
async def extract_browser_session(
    session_id: Annotated[str, Field(description="session_id returned by open_browser_session")],
    extract_mode: Annotated[str, Field(description="'page', 'visible_text', 'selector', 'screenshot', 'html', 'element_snapshot', 'picked_element', 'picked_region', 'page_clone', 'accessibility_snapshot', 'visible_blocks', 'fit_markdown', or 'bundle'", default="page")] = "page",
    selector: Annotated[str, Field(description="CSS selector used when extract_mode='selector'", default="")] = "",
    actions: Annotated[list[dict] | None, Field(description="Optional ordered actions to run before extraction", default=None)] = None,
    action_timeout: Annotated[int, Field(description="Default per-action timeout in seconds", default=30)] = 30,
    wait_until: Annotated[str, Field(description="Navigation wait state for pre-extraction goto actions", default="domcontentloaded")] = "domcontentloaded",
    max_markdown_chars: Annotated[int, Field(description="Character budget for fit_markdown/bundle markdown", default=20000)] = 20000,
    capture_artifact: Annotated[bool, Field(description="Save debug artifact with HTML and screenshot", default=False)] = False,
    close_after: Annotated[bool, Field(description="Close this live browser session after extraction", default=False)] = False,
) -> CallToolResult:
    decision = _policy_check(
        "extract_browser_session",
        uses_live_browser=True,
        capture_artifact=capture_artifact or extract_mode in {
            "screenshot",
            "element_snapshot",
            "picked_element",
            "picked_region",
            "page_clone",
            "bundle",
        },
    )
    if not decision.allowed:
        return _policy_denied_result(decision)
    result = await _extract_browser_session(
        session_id,
        extract_mode=extract_mode,
        selector=selector,
        actions=actions,
        action_timeout=action_timeout,
        wait_until=wait_until,
        max_markdown_chars=max_markdown_chars,
        capture_artifact=capture_artifact,
        close_after=close_after,
    )
    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=result.legacy_text)
    ]
    if result.screenshot is not None:
        content.append(
            ImageContent(
                type="image",
                data=base64.b64encode(result.screenshot).decode("ascii"),
                mimeType="image/png",
            )
        )
    return CallToolResult(
        content=content,
        structuredContent=result.structured,
        isError=False,
    )


@mcp.tool(
    description="Close a live browser session and persist encrypted account state when configured."
)
async def close_browser_session(
    session_id: Annotated[str, Field(description="session_id returned by open_browser_session")],
) -> str:
    decision = _policy_check("close_browser_session", uses_live_browser=True)
    if not decision.allowed:
        return _policy_denied_text(decision)
    return await _close_browser_session(session_id)


@mcp.tool(
    description="Fetch a single web page and return clean markdown. "
    "Always returns a string: '__TRAWLER_OK__:\n\n<markdown>' on success, "
    "or '__TRAWLER_ERROR__:{json}' on failure. "
    "Blocks up to 35s. Use force_refresh or cache_mode='write_only' to bypass cache reads. "
    "For single-page, user-directed browser-equivalent access with authorization, set user_authorized_access=true. "
    "Use mode='toc' to get Table of Contents index, mode='section' for specific section, or mode='chunk' for token slicing."
)
async def crawl_url(
    url: Annotated[str, Field(description="Full URL to fetch (http/https)")],
    use_proxy: Annotated[bool, Field(description="Use configured proxy", default=False)] = False,
    force_refresh: Annotated[bool, Field(description="Deprecated alias for cache_mode='write_only'", default=False)] = False,
    cache_mode: Annotated[str, Field(description="Cache mode: enabled, read_only, write_only, bypass, or disabled", default="enabled")] = "enabled",
    bypass_robots: Annotated[bool, Field(description="Explicitly bypass robots.txt checks", default=False)] = False,
    user_authorized_access: Annotated[bool, Field(description="Single-page user-authorized browser-equivalent access; skips robots precheck and uses account/HITL flow when needed", default=False)] = False,
    account_id: Annotated[str, Field(description="Optional account profile id for user_authorized_access/HITL", default="")] = "",
    human_assist: Annotated[str, Field(description="Human assist policy: auto, required, or off", default="auto")] = "auto",
    selector: Annotated[str, Field(description="Request-level CSS selector for extraction", default="")] = "",
    capture_artifact: Annotated[bool, Field(description="Capture a browser debug artifact/screenshot when a browser rung is used", default=False)] = False,
    bypass_l3: Annotated[bool, Field(description="Skip empty-content detection (for SPAs/misdetected pages)", default=False)] = False,
    timeout: Annotated[int, Field(description="Max seconds to wait (increase for HITL)", default=35)] = 35,
    mode: Annotated[str, Field(description="Output mode: 'full' (default), 'toc' (table of contents), 'section' (specific section), or 'chunk' (token slice)", default="full")] = "full",
    section_id: Annotated[str, Field(description="Section ID or heading keyword (used with mode='section')", default="")] = "",
    chunk_index: Annotated[int, Field(description="Chunk page index (1-based, used with mode='chunk')", default=1)] = 1,
    keywords: Annotated[dict | None, Field(description="Temporary keyword filter for this call. Format: {\"include\": [...], \"exclude\": [...], \"regex\": [...]}. include=must match at least one; exclude=reject if any matches; regex=pattern match", default=None)] = None,
) -> str:
    decision = _policy_check(
        "crawl_url",
        target_url=url,
        access_mode="user_authorized" if user_authorized_access else "standard",
        uses_live_browser=user_authorized_access or human_assist != "off",
        capture_artifact=capture_artifact,
    )
    if not decision.allowed:
        return _policy_denied_text(decision)
    from trawler.otel import span_context
    from trawler.tracing import telemetry_context
    with telemetry_context("crawl_url", agent_id=account_id or ""), \
         span_context("crawl_url", url=url, account_id=account_id or ""):
        return await _crawl_url(
            url,
            use_proxy=use_proxy,
            force_refresh=force_refresh,
            cache_mode=cache_mode,
            bypass_robots=bypass_robots,
            user_authorized_access=user_authorized_access,
            account_id=account_id,
            human_assist=human_assist,
            selector=selector,
            capture_artifact=capture_artifact,
            bypass_l3=bypass_l3,
            timeout=timeout,
            mode=mode,
            section_id=section_id,
            chunk_index=chunk_index,
            keywords=keywords,
        )


@mcp.tool(
    description="Fetch a single web page and return both the legacy text result and MCP structuredContent. "
    "The text content preserves the '__TRAWLER_OK__:' / '__TRAWLER_ERROR__:{json}' contract; "
    "structuredContent includes ok, text, error, raw_id, metadata, artifact_id, cache_mode, mode, and links.",
    structured_output=False,
)
async def crawl_url_structured(
    url: Annotated[str, Field(description="Full URL to fetch (http/https)")],
    use_proxy: Annotated[bool, Field(description="Use configured proxy", default=False)] = False,
    force_refresh: Annotated[bool, Field(description="Deprecated alias for cache_mode='write_only'", default=False)] = False,
    cache_mode: Annotated[str, Field(description="Cache mode: enabled, read_only, write_only, bypass, or disabled", default="enabled")] = "enabled",
    bypass_robots: Annotated[bool, Field(description="Explicitly bypass robots.txt checks", default=False)] = False,
    user_authorized_access: Annotated[bool, Field(description="Single-page user-authorized browser-equivalent access; skips robots precheck and uses account/HITL flow when needed", default=False)] = False,
    account_id: Annotated[str, Field(description="Optional account profile id for user_authorized_access/HITL", default="")] = "",
    human_assist: Annotated[str, Field(description="Human assist policy: auto, required, or off", default="auto")] = "auto",
    selector: Annotated[str, Field(description="Request-level CSS selector for extraction", default="")] = "",
    capture_artifact: Annotated[bool, Field(description="Capture a browser debug artifact/screenshot when a browser rung is used", default=False)] = False,
    bypass_l3: Annotated[bool, Field(description="Skip empty-content detection (for SPAs/misdetected pages)", default=False)] = False,
    timeout: Annotated[int, Field(description="Max seconds to wait (increase for HITL)", default=35)] = 35,
    mode: Annotated[str, Field(description="Output mode: 'full' (default), 'toc', 'section', or 'chunk'", default="full")] = "full",
    section_id: Annotated[str, Field(description="Section ID or heading keyword (used with mode='section')", default="")] = "",
    chunk_index: Annotated[int, Field(description="Chunk page index (1-based, used with mode='chunk')", default=1)] = 1,
) -> CallToolResult:
    decision = _policy_check(
        "crawl_url_structured",
        target_url=url,
        access_mode="user_authorized" if user_authorized_access else "standard",
        uses_live_browser=user_authorized_access or human_assist != "off",
        capture_artifact=capture_artifact,
    )
    if not decision.allowed:
        return _policy_denied_result(decision)
    result = await _crawl_url(
        url,
        use_proxy=use_proxy,
        force_refresh=force_refresh,
        cache_mode=cache_mode,
        bypass_robots=bypass_robots,
        user_authorized_access=user_authorized_access,
        account_id=account_id,
        human_assist=human_assist,
        selector=selector,
        capture_artifact=capture_artifact,
        bypass_l3=bypass_l3,
        timeout=timeout,
        mode=mode,
        section_id=section_id,
        chunk_index=chunk_index,
    )
    return structured.crawl_result_to_call_result(
        input_url=url,
        result=result,
        cache_mode=cache_mode,
        force_refresh=force_refresh,
        mode=mode,
    )


@mcp.tool(
    name="crawl_site",
    description="Crawl a site starting from start_url, following same-domain links. "
    "Optional crawl policy: max_depth (-1 unlimited), include_paths/exclude_paths glob path filters, "
    "include_subdomains, and ignore_query_parameters. "
    "Returns immediately with {job_id}. Use wait_for_job(job_id) to get the aggregated result. "
    "max_pages default 20 (capped at 500)."
)
async def crawl_site_tool(
    start_url: Annotated[str, Field(description="Starting URL")],
    max_pages: Annotated[int, Field(description="Max pages to crawl (default 20, hard cap 500)", default=20)] = 20,
    same_domain_only: Annotated[bool, Field(description="Only follow same-domain links", default=True)] = True,
    use_proxy: Annotated[bool, Field(description="Use configured proxy", default=False)] = False,
    max_depth: Annotated[int, Field(description="Maximum link depth from start_url; -1 means unlimited", default=-1)] = -1,
    include_paths: Annotated[list[str] | None, Field(description="Only follow URL paths matching these glob patterns, e.g. ['/docs/*']", default=None)] = None,
    exclude_paths: Annotated[list[str] | None, Field(description="Do not follow URL paths matching these glob patterns", default=None)] = None,
    include_subdomains: Annotated[bool, Field(description="When same_domain_only=true, also allow subdomains of the start domain", default=False)] = False,
    ignore_query_parameters: Annotated[bool, Field(description="Drop query strings before enqueueing/deduplicating URLs", default=False)] = False,
) -> str:
    decision = _policy_check(
        "crawl_site",
        target_url=start_url,
        requested_pages=max_pages,
    )
    if not decision.allowed:
        return _policy_denied_text(decision)
    from trawler.otel import span_context
    from trawler.tracing import telemetry_context
    with telemetry_context("crawl_site"), \
         span_context("crawl_site", url=start_url, max_pages=max_pages):
        result = await crawl_site(
            start_url,
            max_pages=max_pages,
            same_domain_only=same_domain_only,
            use_proxy=use_proxy,
            max_depth=max_depth,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
            include_subdomains=include_subdomains,
            ignore_query_parameters=ignore_query_parameters,
        )
        return format_ok(
            f"Started crawl job: {result['job_id']} (max_pages={result['max_pages']}). "
            "Use wait_for_job to get results."
        )


@mcp.tool(
    description="Structured variant of crawl_site. Text preserves the legacy OK/error contract; "
    "structuredContent includes job_id, max_pages, seed_count, and crawl policy.",
    structured_output=False,
)
async def crawl_site_structured(
    start_url: Annotated[str, Field(description="Starting URL")],
    max_pages: Annotated[int, Field(description="Max pages to crawl (default 20, hard cap 500)", default=20)] = 20,
    same_domain_only: Annotated[bool, Field(description="Only follow same-domain links", default=True)] = True,
    use_proxy: Annotated[bool, Field(description="Use configured proxy", default=False)] = False,
    max_depth: Annotated[int, Field(description="Maximum link depth from start_url; -1 means unlimited", default=-1)] = -1,
    include_paths: Annotated[list[str] | None, Field(description="Only follow URL paths matching these glob patterns", default=None)] = None,
    exclude_paths: Annotated[list[str] | None, Field(description="Do not follow URL paths matching these glob patterns", default=None)] = None,
    include_subdomains: Annotated[bool, Field(description="When same_domain_only=true, also allow subdomains", default=False)] = False,
    ignore_query_parameters: Annotated[bool, Field(description="Drop query strings before enqueueing/deduplicating URLs", default=False)] = False,
) -> CallToolResult:
    decision = _policy_check(
        "crawl_site_structured",
        target_url=start_url,
        requested_pages=max_pages,
    )
    if not decision.allowed:
        return _policy_denied_result(decision)
    policy = _policy_payload(
        same_domain_only=same_domain_only,
        max_depth=max_depth,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        include_subdomains=include_subdomains,
        ignore_query_parameters=ignore_query_parameters,
    )
    result = await crawl_site(
        start_url,
        max_pages=max_pages,
        same_domain_only=same_domain_only,
        use_proxy=use_proxy,
        max_depth=max_depth,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        include_subdomains=include_subdomains,
        ignore_query_parameters=ignore_query_parameters,
    )
    legacy_text = format_ok(
        f"Started crawl job: {result['job_id']} (max_pages={result['max_pages']}). "
        "Use wait_for_job to get results."
    )
    return structured.call_tool_result(
        legacy_text,
        structured.StructuredCrawlJobResult(
            ok=True,
            text=legacy_text,
            job_id=result["job_id"],
            status=str(result.get("status") or "crawling"),
            max_pages=int(result["max_pages"]),
            seed_count=int(result.get("seed_count") or 0),
            policy=policy,
        ),
    )


@mcp.tool(
    description="Discover sitemap/feed seed URLs, then start a crawl_site job seeded from them. "
    "The same crawl policy parameters are applied to discovered seeds and later links. "
    "Returns immediately with {job_id}; use wait_for_job or get_job_results."
)
async def crawl_site_indexed(
    start_url: Annotated[str, Field(description="Starting URL")],
    max_pages: Annotated[int, Field(description="Max pages to crawl (default 20, hard cap 500)", default=20)] = 20,
    max_seed_urls: Annotated[int, Field(description="Max sitemap/feed URLs to seed into the frontier", default=200)] = 200,
    same_domain_only: Annotated[bool, Field(description="Only crawl same-domain links", default=True)] = True,
    use_proxy: Annotated[bool, Field(description="Use configured proxy", default=False)] = False,
    max_depth: Annotated[int, Field(description="Maximum link depth from each seed; -1 means unlimited", default=-1)] = -1,
    include_paths: Annotated[list[str] | None, Field(description="Only crawl URL paths matching these glob patterns, e.g. ['/docs/*']", default=None)] = None,
    exclude_paths: Annotated[list[str] | None, Field(description="Do not crawl URL paths matching these glob patterns", default=None)] = None,
    include_subdomains: Annotated[bool, Field(description="When same_domain_only=true, also allow subdomains of the start domain", default=False)] = False,
    ignore_query_parameters: Annotated[bool, Field(description="Drop query strings before enqueueing/deduplicating URLs", default=False)] = False,
) -> str:
    decision = _policy_check(
        "crawl_site_indexed",
        target_url=start_url,
        requested_pages=max_pages,
    )
    if not decision.allowed:
        return _policy_denied_text(decision)
    discovered = await _discover_site_index(
        start_url,
        max_urls=max_seed_urls,
        same_domain_only=same_domain_only,
        include_subdomains=include_subdomains,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        ignore_query_parameters=ignore_query_parameters,
        use_proxy=use_proxy,
    )
    if not discovered.get("ok"):
        return str(discovered.get("error") or format_error("map-failed", "Failed to discover site index"))
    seed_urls = discovered.get("urls") if isinstance(discovered.get("urls"), list) else []
    result = await crawl_site(
        start_url,
        max_pages=max_pages,
        same_domain_only=same_domain_only,
        use_proxy=use_proxy,
        seed_urls=[str(url) for url in seed_urls],
        max_depth=max_depth,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        include_subdomains=include_subdomains,
        ignore_query_parameters=ignore_query_parameters,
    )
    payload = {
        "job_id": result["job_id"],
        "max_pages": result["max_pages"],
        "seed_count": result["seed_count"],
        "discovered_url_count": discovered.get("url_count", 0),
        "sitemap_count": discovered.get("sitemap_count", 0),
        "feed_count": discovered.get("feed_count", 0),
    }
    return format_ok(json.dumps(payload, ensure_ascii=False, indent=2))


@mcp.tool(
    description="Structured variant of crawl_site_indexed. Text preserves the legacy JSON string result; "
    "structuredContent includes job_id, discovery counts, seed_count, and crawl policy.",
    structured_output=False,
)
async def crawl_site_indexed_structured(
    start_url: Annotated[str, Field(description="Starting URL")],
    max_pages: Annotated[int, Field(description="Max pages to crawl (default 20, hard cap 500)", default=20)] = 20,
    max_seed_urls: Annotated[int, Field(description="Max sitemap/feed URLs to seed into the frontier", default=200)] = 200,
    same_domain_only: Annotated[bool, Field(description="Only crawl same-domain links", default=True)] = True,
    use_proxy: Annotated[bool, Field(description="Use configured proxy", default=False)] = False,
    max_depth: Annotated[int, Field(description="Maximum link depth from each seed; -1 means unlimited", default=-1)] = -1,
    include_paths: Annotated[list[str] | None, Field(description="Only crawl URL paths matching these glob patterns", default=None)] = None,
    exclude_paths: Annotated[list[str] | None, Field(description="Do not crawl URL paths matching these glob patterns", default=None)] = None,
    include_subdomains: Annotated[bool, Field(description="When same_domain_only=true, also allow subdomains", default=False)] = False,
    ignore_query_parameters: Annotated[bool, Field(description="Drop query strings before enqueueing/deduplicating URLs", default=False)] = False,
) -> CallToolResult:
    decision = _policy_check(
        "crawl_site_indexed_structured",
        target_url=start_url,
        requested_pages=max_pages,
    )
    if not decision.allowed:
        return _policy_denied_result(decision)
    policy = _policy_payload(
        same_domain_only=same_domain_only,
        max_depth=max_depth,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        include_subdomains=include_subdomains,
        ignore_query_parameters=ignore_query_parameters,
    )
    discovered = await _discover_site_index(
        start_url,
        max_urls=max_seed_urls,
        same_domain_only=same_domain_only,
        include_subdomains=include_subdomains,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        ignore_query_parameters=ignore_query_parameters,
        use_proxy=use_proxy,
    )
    if not discovered.get("ok"):
        legacy_text = str(discovered.get("error") or format_error("map-failed", "Failed to discover site index"))
        return structured.call_tool_result(
            legacy_text,
            structured.StructuredCrawlJobResult(
                ok=False,
                error={"errorType": "map-failed", "message": legacy_text},
                policy=policy,
            ),
        )
    seed_urls = discovered.get("urls") if isinstance(discovered.get("urls"), list) else []
    result = await crawl_site(
        start_url,
        max_pages=max_pages,
        same_domain_only=same_domain_only,
        use_proxy=use_proxy,
        seed_urls=[str(url) for url in seed_urls],
        max_depth=max_depth,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        include_subdomains=include_subdomains,
        ignore_query_parameters=ignore_query_parameters,
    )
    legacy_payload = {
        "job_id": result["job_id"],
        "max_pages": result["max_pages"],
        "seed_count": result["seed_count"],
        "discovered_url_count": discovered.get("url_count", 0),
        "sitemap_count": discovered.get("sitemap_count", 0),
        "feed_count": discovered.get("feed_count", 0),
    }
    legacy_text = format_ok(json.dumps(legacy_payload, ensure_ascii=False, indent=2))
    return structured.call_tool_result(
        legacy_text,
        structured.StructuredCrawlJobResult(
            ok=True,
            text=legacy_text,
            job_id=result["job_id"],
            status=str(result.get("status") or "crawling"),
            max_pages=int(result["max_pages"]),
            seed_count=int(result.get("seed_count") or 0),
            discovered_url_count=int(discovered.get("url_count") or 0),
            sitemap_count=int(discovered.get("sitemap_count") or 0),
            feed_count=int(discovered.get("feed_count") or 0),
            policy=policy,
        ),
    )


@mcp.tool(
    name="map_site",
    description="Map links from a starting page without crawling every page. "
    "Fetches start_url once, extracts links from the raw DOM, and returns JSON. "
    "Accepts the same path/subdomain/query policy filters used by crawl_site. "
    "Use this before crawl_site when you need to inspect a site's shape."
)
async def map_site_tool(
    start_url: Annotated[str, Field(description="Starting URL")],
    max_links: Annotated[int, Field(description="Max links to return", default=200)] = 200,
    same_domain_only: Annotated[bool, Field(description="Only include same-domain links", default=True)] = True,
    use_proxy: Annotated[bool, Field(description="Use configured proxy", default=False)] = False,
    include_paths: Annotated[list[str] | None, Field(description="Only include URL paths matching these glob patterns", default=None)] = None,
    exclude_paths: Annotated[list[str] | None, Field(description="Exclude URL paths matching these glob patterns", default=None)] = None,
    include_subdomains: Annotated[bool, Field(description="When same_domain_only=true, also include subdomains", default=False)] = False,
    ignore_query_parameters: Annotated[bool, Field(description="Drop query strings before returning/deduplicating links", default=False)] = False,
) -> str:
    import json

    decision = _policy_check("map_site", target_url=start_url)
    if not decision.allowed:
        return _policy_denied_text(decision)
    result = await map_site(
        start_url,
        max_links=max_links,
        same_domain_only=same_domain_only,
        use_proxy=use_proxy,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        include_subdomains=include_subdomains,
        ignore_query_parameters=ignore_query_parameters,
    )
    if not result.get("ok"):
        return str(result.get("error") or format_error("map-failed", "Failed to map site"))
    return format_ok(json.dumps(result, ensure_ascii=False, indent=2))


@mcp.tool(
    description="Map links from a starting page and return both legacy text and MCP structuredContent. "
    "structuredContent includes ok, url, canonical_url, links, link_count, and error.",
    structured_output=False,
)
async def map_site_structured(
    start_url: Annotated[str, Field(description="Starting URL")],
    max_links: Annotated[int, Field(description="Max links to return", default=200)] = 200,
    same_domain_only: Annotated[bool, Field(description="Only include same-domain links", default=True)] = True,
    use_proxy: Annotated[bool, Field(description="Use configured proxy", default=False)] = False,
    include_paths: Annotated[list[str] | None, Field(description="Only include URL paths matching these glob patterns", default=None)] = None,
    exclude_paths: Annotated[list[str] | None, Field(description="Exclude URL paths matching these glob patterns", default=None)] = None,
    include_subdomains: Annotated[bool, Field(description="When same_domain_only=true, also include subdomains", default=False)] = False,
    ignore_query_parameters: Annotated[bool, Field(description="Drop query strings before returning/deduplicating links", default=False)] = False,
) -> CallToolResult:
    decision = _policy_check("map_site_structured", target_url=start_url)
    if not decision.allowed:
        return _policy_denied_result(decision)
    result = await map_site(
        start_url,
        max_links=max_links,
        same_domain_only=same_domain_only,
        use_proxy=use_proxy,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        include_subdomains=include_subdomains,
        ignore_query_parameters=ignore_query_parameters,
    )
    if not result.get("ok"):
        legacy_text = str(result.get("error") or format_error("map-failed", "Failed to map site"))
    else:
        legacy_text = format_ok(json.dumps(result, ensure_ascii=False, indent=2))
    return structured.map_result_to_call_result(
        start_url=start_url,
        legacy_text=legacy_text,
        result=result,
    )


@mcp.tool(
    name="discover_site_index",
    description="Discover sitemap and feed URLs for a site, then return bounded seed URLs as JSON. "
    "Use before crawl_site when the homepage does not expose the important pages."
)
async def discover_site_index_tool(
    start_url: Annotated[str, Field(description="Starting URL")],
    max_urls: Annotated[int, Field(description="Max discovered page URLs to return", default=200)] = 200,
    same_domain_only: Annotated[bool, Field(description="Only include same-domain URLs", default=True)] = True,
    include_paths: Annotated[list[str] | None, Field(description="Only include URL paths matching these glob patterns", default=None)] = None,
    exclude_paths: Annotated[list[str] | None, Field(description="Exclude URL paths matching these glob patterns", default=None)] = None,
    include_subdomains: Annotated[bool, Field(description="When same_domain_only=true, also include subdomains", default=False)] = False,
    ignore_query_parameters: Annotated[bool, Field(description="Drop query strings before returning/deduplicating URLs", default=False)] = False,
    use_proxy: Annotated[bool, Field(description="Use configured proxy", default=False)] = False,
    timeout: Annotated[int, Field(description="HTTP timeout in seconds", default=10)] = 10,
) -> str:
    decision = _policy_check("discover_site_index", target_url=start_url)
    if not decision.allowed:
        return _policy_denied_text(decision)
    result = await _discover_site_index(
        start_url,
        max_urls=max_urls,
        same_domain_only=same_domain_only,
        include_subdomains=include_subdomains,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        ignore_query_parameters=ignore_query_parameters,
        use_proxy=use_proxy,
        timeout=timeout,
    )
    if not result.get("ok"):
        return str(result.get("error") or format_error("map-failed", "Failed to discover site index"))
    return format_ok(json.dumps(result, ensure_ascii=False, indent=2))


@mcp.tool(
    description="Structured variant of discover_site_index. Text preserves the legacy JSON string result; "
    "structuredContent includes discovered URLs, sitemap/feed URLs, counts, and errors.",
    structured_output=False,
)
async def discover_site_index_structured(
    start_url: Annotated[str, Field(description="Starting URL")],
    max_urls: Annotated[int, Field(description="Max discovered page URLs to return", default=200)] = 200,
    same_domain_only: Annotated[bool, Field(description="Only include same-domain URLs", default=True)] = True,
    include_paths: Annotated[list[str] | None, Field(description="Only include URL paths matching these glob patterns", default=None)] = None,
    exclude_paths: Annotated[list[str] | None, Field(description="Exclude URL paths matching these glob patterns", default=None)] = None,
    include_subdomains: Annotated[bool, Field(description="When same_domain_only=true, also include subdomains", default=False)] = False,
    ignore_query_parameters: Annotated[bool, Field(description="Drop query strings before returning/deduplicating URLs", default=False)] = False,
    use_proxy: Annotated[bool, Field(description="Use configured proxy", default=False)] = False,
    timeout: Annotated[int, Field(description="HTTP timeout in seconds", default=10)] = 10,
) -> CallToolResult:
    decision = _policy_check("discover_site_index_structured", target_url=start_url)
    if not decision.allowed:
        return _policy_denied_result(decision)
    result = await _discover_site_index(
        start_url,
        max_urls=max_urls,
        same_domain_only=same_domain_only,
        include_subdomains=include_subdomains,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        ignore_query_parameters=ignore_query_parameters,
        use_proxy=use_proxy,
        timeout=timeout,
    )
    if not result.get("ok"):
        legacy_text = str(result.get("error") or format_error("map-failed", "Failed to discover site index"))
        return structured.call_tool_result(
            legacy_text,
            structured.StructuredSiteIndexResult(
                ok=False,
                error=structured.error_payload(legacy_text),
                start_url=start_url,
                errors=result.get("errors") if isinstance(result.get("errors"), list) else [],
            ),
        )
    legacy_text = format_ok(json.dumps(result, ensure_ascii=False, indent=2))
    urls = result.get("urls") if isinstance(result.get("urls"), list) else []
    sitemap_urls = result.get("sitemap_urls") if isinstance(result.get("sitemap_urls"), list) else []
    feed_urls = result.get("feed_urls") if isinstance(result.get("feed_urls"), list) else []
    errors = result.get("errors") if isinstance(result.get("errors"), list) else []
    return structured.call_tool_result(
        legacy_text,
        structured.StructuredSiteIndexResult(
            ok=True,
            text=unwrap_ok(legacy_text),
            start_url=str(result.get("start_url") or start_url),
            urls=[str(url) for url in urls],
            sitemap_urls=[str(url) for url in sitemap_urls],
            feed_urls=[str(url) for url in feed_urls],
            url_count=int(result.get("url_count") or len(urls)),
            sitemap_count=int(result.get("sitemap_count") or len(sitemap_urls)),
            feed_count=int(result.get("feed_count") or len(feed_urls)),
            errors=errors,
        ),
    )


@mcp.tool(
    name="wait_for_job",
    description="Block-wait for an async crawl_site job to finish, then return aggregated markdown. "
    "Sends progress notifications to keep the connection alive. Default timeout 120s."
)
async def wait_for_job_tool(
    job_id: Annotated[str, Field(description="Job ID from crawl_site")],
    timeout: Annotated[int, Field(description="Max seconds to wait", default=120)] = 120,
    ctx: Context | None = None,
) -> str:
    async def progress_cb(completed: int, total: int) -> None:
        if ctx is not None:
            try:
                await ctx.report_progress(completed, total)
            except Exception:
                pass  # progress 是 best-effort
    return await wait_for_job(job_id, timeout=timeout, progress_cb=progress_cb)


@mcp.tool(
    description="Structured variant of wait_for_job. Text preserves the aggregated legacy result; "
    "structuredContent includes ok/error and the final known job status.",
    structured_output=False,
)
async def wait_for_job_structured(
    job_id: Annotated[str, Field(description="Job ID from crawl_site")],
    timeout: Annotated[int, Field(description="Max seconds to wait", default=120)] = 120,
    ctx: Context | None = None,
) -> CallToolResult:
    async def progress_cb(completed: int, total: int) -> None:
        if ctx is not None:
            try:
                await ctx.report_progress(completed, total)
            except Exception:
                pass

    legacy_text = await wait_for_job(job_id, timeout=timeout, progress_cb=progress_cb)
    job = get_job_status(job_id) or {}
    if is_ok(legacy_text):
        return structured.call_tool_result(
            legacy_text,
            structured.StructuredJobStatusResult(
                ok=True,
                text=unwrap_ok(legacy_text),
                job_id=job_id,
                status=str(job.get("status") or ""),
                completed=int(job.get("completed") or 0),
                total=int(job.get("total") or 0),
                updated_at=str(job.get("updated_at") or ""),
                frontier=job.get("frontier") if isinstance(job.get("frontier"), dict) else {},
            ),
        )
    return structured.call_tool_result(
        legacy_text,
        structured.StructuredJobStatusResult(
            ok=False,
            error=structured.error_payload(legacy_text),
            job_id=job_id,
            status=str(job.get("status") or ""),
            completed=int(job.get("completed") or 0),
            total=int(job.get("total") or 0),
            updated_at=str(job.get("updated_at") or ""),
            frontier=job.get("frontier") if isinstance(job.get("frontier"), dict) else {},
        ),
    )


@mcp.tool(
    name="get_job_status",
    description="Peek at an async crawl_site job's current progress (non-blocking). "
    "Returns status, completed/total pages."
)
async def get_job_status_tool(
    job_id: Annotated[str, Field(description="Job ID to check")],
) -> str:
    job = get_job_status(job_id)
    if job is None:
        return format_error("job-not-found", f"Job {job_id} not found")
    return format_ok(
        f"Job {job_id}: status={job['status']} "
        f"completed={job['completed']}/{job['total']} updated={job['updated_at']} "
        f"frontier={job.get('frontier', {})}"
    )


@mcp.tool(
    description="Structured variant of get_job_status. Returns job status/progress/frontier counts.",
    structured_output=False,
)
async def get_job_status_structured(
    job_id: Annotated[str, Field(description="Job ID to check")],
) -> CallToolResult:
    job = get_job_status(job_id)
    if job is None:
        legacy_text = format_error("job-not-found", f"Job {job_id} not found")
        return structured.call_tool_result(
            legacy_text,
            structured.StructuredJobStatusResult(
                ok=False,
                error=structured.error_payload(legacy_text),
                job_id=job_id,
            ),
        )
    legacy_text = format_ok(
        f"Job {job_id}: status={job['status']} "
        f"completed={job['completed']}/{job['total']} updated={job['updated_at']} "
        f"frontier={job.get('frontier', {})}"
    )
    return structured.call_tool_result(
        legacy_text,
        structured.StructuredJobStatusResult(
            ok=True,
            text=unwrap_ok(legacy_text),
            job_id=job_id,
            status=str(job.get("status") or ""),
            completed=int(job.get("completed") or 0),
            total=int(job.get("total") or 0),
            updated_at=str(job.get("updated_at") or ""),
            frontier=job.get("frontier") if isinstance(job.get("frontier"), dict) else {},
        ),
    )


@mcp.tool(
    name="cancel_job",
    description="Cancel a running crawl_site job. Returns whether a job/task was cancelled."
)
async def cancel_job_tool(
    job_id: Annotated[str, Field(description="Job ID to cancel")],
) -> str:
    cancelled = cancel_job(job_id)
    if not cancelled:
        return format_error("job-not-found", f"Job {job_id} not found or already stopped")
    return format_ok(f"Job {job_id} cancelled.")


@mcp.tool(
    name="get_job_errors",
    description="Return recent per-URL errors for a crawl_site job from the persistent frontier."
)
async def get_job_errors_tool(
    job_id: Annotated[str, Field(description="Job ID to inspect")],
    limit: Annotated[int, Field(description="Max errors to return", default=50)] = 50,
) -> str:
    import json

    if get_job_status(job_id) is None:
        return format_error("job-not-found", f"Job {job_id} not found")
    return format_ok(json.dumps(get_job_errors(job_id, limit=limit), ensure_ascii=False, indent=2))


@mcp.tool(
    description="Structured variant of get_job_errors. Returns recent per-URL error rows.",
    structured_output=False,
)
async def get_job_errors_structured(
    job_id: Annotated[str, Field(description="Job ID to inspect")],
    limit: Annotated[int, Field(description="Max errors to return", default=50)] = 50,
) -> CallToolResult:
    if get_job_status(job_id) is None:
        legacy_text = format_error("job-not-found", f"Job {job_id} not found")
        return structured.call_tool_result(
            legacy_text,
            structured.StructuredJobItemsResult(
                ok=False,
                error=structured.error_payload(legacy_text),
                job_id=job_id,
            ),
        )
    items = get_job_errors(job_id, limit=limit)
    legacy_text = format_ok(json.dumps(items, ensure_ascii=False, indent=2))
    return structured.call_tool_result(
        legacy_text,
        structured.StructuredJobItemsResult(
            ok=True,
            text=unwrap_ok(legacy_text),
            job_id=job_id,
            items=items,
            count=len(items),
        ),
    )


@mcp.tool(
    name="get_job_results",
    description="Return a paginated list of fetched/error URLs for a crawl_site job. "
    "Use next_cursor for subsequent pages."
)
async def get_job_results_tool(
    job_id: Annotated[str, Field(description="Job ID to inspect")],
    cursor: Annotated[int, Field(description="Offset cursor returned by previous call", default=0)] = 0,
    limit: Annotated[int, Field(description="Page size, max 100", default=20)] = 20,
) -> str:
    import json

    if get_job_status(job_id) is None:
        return format_error("job-not-found", f"Job {job_id} not found")
    page = get_job_results(job_id, cursor=cursor, limit=limit)
    return format_ok(json.dumps(page, ensure_ascii=False, indent=2))


@mcp.tool(
    description="Structured variant of get_job_results. Returns paginated fetched/error URL rows.",
    structured_output=False,
)
async def get_job_results_structured(
    job_id: Annotated[str, Field(description="Job ID to inspect")],
    cursor: Annotated[int, Field(description="Offset cursor returned by previous call", default=0)] = 0,
    limit: Annotated[int, Field(description="Page size, max 100", default=20)] = 20,
) -> CallToolResult:
    if get_job_status(job_id) is None:
        legacy_text = format_error("job-not-found", f"Job {job_id} not found")
        return structured.call_tool_result(
            legacy_text,
            structured.StructuredJobItemsResult(
                ok=False,
                error=structured.error_payload(legacy_text),
                job_id=job_id,
            ),
        )
    page = get_job_results(job_id, cursor=cursor, limit=limit)
    items = page.get("items") if isinstance(page.get("items"), list) else []
    legacy_text = format_ok(json.dumps(page, ensure_ascii=False, indent=2))
    next_cursor = page.get("next_cursor")
    return structured.call_tool_result(
        legacy_text,
        structured.StructuredJobItemsResult(
            ok=True,
            text=unwrap_ok(legacy_text),
            job_id=job_id,
            items=items,
            next_cursor=next_cursor if isinstance(next_cursor, int) else None,
            count=len(items),
        ),
    )


@mcp.tool(
    description="List all raw markdown files in the archive. "
    "Returns list of {raw_id, blocked, size}. Use get_raw to read one."
)
async def list_raw() -> str:
    items = _list_raw()
    if not items:
        return format_ok("No raw files yet. Use crawl_url first.")
    lines = [f"{i['raw_id']} {'[BLOCKED]' if i['blocked'] else ''} ({i['size']}b)" for i in items]
    return format_ok("Raw files:\n" + "\n".join(lines))


@mcp.tool(
    description="Structured variant of list_raw. Returns raw archive items as structuredContent.",
    structured_output=False,
)
async def list_raw_structured() -> CallToolResult:
    items = _list_raw()
    legacy_text = (
        format_ok("No raw files yet. Use crawl_url first.")
        if not items
        else format_ok(json.dumps(items, ensure_ascii=False, indent=2))
    )
    return structured.call_tool_result(
        legacy_text,
        structured.StructuredItemsResult(
            ok=True,
            text=unwrap_ok(legacy_text),
            items=items,
            count=len(items),
        ),
    )


@mcp.tool(
    description="Read a raw markdown file by path or raw_id. "
    "Path is restricted to the raw/ directory (traversal-safe)."
)
async def get_raw(
    path: Annotated[str, Field(description="Path to raw file, or raw_id")],
    mode: Annotated[str, Field(description="Read mode: full, toc, section, or chunk", default="full")] = "full",
    section_id: Annotated[str, Field(description="Section ID or heading keyword (used with mode='section')", default="")] = "",
    chunk_index: Annotated[int, Field(description="Chunk page index (1-based, used with mode='chunk')", default=1)] = 1,
    include_frontmatter: Annotated[bool, Field(description="Keep YAML frontmatter in mode='full'", default=True)] = True,
) -> str:
    # 如果传的是 raw_id 而非完整路径, 补全
    decision = _policy_check("get_raw")
    if not decision.allowed:
        return _policy_denied_text(decision)
    try:
        if "/" not in path and "\\" not in path:
            from trawler.raw_store import raw_path
            path = str(raw_path(path))
        raw_content = _get_raw(path)
        if mode == "full":
            return format_ok(raw_content if include_frontmatter else _strip_frontmatter(raw_content))

        from trawler.errors import is_error
        from trawler.parser import chunker

        body = _strip_frontmatter(raw_content)
        if mode == "toc":
            return format_ok(chunker.generate_toc(body))
        if mode == "section":
            section = chunker.slice_by_section(body, section_id or "Section 1")
            if is_error(section):
                return section
            return format_ok(section)
        if mode == "chunk":
            return format_ok(chunker.slice_by_tokens(body, chunk_index=chunk_index))
        return format_error("invalid-mode", f"Unsupported mode: {mode}")
    except ValueError as e:
        return format_error("invalid-url", str(e))
    except PermissionError as e:
        return format_error("permission-denied", str(e))
    except FileNotFoundError as e:
        return format_error("raw-not-found", str(e))


@mcp.tool(
    description="Read only YAML frontmatter metadata for a raw_id or URL. "
    "Use this before get_raw when you need provenance, links, quality signals, or artifact_id."
)
async def get_raw_metadata(
    identifier: Annotated[str, Field(description="raw_id or full URL")],
) -> str:
    decision = _policy_check("get_raw_metadata")
    if not decision.allowed:
        return _policy_denied_text(decision)
    import re

    raw_id = ""
    if "://" in identifier:
        raw_id = _url_id(identifier)
    elif re.fullmatch(r"[a-zA-Z0-9_\-]+", identifier):
        raw_id = identifier
    else:
        return format_error("invalid-url", "identifier must be a raw_id or full URL")

    metadata = _read_raw_metadata(raw_id)
    if not metadata:
        return format_error("raw-not-found", f"Raw metadata not found: {identifier}")
    payload = {"raw_id": raw_id, "metadata": metadata}
    return format_ok(json.dumps(payload, ensure_ascii=False, indent=2))


@mcp.tool(
    description="Structured variant of get_raw_metadata. Returns raw_id and frontmatter metadata.",
    structured_output=False,
)
async def get_raw_metadata_structured(
    identifier: Annotated[str, Field(description="raw_id or full URL")],
) -> CallToolResult:
    decision = _policy_check("get_raw_metadata_structured")
    if not decision.allowed:
        return _policy_denied_result(decision)
    import re

    raw_id = ""
    if "://" in identifier:
        raw_id = _url_id(identifier)
    elif re.fullmatch(r"[a-zA-Z0-9_\-]+", identifier):
        raw_id = identifier
    else:
        legacy_text = format_error("invalid-url", "identifier must be a raw_id or full URL")
        return structured.call_tool_result(
            legacy_text,
            structured.StructuredRawMetadataResult(
                ok=False,
                error=structured.error_payload(legacy_text),
            ),
        )

    metadata = _read_raw_metadata(raw_id)
    if not metadata:
        legacy_text = format_error("raw-not-found", f"Raw metadata not found: {identifier}")
        return structured.call_tool_result(
            legacy_text,
            structured.StructuredRawMetadataResult(
                ok=False,
                error=structured.error_payload(legacy_text),
                raw_id=raw_id,
            ),
        )

    payload = {"raw_id": raw_id, "metadata": metadata}
    legacy_text = format_ok(json.dumps(payload, ensure_ascii=False, indent=2))
    return structured.call_tool_result(
        legacy_text,
        structured.StructuredRawMetadataResult(
            ok=True,
            text=unwrap_ok(legacy_text),
            raw_id=raw_id,
            metadata=metadata,
        ),
    )


@mcp.tool(
    description="List recent debug artifacts captured for failed or sampled fetches. "
    "Use get_artifact to read metadata.json, page.html, console.json, or request_failures.json."
)
async def list_artifacts(
    limit: Annotated[int, Field(description="Max artifacts to return, capped at 500", default=50)] = 50,
) -> str:
    import json

    return format_ok(json.dumps(_list_artifacts(limit=limit), ensure_ascii=False, indent=2))


@mcp.tool(
    description="Structured variant of list_artifacts. Returns artifact metadata rows.",
    structured_output=False,
)
async def list_artifacts_structured(
    limit: Annotated[int, Field(description="Max artifacts to return, capped at 500", default=50)] = 50,
) -> CallToolResult:
    items = _list_artifacts(limit=limit)
    legacy_text = format_ok(json.dumps(items, ensure_ascii=False, indent=2))
    return structured.call_tool_result(
        legacy_text,
        structured.StructuredItemsResult(
            ok=True,
            text=unwrap_ok(legacy_text),
            items=items,
            count=len(items),
        ),
    )


@mcp.tool(
    description="Read a text file from a debug artifact. "
    "Allowed files: metadata.json, page.html, console.json, request_failures.json."
)
async def get_artifact(
    artifact_id: Annotated[str, Field(description="Artifact ID from an error payload or list_artifacts")],
    file_name: Annotated[str, Field(description="Text file to read", default="metadata.json")] = "metadata.json",
) -> str:
    try:
        decision = _policy_check(
            "get_artifact",
            reads_artifact_body=file_name == "page.html",
        )
        if not decision.allowed:
            return _policy_denied_text(decision)
        if file_name == "page.html" and not config.EXPOSE_ARTIFACT_BODIES:
            return format_error(
                "permission-denied",
                "artifact page.html body access is disabled; use get_artifact_summary "
                "or set TRAWLER_EXPOSE_ARTIFACT_BODIES=1 for trusted local debugging",
            )
        return format_ok(_read_artifact(artifact_id, file_name=file_name))
    except ValueError as e:
        return format_error("invalid-artifact", str(e))
    except PermissionError as e:
        return format_error("permission-denied", str(e))
    except FileNotFoundError as e:
        return format_error("artifact-not-found", str(e))


@mcp.tool(
    description="Return screenshot.png from a debug artifact as MCP image content. "
    "Use get_artifact_summary first to check whether screenshot.png exists."
)
async def get_artifact_screenshot(
    artifact_id: Annotated[str, Field(description="Artifact ID from an error payload or list_artifacts")],
) -> CallToolResult:
    decision = _policy_check("get_artifact_screenshot")
    if not decision.allowed:
        return _policy_denied_result(decision)
    try:
        data = _read_artifact_screenshot(artifact_id)
        legacy_text = format_ok(
            json.dumps(
                {
                    "artifact_id": artifact_id,
                    "file_name": "screenshot.png",
                    "bytes": len(data),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return CallToolResult(
            content=[
                TextContent(type="text", text=legacy_text),
                ImageContent(
                    type="image",
                    data=base64.b64encode(data).decode("ascii"),
                    mimeType="image/png",
                ),
            ],
            structuredContent={
                "ok": True,
                "artifact_id": artifact_id,
                "file_name": "screenshot.png",
                "bytes": len(data),
            },
            isError=False,
        )
    except ValueError as e:
        legacy_text = format_error("invalid-artifact", str(e))
    except PermissionError as e:
        legacy_text = format_error("permission-denied", str(e))
    except FileNotFoundError as e:
        legacy_text = format_error("artifact-not-found", str(e))
    except OSError as e:
        legacy_text = format_error("internal-error", f"Failed to read artifact screenshot: {e}")
    return CallToolResult(
        content=[TextContent(type="text", text=legacy_text)],
        structuredContent={
            "ok": False,
            "error": structured.error_payload(legacy_text),
            "artifact_id": artifact_id,
        },
        isError=False,
    )


@mcp.tool(
    description="Summarize a debug artifact without returning large bodies. "
    "Returns metadata, diagnostic counts, and file names/sizes. "
    "Use get_artifact only after this if you need metadata.json, page.html, console.json, or request_failures.json."
)
async def get_artifact_summary(
    artifact_id: Annotated[str, Field(description="Artifact ID from an error payload or list_artifacts")],
) -> str:
    decision = _policy_check("get_artifact_summary")
    if not decision.allowed:
        return _policy_denied_text(decision)
    try:
        return format_ok(json.dumps(_artifact_summary(artifact_id), ensure_ascii=False, indent=2))
    except ValueError as e:
        return format_error("invalid-artifact", str(e))
    except PermissionError as e:
        return format_error("permission-denied", str(e))
    except FileNotFoundError as e:
        return format_error("artifact-not-found", str(e))
    except OSError as e:
        return format_error("internal-error", f"Failed to summarize artifact: {e}")


@mcp.tool(
    description="Structured variant of get_artifact_summary.",
    structured_output=False,
)
async def get_artifact_summary_structured(
    artifact_id: Annotated[str, Field(description="Artifact ID from an error payload or list_artifacts")],
) -> CallToolResult:
    decision = _policy_check("get_artifact_summary_structured")
    if not decision.allowed:
        return _policy_denied_result(decision)
    try:
        summary = _artifact_summary(artifact_id)
        legacy_text = format_ok(json.dumps(summary, ensure_ascii=False, indent=2))
        return structured.call_tool_result(
            legacy_text,
            structured.StructuredArtifactSummaryResult(
                ok=True,
                text=unwrap_ok(legacy_text),
                artifact_id=str(summary.get("artifact_id") or artifact_id),
                summary=summary,
            ),
        )
    except ValueError as e:
        legacy_text = format_error("invalid-artifact", str(e))
    except PermissionError as e:
        legacy_text = format_error("permission-denied", str(e))
    except FileNotFoundError as e:
        legacy_text = format_error("artifact-not-found", str(e))
    except OSError as e:
        legacy_text = format_error("internal-error", f"Failed to summarize artifact: {e}")
    return structured.call_tool_result(
        legacy_text,
        structured.StructuredArtifactSummaryResult(
            ok=False,
            error=structured.error_payload(legacy_text),
            artifact_id=artifact_id,
        ),
    )


@mcp.tool(
    name="cleanup_artifacts",
    description="Clean up debug artifacts by age and/or total size. "
    "Defaults to dry_run=true; set dry_run=false to delete eligible artifact directories."
)
async def cleanup_artifacts_tool(
    dry_run: Annotated[bool, Field(description="Report candidates without deleting", default=True)] = True,
    max_age_days: Annotated[int, Field(description="Delete artifacts older than this many days; -1 disables age cleanup", default=-1)] = -1,
    max_total_bytes: Annotated[int, Field(description="Keep artifact storage under this many bytes; -1 disables size cleanup", default=-1)] = -1,
) -> str:
    decision = _policy_check("cleanup_artifacts", dry_run=dry_run)
    if not decision.allowed:
        return _policy_denied_text(decision)
    age_limit = None if max_age_days == -1 else max_age_days
    size_limit = None if max_total_bytes == -1 else max_total_bytes
    try:
        result = _cleanup_artifacts(
            dry_run=dry_run,
            max_age_days=age_limit,
            max_total_bytes=size_limit,
        )
        result["current_total_bytes"] = _artifact_dir_size()
        return format_ok(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, PermissionError) as e:
        return format_error("internal-error", f"Failed to clean artifacts: {e}")


@mcp.tool(
    description="Check engine health: storage paths, DB, recent errors, patchright availability."
)
async def get_engine_status() -> str:
    from trawler.audit import recent_errors
    from trawler.fetcher import curlcffi_rung, patchright_rung
    conn = db.connect()
    try:
        errors = recent_errors(conn, 10)
        # 磁盘
        import shutil
        disk = shutil.disk_usage(str(config.RAW_DIR))
        raw_count = len(list(config.RAW_DIR.glob("*.md")))
        artifact_count = len(list(config.ARTIFACT_DIR.glob("*/metadata.json")))
        # raw 文件数
        raw_count = len(list(config.RAW_DIR.glob("*.md")))
        # 域规则数
        rule_count = conn.execute("SELECT COUNT(*) FROM domain_rules").fetchone()[0]
        # 错误类型统计
        error_counts = conn.execute(
            "SELECT status, COUNT(*) FROM audit_log WHERE status NOT IN ('ok', 'cache_hit') GROUP BY status"
        ).fetchall()
        error_stats = {row[0]: row[1] for row in error_counts}
    finally:
        conn.close()
    try:
        import os

        import psutil
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        rss_mb = mem_info.rss / (1024 * 1024)
        threads = process.num_threads()
        proc_stats = f"process: RSS {rss_mb:.1f} MB, threads: {threads}"
    except ImportError:
        proc_stats = "psutil not installed"

    error_summary = ", ".join(f"{k}: {v}" for k, v in error_stats.items()) if error_stats else "none"

    from trawler.errors import VALID_ERROR_TYPES

    return format_ok(
        f"Trawler Engine Status\n"
        f"=====================\n"
        f"curl_cffi available: {curlcffi_rung.CURLCFFI_AVAILABLE}\n"
        f"patchright available: {patchright_rung.PATCHRIGHT_AVAILABLE}\n"
        f"anti-detect: {patchright_rung._ANTI_DETECT}\n"
        f"{proc_stats}\n"
        f"data dir: {config.DATA_DIR}\n"
        f"raw files: {raw_count}\n"
        f"debug artifacts: {artifact_count} (mode={config.DEBUG_ARTIFACTS})\n"
        f"domain rules: {rule_count}\n"
        f"disk free: {disk.free // (1024*1024)} MB / {disk.total // (1024*1024)} MB\n"
        f"error distribution: {error_summary}\n"
        f"supported error_types (schema): {', '.join(VALID_ERROR_TYPES)}\n"
        f"recent errors ({len(errors)}): " + ("none" if not errors else
        "\n  " + "\n  ".join(f"{e['ts']} {e['tool']} {e['url']}: {e['status']}" for e in errors[:5]))
    )


# ── Resource ──────────────────────────────────────────────────────

@mcp.resource("raw://{raw_id}")
async def read_raw_resource(raw_id: str) -> str:
    """Read a raw markdown file by raw_id. Exposes raw/ as MCP resources for client file-tree UI.

    #7 路径穿越防护: 复用 get_raw 的 pathlib.resolve 白名单 (防 ../和UNC)。
    raw_id 仅允许 [a-zA-Z0-9_-], 拒绝任何路径分隔符。
    """
    import asyncio
    import re
    # raw_id 只允许安全字符 (防 ../../../etc/passwd)
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", raw_id):
        return format_error("invalid-url", "Invalid raw_id: only [a-zA-Z0-9_-] allowed")
    from trawler.raw_store import get_raw, raw_path
    p = raw_path(raw_id)
    if not p.exists():
        return format_error("raw-not-found", f"Raw file not found: {raw_id}")
    try:
        return await asyncio.to_thread(get_raw, str(p))  # 复用白名单校验
    except PermissionError as e:
        return format_error("permission-denied", str(e))


@mcp.resource("artifact://{artifact_id}")
async def read_artifact_resource(artifact_id: str) -> str:
    """Read a debug artifact's metadata.json."""
    import re

    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", artifact_id):
        return format_error("invalid-artifact", "Invalid artifact_id: only [a-zA-Z0-9_-] allowed")
    try:
        return _read_artifact(artifact_id, "metadata.json")
    except ValueError as e:
        return format_error("invalid-artifact", str(e))
    except PermissionError as e:
        return format_error("permission-denied", str(e))
    except FileNotFoundError as e:
        return format_error("artifact-not-found", str(e))


@mcp.resource("recent://scrapes")
async def recent_scrapes_resource() -> str:
    """List the 50 most recently scraped URLs with raw_ids and timestamps.

    LLMs consult this before calling crawl_url to avoid re-fetching cached pages,
    reducing token spend by ~20-30%.
    """
    import asyncio

    def _query():
        conn = db.connect()
        try:
            return conn.execute(
                "SELECT url, raw_id, crawled_at FROM seen_urls "
                "WHERE raw_id IS NOT NULL AND raw_id != '' "
                "ORDER BY crawled_at DESC LIMIT 50"
            ).fetchall()
        finally:
            conn.close()

    try:
        rows = await asyncio.to_thread(_query)
    except Exception:
        return format_error("internal-error", "Failed to query recent scrapes")

    if not rows:
        return "No scrapes yet. Use crawl_url to fetch pages."

    lines = [f"{r['url']}\t→ {r['raw_id']} (crawled: {r['crawled_at']})" for r in rows]
    return f"Recent scrapes ({len(rows)}):\n" + "\n".join(lines)


# ── 关键词规则 Resource ───────────────────────────────────────────

@mcp.resource("keyword-rules://{scope}")
async def keyword_rules_resource(scope: str) -> str:
    """List keyword rules by scope. Use 'all' for everything, 'global' for global rules,
    or 'domain:example.com' for domain-specific rules.

    Application-controlled resource: clients can read this without LLM tool calls.
    """
    import asyncio
    import json as _json
    from trawler import keyword_rules

    def _query():
        conn = db.connect()
        try:
            if scope == "all":
                rules = keyword_rules.list_rules(conn)
            else:
                rules = keyword_rules.list_rules(conn, scope=scope)
            return rules
        finally:
            conn.close()

    try:
        rules = await asyncio.to_thread(_query)
    except Exception:
        return format_error("internal-error", "Failed to query keyword rules")

    payload = {
        "total": len(rules),
        "rules": [r.to_dict() for r in rules],
    }
    return _json.dumps(payload, ensure_ascii=False, indent=2)


# ── 关键词规则 Tool (CRUD + 测试) ─────────────────────────────────

@mcp.tool(
    description="List all keyword filter rules. Optional scope filter: 'global' or 'domain:example.com'. "
    "Returns JSON with rule details (include/exclude/regex/enabled/scope)."
)
async def list_keyword_rules(
    scope: Annotated[str, Field(description="Optional scope filter: 'global' or 'domain:example.com'. Empty = all.", default="")] = "",
) -> str:
    import asyncio
    import json as _json
    from trawler import keyword_rules

    def _query():
        conn = db.connect()
        try:
            return keyword_rules.list_rules(conn, scope=scope) if scope else keyword_rules.list_rules(conn)
        finally:
            conn.close()

    rules = await asyncio.to_thread(_query)
    payload = {"ok": True, "count": len(rules), "rules": [r.to_dict() for r in rules]}
    return format_ok(_json.dumps(payload, ensure_ascii=False, indent=2))


@mcp.tool(
    description="Add a keyword filter rule. The rule persists in DB and applies to future crawl_url calls. "
    "include: content must contain at least one (OR). exclude: reject if any matches. "
    "regex: pattern match (counts as include). scope: 'global' or 'domain:example.com'."
)
async def add_keyword_rule(
    name: Annotated[str, Field(description="Unique rule name")],
    include: Annotated[list[str] | None, Field(description="Keywords that must appear (OR logic). Empty = no include requirement.", default=None)] = None,
    exclude: Annotated[list[str] | None, Field(description="Keywords that cause rejection if any matches.", default=None)] = None,
    regex: Annotated[list[str] | None, Field(description="Regex patterns to match (counts as include).", default=None)] = None,
    case_sensitive: Annotated[bool, Field(description="Case-sensitive matching", default=False)] = False,
    match_position: Annotated[str, Field(description="'any' (default), 'title' (first line only), or 'body' (after first line)", default="any")] = "any",
    scope: Annotated[str, Field(description="'global' for all domains, or 'domain:example.com' for specific domain", default="global")] = "global",
    notes: Annotated[str, Field(description="Optional notes for this rule", default="")] = "",
) -> str:
    import asyncio
    import json as _json
    from trawler import keyword_rules

    rule = keyword_rules.KeywordRule(
        name=name,
        include=include or [],
        exclude=exclude or [],
        regex=regex or [],
        case_sensitive=case_sensitive,
        match_position=match_position,
        scope=scope,
        notes=notes,
    )

    def _add():
        conn = db.connect()
        try:
            keyword_rules.add_rule(conn, rule)
        finally:
            conn.close()

    try:
        await asyncio.to_thread(_add)
    except ValueError as e:
        return format_error("invalid-mode", str(e))
    return format_ok(_json.dumps({"ok": True, "rule": rule.to_dict()}, ensure_ascii=False, indent=2))


@mcp.tool(
    description="Update a keyword filter rule (partial update). Only provided fields are changed."
)
async def update_keyword_rule(
    name: Annotated[str, Field(description="Rule name to update")],
    include: Annotated[list[str] | None, Field(description="New include keywords", default=None)] = None,
    exclude: Annotated[list[str] | None, Field(description="New exclude keywords", default=None)] = None,
    regex: Annotated[list[str] | None, Field(description="New regex patterns", default=None)] = None,
    case_sensitive: Annotated[bool | None, Field(description="Case-sensitive flag", default=None)] = None,
    match_position: Annotated[str | None, Field(description="'any', 'title', or 'body'", default=None)] = None,
    enabled: Annotated[bool | None, Field(description="Enable/disable rule", default=None)] = None,
    scope: Annotated[str | None, Field(description="New scope", default=None)] = None,
    notes: Annotated[str | None, Field(description="New notes", default=None)] = None,
) -> str:
    import asyncio
    import json as _json
    from trawler import keyword_rules

    fields: dict = {}
    for k, v in {
        "include": include, "exclude": exclude, "regex": regex,
        "case_sensitive": case_sensitive, "match_position": match_position,
        "enabled": enabled, "scope": scope, "notes": notes,
    }.items():
        if v is not None:
            fields[k] = v

    def _update():
        conn = db.connect()
        try:
            return keyword_rules.update_rule(conn, name, **fields)
        finally:
            conn.close()

    rule = await asyncio.to_thread(_update)
    if not rule:
        return format_error("raw-not-found", f"keyword rule not found: {name}")
    return format_ok(_json.dumps({"ok": True, "rule": rule.to_dict()}, ensure_ascii=False, indent=2))


@mcp.tool(
    description="Delete a keyword filter rule by name."
)
async def delete_keyword_rule(
    name: Annotated[str, Field(description="Rule name to delete")],
) -> str:
    import asyncio
    from trawler import keyword_rules

    def _delete():
        conn = db.connect()
        try:
            return keyword_rules.delete_rule(conn, name)
        finally:
            conn.close()

    deleted = await asyncio.to_thread(_delete)
    if not deleted:
        return format_error("raw-not-found", f"keyword rule not found: {name}")
    return format_ok(f"Keyword rule {name!r} deleted.")


@mcp.tool(
    description="Test keyword rules against sample text (dry run, no DB write). "
    "Returns pass/fail status and which keywords matched."
)
async def test_keyword_rules(
    text: Annotated[str, Field(description="Text to test against")],
    include: Annotated[list[str] | None, Field(description="Include keywords (OR logic)", default=None)] = None,
    exclude: Annotated[list[str] | None, Field(description="Exclude keywords (reject if any match)", default=None)] = None,
    regex: Annotated[list[str] | None, Field(description="Regex patterns", default=None)] = None,
    case_sensitive: Annotated[bool, Field(description="Case-sensitive matching", default=False)] = False,
    match_position: Annotated[str, Field(description="'any', 'title', or 'body'", default="any")] = "any",
) -> str:
    import asyncio
    import json as _json
    from trawler import keyword_rules

    rule = keyword_rules.KeywordRule(
        name="__test__",
        include=include or [],
        exclude=exclude or [],
        regex=regex or [],
        case_sensitive=case_sensitive,
        match_position=match_position,
    )
    matcher = keyword_rules.KeywordMatcher()
    passed, reason = await asyncio.to_thread(matcher.match, text, [rule])

    # 命中详情
    flags = 0 if case_sensitive else __import__("re").IGNORECASE
    inc_hits = [k for k in (include or []) if __import__("re").search(__import__("re").escape(k), text, flags)]
    exc_hits = [k for k in (exclude or []) if __import__("re").search(__import__("re").escape(k), text, flags)]

    payload = {
        "ok": True,
        "passed": passed,
        "reason": reason,
        "include_hits": inc_hits,
        "exclude_hits": exc_hits,
    }
    return format_ok(_json.dumps(payload, ensure_ascii=False, indent=2))


# ── 启动钩子 ──────────────────────────────────────────────────────

def on_startup() -> None:
    """启动时: init_db + 清理 + 信号处理 + OTel + 认证检查。"""
    import os
    if os.getenv("JSON_LOGS"):
        try:
            from pythonjsonlogger.json import JsonFormatter
        except ImportError:
            # Fallback for older pythonjsonlogger versions
            from pythonjsonlogger.jsonlogger import JsonFormatter

        import sys

        from trawler.tracing import (
            agent_id_var, request_id_var, span_id_var, tool_name_var, trace_id_var,
        )

        class TracingFilter(logging.Filter):
            """注入 trace_id/span_id/tool_name/request_id/agent_id 到每条日志。"""
            def filter(self, record):
                record.trace_id = trace_id_var.get() or ""
                record.span_id = span_id_var.get() or ""
                record.tool_name = tool_name_var.get() or ""
                record.request_id = request_id_var.get() or ""
                record.agent_id = agent_id_var.get() or ""
                return True

        # 移除默认的 basicConfig 如果有的话
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addFilter(TracingFilter())

        # 清除已有的 handlers 防重复
        for h in root_logger.handlers[:]:
            root_logger.removeHandler(h)

        logHandler = logging.StreamHandler(sys.stderr)
        # 业务富字段: tool_name / request_id / agent_id 供 Loki/Prometheus 聚合
        formatter = JsonFormatter(
            '%(asctime)s %(levelname)s %(name)s %(message)s '
            '%(trace_id)s %(span_id)s %(tool_name)s %(request_id)s %(agent_id)s',
            timestamp=True
        )
        logHandler.setFormatter(formatter)
        root_logger.addHandler(logHandler)
        log.info("JSON structured logging enabled with tracing + telemetry fields")
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )
    db.init_db()
    conn = db.connect()
    try:
        result = lifecycle.startup_cleanup(conn)
        log.info("startup cleanup done: %s", result)
    finally:
        conn.close()
    signals.install_handlers()

    # OpenTelemetry 初始化 (可选依赖, 无依赖时降级为 no-op)
    try:
        from trawler.otel import init_otel
        init_otel()
    except Exception as e:
        log.warning("OTel init failed (non-fatal): %s", e)

    # 非 stdio transport 认证检查
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport != "stdio":
        try:
            from trawler.auth import is_auth_enabled, is_mtls_enabled
            if is_auth_enabled() or is_mtls_enabled():
                log.info("Auth middleware enabled (OIDC=%s, mTLS=%s) for transport=%s",
                         is_auth_enabled(), is_mtls_enabled(), transport)
            else:
                log.warning("Non-stdio transport (%s) without auth — exposed to network (set TRAWLER_AUTH_ENABLED=true)", transport)
        except Exception:
            pass

    # 打印内容安全防护状态，方便开发者与 Agent 审计
    pii_status = "【已开启】(所有手机、身份证、邮箱等敏感数据均已应用掩码)" if config.ENABLE_PII_MASKING else "【已关闭】(默认放行网页原始手机、身份证、邮箱等取证/分析数据)"
    word_status = "【已开启】" if config.ENABLE_WORD_FILTER else "【已关闭】"
    from trawler.parser import safety
    words_cache, _ = safety.load_sensitive_words()
    log.info("=" * 60)
    log.info("Trawler 内容安全防护与合规模块初始化：")
    log.info("  - 个人敏感信息脱敏 (PII Masking): %s", pii_status)
    log.info("  - 违规敏感词库过滤 (Word Filter): %s (已加载 %d 个词汇规则，修改 data/sensitive_words.txt 即时热更新)", word_status, len(words_cache))
    log.info("=" * 60)
    log.info("Trawler MCP server ready (patchright=%s, data=%s)",
             __import__("trawler.fetcher.patchright_rung", fromlist=["PATCHRIGHT_AVAILABLE"]).PATCHRIGHT_AVAILABLE,
             config.DATA_DIR)


def main() -> None:
    """入口: 启动 MCP server。支持 stdio / sse / streamable-http 三种 transport。

    非 stdio transport 时, 如启用认证 (TRAWLER_AUTH_ENABLED / TRAWLER_MTLS_ENABLED),
    用 AuthMiddleware 包裹 ASGI app 校验 Bearer token / mTLS 证书。
    """
    on_startup()
    import os
    transport = os.getenv("MCP_TRANSPORT", "stdio")

    if transport in ("sse", "streamable-http"):
        mcp.settings.host = os.getenv("FASTMCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.getenv("FASTMCP_PORT", "8000"))
        if os.getenv("FASTMCP_TRANSPORT_SECURITY__ENABLE_DNS_REBINDING_PROTECTION", "true").lower() == "false":
            mcp.settings.transport_security = None

    # 认证中间件 (非 stdio transport)
    if transport in ("sse", "streamable-http"):
        try:
            from trawler.auth import AuthMiddleware, is_auth_enabled, is_mtls_enabled
            if is_auth_enabled() or is_mtls_enabled():
                # 拿到 FastMCP 的 ASGI app, 包一层 auth middleware
                try:
                    asgi_app = mcp.streamable_http_app() if transport == "streamable-http" else mcp.sse_app()
                except AttributeError:
                    # FastMCP 版本不支持 *_app() → 降级, 用 mcp.run (无 auth)
                    log.warning("FastMCP version does not expose ASGI app — auth middleware skipped")
                    asgi_app = None
                if asgi_app is not None:
                    wrapped = AuthMiddleware(asgi_app)
                    import uvicorn
                    log.info("Starting %s server with auth middleware on %s:%s",
                             transport, mcp.settings.host, mcp.settings.port)
                    # P1: uvicorn 优雅关闭时 await 关闭 curl_cffi session pool
                    # (SIGTERM 路径的 _force_close_all 是同步 clear, 无法 await close)
                    server = uvicorn.Server(uvicorn.Config(
                        wrapped, host=mcp.settings.host, port=mcp.settings.port,
                    ))
                    _orig_shutdown = server.shutdown
                    async def _shutdown_with_curlcffi():
                        await _orig_shutdown()
                        try:
                            from trawler.fetcher.curlcffi_rung import shutdown_sessions
                            await shutdown_sessions()
                        except Exception as e:
                            log.warning("curl_cffi session shutdown failed: %s", e)
                    server.shutdown = _shutdown_with_curlcffi
                    server.run()
                    return
        except Exception as e:
            log.warning("Auth middleware setup failed (non-fatal, falling back to mcp.run): %s", e)
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
