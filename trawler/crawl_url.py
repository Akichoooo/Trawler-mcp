"""crawl_url — 主流水线, 组装所有模块。

永远返回字符串 (根除多态, 防 LLM 幻觉):
  成功   → "__TRAWLER_OK__:\n\n<markdown>"
  失败   → "__TRAWLER_ERROR__:{json}"

墙钟 35s asyncio.wait_for。全局异常捕获 (不泄 stacktrace)。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time

from trawler import (
    account_profiles,
    account_vault,
    artifacts,
    audit,
    browser_session,
    config,
    prompt_audit,
    proxy_pool,
    rules,
    seen,
    site_rules,
    ssrf,
    urlnorm,
)
from trawler.crawl_policy import CrawlPolicy
from trawler.errors import (
    RateLimitError,
)
from trawler.errors import (
    format_error as _format_error,
)
from trawler.errors import (
    format_ok as _format_ok,
)
from trawler.errors import (
    is_error as _is_error,
)
from trawler.errors import (
    is_ok as _is_ok,
)
from trawler.errors import (
    unwrap_ok as _unwrap_ok,
)
from trawler.fetcher import challenge_detect, curlcffi_rung, hitl_rung, jina_rung, patchright_rung
from trawler.fetcher import detect as detect_mod
from trawler.parser import extract as parser_extract
from trawler.parser import title as parser_title
from trawler.raw_store import save_blocked, save_raw
from trawler.urlnorm import domain_of as _domain

log = logging.getLogger("trawler.crawl_url")

# 全局并发上限 (延迟初始化, 避免绑错事件循环)
_sem: asyncio.Semaphore | None = None
_domain_rl_lock: asyncio.Lock | None = None

def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(config.SEMAPHORE)
    return _sem

def _get_domain_rl_lock() -> asyncio.Lock:
    global _domain_rl_lock
    if _domain_rl_lock is None:
        _domain_rl_lock = asyncio.Lock()
    return _domain_rl_lock

_domain_rl_counter = 0
_domain_last_request: dict[str, float] = {}
_domain_backoff_until: dict[str, float] = {}
# AIMD 自适应窗口: 每域一个间隔乘数因子 (1.0 = 基准, 越大越慢)。
# 连续成功 → 指数衰减回 1.0 (恢复速度); 遇 429/403 → 立即 ×2 (乘性减)。
# 100 并发下防雪崩: 单点失败不拖垮全局, 但对"坏邻居"域自动降速。
_domain_aimd_factor: dict[str, float] = {}
_AIMD_MIN = 1.0       # 最低速 (基准间隔)
_AIMD_MAX = 32.0      # 最高减速 (32x 基准, 防过度惩罚)
_AIMD_DECREASE = 2.0  # 失败时乘 2 (更慢)
_AIMD_RECOVER = 0.9   # 每次成功 ×0.9 (渐近恢复到 1.0)


def _aimd_on_success(domain: str) -> None:
    """连续成功 → 渐近恢复间隔因子到 1.0 (乘性恢复)。"""
    cur = _domain_aimd_factor.get(domain, 1.0)
    if cur > _AIMD_MIN:
        _domain_aimd_factor[domain] = max(_AIMD_MIN, cur * _AIMD_RECOVER)


def _aimd_on_failure(domain: str) -> None:
    """失败 (429/403) → 立即乘性减慢 (×2, 上限 32x)。"""
    cur = _domain_aimd_factor.get(domain, 1.0)
    _domain_aimd_factor[domain] = min(_AIMD_MAX, cur * _AIMD_DECREASE)

async def _domain_rate_limit_wait(domain: str, retry_after: float = 0.0, attempt: int = 0):
    global _domain_rl_counter
    async with _get_domain_rl_lock():
        now = time.monotonic()
        _domain_rl_counter += 1
        if _domain_rl_counter >= 1000:
            _domain_rl_counter = 0
            to_delete = [k for k, v in _domain_last_request.items() if now - v > 3600.0]
            for k in to_delete:
                del _domain_last_request[k]
            to_delete_backoff = [k for k, v in _domain_backoff_until.items() if now > v]
            for k in to_delete_backoff:
                del _domain_backoff_until[k]
            # 清理长期未用的 AIMD 因子 (防内存增长, 1h 未活跃的域回收)
            to_delete_aimd = [k for k, v in _domain_aimd_factor.items() if k not in _domain_last_request and k not in _domain_backoff_until]
            for k in to_delete_aimd:
                del _domain_aimd_factor[k]

        if retry_after > 0:
            _domain_backoff_until[domain] = now + retry_after
            # AIMD 乘性减: 被 429 限流的域立即 ×2 (下次该域更慢)
            _aimd_on_failure(domain)
            return

        wait_time = 0.0
        backoff_until = _domain_backoff_until.get(domain, 0.0)
        if backoff_until > now:
            wait_time = backoff_until - now
            # Extend last request so we don't double wait
            _domain_last_request[domain] = now + wait_time
        else:
            last = _domain_last_request.get(domain, 0.0)
            base_interval = getattr(config, "SAME_DOMAIN_INTERVAL", 1.0)
            # AIMD: 用该域的间隔乘数因子 (失败多的域自动更慢)
            aimd_factor = _domain_aimd_factor.get(domain, 1.0)
            # Full Jitter: random.uniform(0, base * aimd_factor * 2^attempt)
            interval = random.uniform(0, base_interval * aimd_factor * (2 ** attempt))
            elapsed = now - last
            if elapsed < interval:
                wait_time = interval - elapsed
                _domain_last_request[domain] = now + wait_time
            else:
                _domain_last_request[domain] = now

    if wait_time > 0:
        await asyncio.sleep(wait_time)


def _content_hash(md: str) -> str:
    return hashlib.sha1(md.encode("utf-8")).hexdigest()[:16]


def _rule_selectors(site_rule, db_rule) -> list[str]:
    selectors: list[str] = []
    if site_rule and getattr(site_rule, "selectors", None):
        selectors.extend(str(item) for item in site_rule.selectors)
    db_selectors = getattr(db_rule, "selectors", "") if db_rule else ""
    if db_selectors:
        for item in str(db_selectors).replace("\n", ",").split(","):
            item = item.strip()
            if item:
                selectors.append(item)
    return selectors


def _strip_frontmatter(raw_content: str) -> str:
    """从 raw 文件内容剥离 YAML frontmatter, 只返回正文 md。

    raw 格式: ---\\n<yaml>\\n---\\n\\n# Title\\n\\n<body>
    """
    if not raw_content.startswith("---"):
        return raw_content
    # 找第二个 --- (frontmatter 结束)
    second = raw_content.find("\n---", 4)
    if second == -1:
        return raw_content
    body = raw_content[second + 4:].lstrip("\n")
    return body


def _final_url_in_scope(
    final_url: str,
    *,
    allowed_domain: str,
    include_subdomains: bool,
    include_paths: list[str],
    exclude_paths: list[str],
) -> bool:
    start_url = f"https://{allowed_domain}/" if allowed_domain else final_url
    policy = CrawlPolicy.from_options(
        start_url,
        same_domain_only=bool(allowed_domain),
        include_subdomains=include_subdomains,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
    )
    return policy.final_url_allowed(final_url)


async def crawl_url(
    url: str,
    *,
    use_proxy: bool = False,
    force_refresh: bool = False,
    cache_mode: str = "enabled",
    bypass_robots: bool = False,
    user_authorized_access: bool = False,
    account_id: str = "",
    human_assist: str = "auto",
    selector: str = "",
    capture_artifact: bool = False,
    bypass_l3: bool = False,
    timeout: int | None = None,
    mode: str = "full",
    section_id: str = "",
    chunk_index: int = 1,
    allowed_domain: str = "",
    include_subdomains: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    ignore_query_parameters: bool = False,
    conn=None,
) -> str:
    """抓单页。永远返回字符串。≤35s。"""
    import uuid

    from trawler import db as db_mod
    from trawler.tracing import span_id_var, trace_id_var

    # 注入 Trace ID + Span ID 以进行全链路追踪
    trace_id = str(uuid.uuid4())
    span_id = uuid.uuid4().hex[:16]
    token = trace_id_var.set(trace_id)
    span_token = span_id_var.set(span_id)

    own_conn = conn is None
    if own_conn:
        conn = db_mod.connect()

    try:
        timeout_val = timeout if timeout is not None else config.CRAWL_TIMEOUT
        if mode not in {"full", "toc", "section", "chunk"}:
            return _format_error("invalid-mode", f"Unsupported mode: {mode}")
        if force_refresh and cache_mode == "enabled":
            cache_mode = "write_only"
        if cache_mode not in {"enabled", "read_only", "write_only", "bypass", "disabled"}:
            return _format_error("invalid-mode", f"Unsupported cache_mode: {cache_mode}")
        if human_assist not in {"auto", "required", "off"}:
            return _format_error("invalid-mode", f"Unsupported human_assist: {human_assist}")

        domain = _domain(url)
        res = await asyncio.wait_for(
            _crawl_url_inner(
                url,
                domain=domain,
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
                allowed_domain=allowed_domain,
                include_subdomains=include_subdomains,
                include_paths=include_paths or [],
                exclude_paths=exclude_paths or [],
                ignore_query_parameters=ignore_query_parameters,
                conn=conn,
            ),
            timeout=timeout_val,
        )
        if _is_ok(res):
            md_body = _unwrap_ok(res)
            from trawler.parser import chunker
            if mode == "toc":
                md_body = chunker.generate_toc(md_body)
            elif mode == "section":
                md_body = chunker.slice_by_section(md_body, section_id or "Section 1")
                if _is_error(md_body):
                    return md_body
            elif mode == "chunk":
                md_body = chunker.slice_by_tokens(md_body, chunk_index=chunk_index)
            return _format_ok(md_body)
        return res
    except TimeoutError:
        await _db_write(audit.write_audit, conn, tool="crawl_url", url=url, status="timeout")
        return _format_error("timeout", f"Crawl for {url} exceeded {timeout_val}s")
    except Exception as e:
        # 全局兜底: 任何未捕获异常 → 字符串, 绝不泄 stacktrace
        log.exception("crawl_url unexpected error for %s", url)
        await _db_write(audit.write_audit, conn, tool="crawl_url", url=url, status="error")
        return _format_error("internal-error", f"Unexpected error: {type(e).__name__}: {e}")
    finally:
        if own_conn:
            conn.close()
        # contextvar token 必须复位, 否则泄漏到调用方 Task 上下文外, 污染后续日志 trace 关联
        trace_id_var.reset(token)
        span_id_var.reset(span_token)


async def _crawl_url_inner(
    url: str,
    *,
    domain: str,
    use_proxy: bool,
    force_refresh: bool,
    cache_mode: str,
    bypass_robots: bool,
    user_authorized_access: bool,
    account_id: str,
    human_assist: str,
    selector: str,
    capture_artifact: bool,
    bypass_l3: bool,
    allowed_domain: str = "",
    include_subdomains: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    ignore_query_parameters: bool = False,
    conn=None,
) -> str:
    # 限制同域名并发: 用记录上次请求时间+sleep的方式, 避免长请求阻塞排队
    for attempt in range(3):
        await _domain_rate_limit_wait(domain, attempt=attempt)
        async with _get_sem():
            # 二次检查域级 backoff: 防排队协程绕过 rate_limit 检查点直打刚被 429 的域。
            # 拿到 sem 后若发现该域在退避中, 释放 sem 并 continue (回 for 顶部 _domain_rate_limit_wait 睡 backoff)。
            # 不在 sem 内 sleep: retry_after 可达 300s, 持全局 sem 槽 300s 会饿死其他域 (SEMAPHORE 默认仅 3)。
            # 防雪崩由 sem 外的 _domain_rate_limit_wait 串行 sleep 保证, sem 内只做"拒绝并重排"。
            async with _get_domain_rl_lock():
                backoff_until = _domain_backoff_until.get(domain, 0.0)
                now = time.monotonic()
            if backoff_until > now:
                log.info("domain %s in backoff, releasing sem and re-queueing", domain)
                continue  # 释放 sem, for 顶部 _domain_rate_limit_wait 会睡 backoff
            try:
                return await _do_crawl(
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
                    allowed_domain=allowed_domain,
                    include_subdomains=include_subdomains,
                    include_paths=include_paths or [],
                    exclude_paths=exclude_paths or [],
                    ignore_query_parameters=ignore_query_parameters,
                    conn=conn,
                )
            except Exception as e:
                if isinstance(e, RateLimitError):
                    log.warning("Rate limit hit on %s, backing off for %ss", domain, e.retry_after)
                    await _domain_rate_limit_wait(domain, retry_after=e.retry_after)
                    if attempt == 2:
                        return _format_error("rate-limit", f"Domain {domain} rate limited, max retries exceeded")
                    continue
                raise


async def _db_write(fn, *args, **kwargs):
    """异步排队批量 DB 写。
    
    使用 trawler.db_writer 单一后台线程排队执行。剥离 conn 参数避免跨线程传连接。
    """
    import sqlite3

    from trawler import db_writer
    
    new_args = list(args)
    if new_args and isinstance(new_args[0], sqlite3.Connection):
        new_args.pop(0)
    elif "conn" in kwargs:
        del kwargs["conn"]
        
    return await db_writer.submit(fn, *new_args, **kwargs)


async def _do_crawl(
    url: str,
    *,
    use_proxy: bool,
    force_refresh: bool,
    cache_mode: str,
    bypass_robots: bool,
    user_authorized_access: bool,
    account_id: str,
    human_assist: str,
    selector: str,
    capture_artifact: bool,
    bypass_l3: bool,
    allowed_domain: str = "",
    include_subdomains: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    ignore_query_parameters: bool = False,
    conn=None,
) -> str:
    # ② URL 规范化
    canon = urlnorm.canonical_url(url)
    if not canon:
        return _format_error("invalid-url", "Invalid URL provided")
    url = canon
    domain = _domain(url)
    raw_id = seen.url_id(url)

    # ③ SSRF 守卫 (最前) — async 版, DNS 解析丢线程池不阻塞
    is_blocked, safe_ip = await ssrf.resolve_and_check_async(url)
    if is_blocked:
        await _db_write(audit.write_audit, conn, tool="crawl_url", url=url, status="blocked-ssrf")
        return ssrf.block_reason(url)

    # ③b robots.txt 合规 (RFC 9309): 非 force_refresh 时尊重 Disallow。
    # force_refresh=True 表示用户明示要刷新, 跳过此检查。
    # Cache refresh does not bypass robots; use bypass_robots explicitly.
    if user_authorized_access:
        await _db_write(
            audit.write_audit,
            conn,
            tool="crawl_url",
            url=url,
            status="user_authorized_access",
        )
    if config.RESPECT_ROBOTS and not (bypass_robots or user_authorized_access):
        from trawler import robots as robots_mod
        try:
            allowed = await robots_mod.is_allowed(url, use_proxy=use_proxy)
        except Exception as e:
            if config.ROBOTS_FAIL_CLOSED:
                await _db_write(
                    audit.write_audit,
                    conn,
                    tool="crawl_url",
                    url=url,
                    status="blocked-robots",
                )
                return _format_error("blocked-robots", f"robots.txt check failed: {e}")
            allowed = True
        if not allowed:
            await _db_write(audit.write_audit, conn, tool="crawl_url", url=url, status="blocked-robots")
            return _format_error(
                "blocked-robots",
                f"URL disallowed by robots.txt for {domain}; "
                "use retrieve_page(access_mode='user_authorized') for single-page, "
                "user-directed browser access; legacy crawl_url can set user_authorized_access=true",
            )

    # ④ 查去重
    if cache_mode in {"enabled", "read_only"}:
        cached = seen.lookup(conn, url)
        if cached:
            log.info("cache hit: %s → %s", url, cached)
            await _db_write(audit.write_audit, conn, tool="crawl_url",
                                     url=url, status="cache_hit")
            # 返回已存的 raw 内容 (剥离 frontmatter, 只返回正文 md)
            try:
                from trawler.raw_store import get_raw, raw_path
                p = raw_path(cached)
                if await asyncio.to_thread(p.exists):
                    raw_content = await asyncio.to_thread(get_raw, str(p))
                    md = _strip_frontmatter(raw_content).rstrip()
                    return _format_ok(md)
            except Exception:
                pass  # 缓存文件丢了, 重新爬

    # ⑤ 查手册 (用前自测) + 查 site_rules
    if cache_mode == "read_only":
        return _format_error("cache-miss", f"No cached raw content for {url}")

    if rules.is_unreachable(conn, domain):
        await _db_write(audit.write_audit, conn, tool="crawl_url",
                                 url=url, status="unreachable")
        return _format_error("domain-unreachable", f"Domain {domain} marked unreachable (retry later)")
    
    wait_strategy = "domcontentloaded"
    wait_for_selector = ""
    needs_account = False
    site_rule = await asyncio.to_thread(site_rules.load, domain)
    db_rule = rules.get(conn, domain)

    if site_rule:
        cached_gear = site_rule.gear_hint or rules.should_use_cached(conn, domain)
        use_proxy = site_rule.needs_proxy or use_proxy
        wait_strategy = site_rule.wait_strategy
        wait_for_selector = site_rule.wait_for_selector
        needs_account = site_rule.needs_account
    else:
        cached_gear = rules.should_use_cached(conn, domain)
        if db_rule:
            needs_account = db_rule.needs_account

    # ⑥ 查账号库
    session_account_id = ""
    session_account_profile = None
    if account_id or user_authorized_access or needs_account:
        try:
            session_account_id = account_profiles.resolve_account_id(domain, account_id)
        except ValueError as e:
            return _format_error("invalid-mode", str(e))
        session_account_profile = account_profiles.get_profile(domain, session_account_id)
        if session_account_profile is None:
            session_account_profile = account_profiles.register_profile(
                domain,
                account_id=session_account_id,
                make_default=session_account_id == "default",
            )
    elif account_vault.has_account(domain, account_id="default"):
        default_profile = account_profiles.get_profile(domain, "default")
        if default_profile is None or account_profiles.is_usable_for_automation(default_profile):
            session_account_id = "default"
            session_account_profile = default_profile

    account_usable = (
        session_account_profile is None
        or account_profiles.is_usable_for_automation(session_account_profile)
    )
    if session_account_id and not account_usable:
        needs_account = True

    storage_state = (
        account_vault.get_storage_state(domain, account_id=session_account_id)
        if session_account_id and account_usable
        else None
    )
    proxy_url = proxy_pool.select_proxy(
        use_proxy,
        domain=domain,
        account_id=session_account_id,
    )
    session = browser_session.select_session(
        conn,
        domain,
        account_id=session_account_id,
        proxy_url=proxy_url,
        storage_state_bound=bool(storage_state),
    )
    # #3 needs_account 保留由 rules.record_success(needs_account=None) 处理:
    # 不传值 = 保留已有 (HITL 标记的 needs_account=1 不被成功回写覆盖)

    # ⑦ Fetcher 阶梯 (传 use_proxy, cached_gear 用于跳档)
    fetch_out = await _fetch_ladder(
        url, domain=domain, storage_state=storage_state, bypass_l3=bypass_l3,
        cached_gear=cached_gear, use_proxy=use_proxy, wait_strategy=wait_strategy,
        wait_for_selector=wait_for_selector,
        needs_account=needs_account, conn=conn, safe_ip=safe_ip, session_id=session.session_id,
        proxy_url=proxy_url,
        account_id=session_account_id,
        human_assist=human_assist,
        capture_artifact=capture_artifact,
        browser_first=user_authorized_access or capture_artifact,
        allow_jina=not user_authorized_access,
    )
    if len(fetch_out) == 4:
        html, http_status, final_url, gear_used = fetch_out
        artifact_id = ""
    else:
        html, http_status, final_url, gear_used, artifact_id = fetch_out

    # ⑦.5 验证 session 有效性 (已移入 _fetch_ladder 内部处理)
    # (Removed to fix P1: verify_session_valid fallback)

    # #2 SSRF 重定向命中: 直接返回阻断串, 不进 parser/不试其他档
    if gear_used == "__SSRF_BLOCKED__":
        await _db_write(audit.write_audit, conn, tool="crawl_url",
                                 url=url, status="blocked-ssrf-redirect")
        return _format_error(
            "blocked-ssrf-redirect",
            "Blocked redirect to internal IP (SSRF guard). Set TRAWLER_ALLOW_LOCAL=1 to allow.",
        )

    if not _final_url_in_scope(
        final_url or url,
        allowed_domain=allowed_domain,
        include_subdomains=include_subdomains,
        include_paths=include_paths or [],
        exclude_paths=exclude_paths or [],
    ):
        out_of_scope_url = final_url or url
        await _db_write(audit.write_audit, conn, tool="crawl_url", url=url, status="blocked-scope")
        await asyncio.to_thread(
            save_blocked,
            raw_id,
            url=url,
            reason=f"Final URL outside crawl scope: {out_of_scope_url}",
            html_excerpt="",
            gear_used=gear_used,
            metadata={"final_url": out_of_scope_url},
        )
        return _format_error(
            "blocked-scope",
            f"Final URL outside crawl scope: {out_of_scope_url}",
        )

    if html is None or (isinstance(html, str) and html.startswith("__SPECIFIC_ERROR__:")):
        if isinstance(html, str) and html.startswith("__SPECIFIC_ERROR__:"):
            _, err_type, err_msg = html.split(":", 2)
        else:
            err_type = "all-fetchers-failed"
            err_msg = f"All fetchers failed to retrieve {url}"
            
        # 全档失败
        await _db_write(rules.record_failure, conn, domain,
                                 error=err_type, mark_unreachable=True)
        browser_session.mark_bad(
            conn,
            session.session_id,
            err_type,
            retire=err_type in ("session-expired", "blocked-bot"),
        )
        await _db_write(audit.write_audit, conn, tool="crawl_url",
                                 url=url, status="failed", rung_used="all")
        await asyncio.to_thread(save_blocked, raw_id, url=url,
                                 reason=err_msg, html_excerpt="", gear_used="all",
                                 metadata={"artifact_id": artifact_id} if artifact_id else None)
        # AIMD 乘性减: 仅限流/反爬类失败减速 (session-expired 是 cookie 过期, 非域级问题, 不减速)
        if err_type in ("rate-limit", "blocked-bot"):
            _aimd_on_failure(domain)
        return _format_error(err_type, err_msg, artifact_id=artifact_id)

    # ⑦b 特殊: HITL 需要但无 GUI
    if html == "__HITL_HEADLESS__" or (
        isinstance(html, str) and html.startswith("__HITL_UNAVAILABLE__:")
    ):
        hitl_reason = (
            html.split(":", 1)[1]
            if isinstance(html, str) and html.startswith("__HITL_UNAVAILABLE__:")
            else "HITL required but no display available (headless environment)"
        )
        await _db_write(rules.mark_needs_account, conn, domain)
        await _db_write(audit.write_audit, conn, tool="crawl_url",
                                 url=url, status="human_window_unavailable", rung_used="hitl")
        # #10 存 .BLOCKED.md 诊断 (留给人/agent 排查)
        await asyncio.to_thread(save_blocked, raw_id, url=url,
                                 reason=hitl_reason,
                                 html_excerpt="", gear_used="hitl")
        return _format_error("human-window-unavailable", f"HITL required for {domain}: {hitl_reason}")

    # ⑧ Parser 阶梯 (提取正文) — #11 包 to_thread, 不阻塞事件循环
    # Jina 已返回 markdown, 跳过 parser; 其他 fetcher 返回 HTML 进 parser
    discovered_links: list[dict[str, object]] = []
    selector_report: dict[str, object] = {}
    if gear_used == "jina_reader":
        md = html  # Jina 的输出直接就是 md
        if not md or not md.strip():
            md = parser_extract.PARSERS_FAILED
    else:
        from trawler import link_map
        from trawler.parser import selectors as parser_selectors

        discovered_links = await asyncio.to_thread(
            link_map.extract_links,
            html[: config.HTML_TRUNCATE],
            final_url or url,
            max_links=int(getattr(config, "MAX_LINKS_PER_PAGE", 200)),
        )
        html_for_parse, selector_report = await asyncio.to_thread(
            parser_selectors.apply_selectors,
            html,
            [selector] if selector else _rule_selectors(site_rule, db_rule),
        )
        md = await asyncio.to_thread(
            parser_extract.extract,
            html_for_parse,
            final_url or url,
        )

    if not parser_extract.is_extracted(md):
        if not artifact_id:
            artifact_id = await asyncio.to_thread(
                artifacts.save_artifact,
                url=url,
                reason="empty-content",
                success=False,
                final_url=final_url or url,
                http_status=http_status,
                gear_used=gear_used,
                session_id=session.session_id,
                html=html[: config.HTML_TRUNCATE] if isinstance(html, str) else "",
                extra={"stage": "parser", "message": "parsers extracted no text"},
            )
        # parser 全失败
        await _db_write(rules.record_failure, conn, domain,
                                 error="parsers extracted no text")
        browser_session.mark_bad(conn, session.session_id, "empty-content")
        await _db_write(audit.write_audit, conn, tool="crawl_url",
                                 url=url, status="parser_failed", rung_used=gear_used)
        await asyncio.to_thread(save_blocked, raw_id, url=url,
                                 reason="parsers extracted no text",
                                 html_excerpt=html[:5000], gear_used=gear_used,
                                 metadata={"artifact_id": artifact_id} if artifact_id else None)
        return _format_error(
            "empty-content",
            "Parsers extracted no text. This usually means a block or unsupported SPA.",
            artifact_id=artifact_id,
        )

    # ⑨ title
    title = await asyncio.to_thread(parser_title.extract_title, html, final_url or url)

    # ⑩ 存 raw (原子写, 丢线程池防阻塞)
    prompt_audit_result = await asyncio.to_thread(prompt_audit.audit_content, md)
    from trawler.parser import quality as parser_quality

    quality_report = await asyncio.to_thread(parser_quality.markdown_quality, md)
    metadata = {
        "http_status": http_status,
        "content_hash": _content_hash(md),
        "content_is_untrusted": True,
        "parser_input_truncated": isinstance(html, str) and len(html) > config.HTML_TRUNCATE,
        "prompt_injection_risk": prompt_audit_result["risk"],
        "links": discovered_links,
        "link_count": len(discovered_links),
    }
    metadata.update(quality_report)
    if prompt_audit_result["signals"]:
        metadata["prompt_injection_signals"] = prompt_audit_result["signals"]
    if artifact_id:
        metadata["artifact_id"] = artifact_id
    if selector_report.get("selector_match_count"):
        metadata["selector_used"] = selector_report.get("selector_used")
        metadata["selector_match_count"] = selector_report.get("selector_match_count")
    elif site_rule and getattr(site_rule, "selectors", None):
        # site_rule 定义了 selectors 但一个都没匹配 -> 页面结构可能不符合预期 (加载失败/改版)
        # 留痕不阻断 (selector 可能只是没更新, 不该阻断正常抓取)
        metadata["selector_all_missed"] = True
        log.warning(
            "site_rule selectors all missed for %s, page structure may be unexpected",
            domain,
        )
    if selector_report.get("selector_errors"):
        metadata["selector_errors"] = selector_report.get("selector_errors")
    await asyncio.to_thread(save_raw, raw_id, url=url, final_url=final_url or url,
                             title=title, markdown=md, gear_used=gear_used, metadata=metadata)

    # ⑪ 回写手册 (成功) — needs_account 用 None 保留已有值, 不被覆盖
    await _db_write(rules.record_success, conn, domain, gear=gear_used,
                             needs_proxy=use_proxy)
    browser_session.mark_good(conn, session.session_id)
    if session_account_id and storage_state:
        try:
            account_profiles.touch_verified(domain, session_account_id)
        except Exception:
            pass
    # AIMD 乘性增: 成功 → 该域间隔因子渐近恢复到 1.0 (下次更快)
    _aimd_on_success(domain)

    # ⑫ 记 seen + audit
    if cache_mode in {"enabled", "write_only"}:
        await _db_write(seen.record, conn, url, raw_id, _content_hash(md))
    await _db_write(audit.write_audit, conn, tool="crawl_url",
                             url=url, status="ok", rung_used=gear_used)

    return _format_ok(md)


async def _fetch_ladder(
    url: str,
    *,
    domain: str,
    storage_state: str | None,
    bypass_l3: bool,
    cached_gear: str | None,
    use_proxy: bool,
    wait_strategy: str,
    wait_for_selector: str = "",
    needs_account: bool,
    conn,
    session_id: str,
    proxy_url: str = "",
    account_id: str = "",
    safe_ip: str | None = None,
    human_assist: str = "auto",
    capture_artifact: bool = False,
    browser_first: bool = False,
    allow_jina: bool = True,
) -> tuple[str | None, int, str, str, str]:
    """Fetcher 阶梯。返回 (html, status, final_url, gear_used) 或 (None,0,'','') 全失败。

    特殊返回: ('__HITL_HEADLESS__', 0, '', '') 表示需 HITL 但无 GUI。

    cached_gear: 手册记的成功档。若命中, 优先用它作起手 (用前自测)。
    """
    import os
    import tempfile

    # storage_state 写到临时文件给 patchright 注入 (finally 必删, 防泄漏)
    storage_state_path = None
    tmp_path_to_clean = None
    if storage_state:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        tmp.write(storage_state)
        tmp.close()
        storage_state_path = tmp.name
        tmp_path_to_clean = tmp.name

    try:
        # #2 cached_gear 跳档: 手册记的成功档优先起手
        # 若 cached_gear 指向特定档, 先试那档; 否则默认从 rung1 起步
        order = _gear_order(
            cached_gear,
            needs_account,
            storage_state_path,
            domain,
            human_assist=human_assist,
            browser_first=browser_first,
            allow_jina=allow_jina,
        )
        if not order:
            return (
                "__HITL_UNAVAILABLE__:human assist is disabled, but this page needs account or human verification",
                0,
                "",
                "hitl",
                "",
            )

        last_error_type = "all-fetchers-failed"
        last_error_msg = f"All fetchers failed to retrieve {url}"
        last_artifact_id = ""

        for gear in order:
            if gear == "curl_cffi":
                result = await _try_curlcffi(
                    url,
                    bypass_l3,
                    use_proxy,
                    safe_ip,
                    session_id,
                    proxy_url,
                    account_id,
                )
                if isinstance(result, str) and result.startswith("__BLOCKED_BOT__"):
                    if ":" in result:
                        last_artifact_id = result.split(":", 1)[1]
                    last_error_type = "blocked-bot"
                    last_error_msg = "Blocked by bot protection (curl_cffi rung, WAF challenge)"
                    continue
                if isinstance(result, str) and result.startswith("__SSRF_BLOCKED__:"):
                    reason = result[len("__SSRF_BLOCKED__:"):]
                    return reason, 0, "", "__SSRF_BLOCKED__", ""
                if result is not None and not isinstance(result, str):
                    return _normalize_ladder_tuple(result)
            elif gear == "patchright_headless":
                result = await _try_patchright(
                    url,
                    storage_state_path,
                    bypass_l3,
                    use_proxy,
                    domain,
                    wait_strategy,
                    safe_ip,
                    session_id,
                    proxy_url,
                    account_id,
                    capture_artifact,
                    wait_for_selector,
                )
                # #1 短路 HITL: 不走顺序 fallback, 直接跳 HITL 档
                if isinstance(result, str) and result.startswith("__ARTIFACT__:"):
                    last_artifact_id = result[len("__ARTIFACT__:"):]
                    continue
                if isinstance(result, str) and result.startswith("__SHORTCIRCUIT_HITL__"):
                    if ":" in result:
                        last_artifact_id = result.split(":", 1)[1]
                    log.info("shortcircuit: patchright → HITL (skip jina)")
                    hitl_result = await _try_hitl(
                        url,
                        domain=domain,
                        use_proxy=use_proxy,
                        conn=conn,
                        safe_ip=safe_ip,
                        session_id=session_id,
                        proxy_url=proxy_url,
                        account_id=account_id,
                        capture_artifact=capture_artifact,
                    )
                    if hitl_result is not None:
                        return _normalize_ladder_tuple(hitl_result)
                    continue
                # #2 SSRF 重定向命中: 阻断, 不再试其他档
                if isinstance(result, str) and result.startswith("__SSRF_BLOCKED__:"):
                    reason = result[len("__SSRF_BLOCKED__:"):]
                    # 用特殊标记让 _do_crawl 直接返回 block_reason, 不进 parser
                    return reason, 0, "", "__SSRF_BLOCKED__", last_artifact_id
                if isinstance(result, str) and result.startswith("__BLOCKED_BOT__"):
                    if ":" in result:
                        last_artifact_id = result.split(":", 1)[1]
                    last_error_type = "blocked-bot"
                    last_error_msg = "Blocked by bot protection (WAF challenge or captcha)"
                    continue
                if result is not None and not isinstance(result, str):
                    html_res, status_res, final_url_res, gear_res, result_artifact_id = _normalize_ladder_tuple(result)
                    if result_artifact_id:
                        last_artifact_id = result_artifact_id
                    if last_error_type == "blocked-bot":
                        from trawler import site_rules
                        site_rules.promote_domain(domain, gear_hint="patchright")
                    # #5 验证 session
                    if storage_state_path and not detect_mod.verify_session_valid(html_res, final_url_res or url):
                        log.warning("session invalid for %s, falling back to next gear", domain)
                        from trawler.account_vault import invalidate_storage_state
                        invalidate_storage_state(domain, account_id=account_id or None)
                        if account_id:
                            account_profiles.mark_profile_status(
                                domain,
                                account_id,
                                "expired",
                                notes="Stored browser state failed session validation.",
                            )
                        browser_session.retire_session(conn, session_id, "session-expired")
                        last_error_type = "session-expired"
                        last_error_msg = "Cookie/session expired, requires re-login"
                        # 不硬标 needs_account=True，给 jina 降级机会
                        continue
                    return html_res, status_res, final_url_res, gear_res, result_artifact_id
            elif gear == "jina_reader":
                if (
                    result := await _try_jina(
                        url,
                        bypass_l3,
                        use_proxy,
                        needs_account=needs_account,
                        proxy_url=proxy_url,
                    )
                ) is not None:
                    return _normalize_ladder_tuple(result)
            elif gear == "hitl":
                result = await _try_hitl(
                    url,
                    domain=domain,
                    use_proxy=use_proxy,
                    conn=conn,
                    safe_ip=safe_ip,
                    session_id=session_id,
                    proxy_url=proxy_url,
                    account_id=account_id,
                    capture_artifact=capture_artifact,
                )
                if isinstance(result, str) and result.startswith("__SSRF_BLOCKED__:"):
                    reason = result[len("__SSRF_BLOCKED__:"):]
                    return reason, 0, "", "__SSRF_BLOCKED__", last_artifact_id
                if result is not None:
                    if result[0] != "__HITL_HEADLESS__":
                        return _normalize_ladder_tuple(result)
                    # HITL_HEADLESS 不重试其他档, 直接透传
                    return _normalize_ladder_tuple(result)

        # 所有档都失败
        return f"__SPECIFIC_ERROR__:{last_error_type}:{last_error_msg}", 0, "", "", last_artifact_id
    finally:
        # #8 清理临时文件
        if tmp_path_to_clean and os.path.exists(tmp_path_to_clean):
            try:
                os.unlink(tmp_path_to_clean)
            except OSError:
                pass


def _gear_order(
    cached_gear: str | None,
    needs_account: bool,
    storage_state_path: str | None,
    domain: str,
    *,
    human_assist: str = "auto",
    browser_first: bool = False,
    allow_jina: bool = True,
) -> list[str]:
    """决定 rung 尝试顺序。cached_gear 命中则它优先, 其余按默认阶梯补全。

    默认阶梯: curl_cffi → patchright_headless → jina_reader → hitl
    curl_cffi 是最低成本档 (非 JS 站秒级返回), 失败/SPA/挑战页才升 patchright。
    needs_account=True 且无 storage_state → 直接跳 hitl (不走无账号档)。
    """
    if human_assist == "required":
        return ["hitl"]

    order = (
        ["patchright_headless", "curl_cffi", "jina_reader", "hitl"]
        if browser_first
        else ["curl_cffi", "patchright_headless", "jina_reader", "hitl"]
    )
    if not allow_jina:
        order = [gear for gear in order if gear != "jina_reader"]
    if human_assist == "off":
        order = [gear for gear in order if gear != "hitl"]
    if needs_account and not storage_state_path:
        log.info("needs_account=True but no storage_state for %s, jumping directly to hitl", domain)
        order = ["hitl"] if human_assist != "off" else []

    if browser_first and cached_gear not in {"patchright_headless", "hitl"}:
        cached_gear = None
    if not cached_gear or cached_gear not in order:
        return order
    # cached 优先, 然后其余 (去重保序)
    return [cached_gear] + [g for g in order if g != cached_gear]


def _normalize_ladder_tuple(result: tuple) -> tuple[str | None, int, str, str, str]:
    if len(result) == 5:
        return result  # type: ignore[return-value]
    html, status, final_url, gear_used = result
    return html, status, final_url, gear_used, ""


async def _try_patchright(
    url: str,
    storage_state_path: str | None,
    bypass_l3: bool,
    use_proxy: bool,
    domain: str,
    wait_strategy: str,
    safe_ip: str | None = None,
    session_id: str | None = None,
    proxy_url: str = "",
    account_id: str = "",
    capture_artifact: bool = False,
    wait_for_selector: str = "",
) -> tuple[str, int, str, str, str] | str | None:
    """试 patchright 档。

    返回:
      tuple  - 成功拿到 HTML
      "__SHORTCIRCUIT_HITL__" - 短路跳 HITL (登录页, 不试 jina)
      "__SSRF_BLOCKED__:<reason>" - 重定向到内网, 阻断
      None - 该档失败, 走下一档
    """
    try:
        r1 = await patchright_rung.fetch(
            url,
            storage_state_path=storage_state_path,
            bypass_l3=bypass_l3,
            use_proxy=use_proxy,
            wait_strategy=wait_strategy,
            safe_ip=safe_ip,
            session_id=session_id,
            proxy_url=proxy_url,
            account_id=account_id,
            capture_artifact=capture_artifact,
            wait_for_selector=wait_for_selector,
        )
        if r1.ok:
            # #6 SSRF 防重定向到内网: final_url 二次检查, 命中直接阻断
            if r1.final_url and await ssrf.is_blocked_async(r1.final_url):
                log.warning("SSRF redirect detected: %s → %s", url, r1.final_url)
                return f"__SSRF_BLOCKED__:{ssrf.block_reason(r1.final_url)}"
            det = await asyncio.to_thread(detect_mod.detect, r1.html, r1.http_status, bypass_l3=bypass_l3, final_url=r1.final_url)
            if det.is_ok:
                return r1.html, r1.http_status, r1.final_url, "patchright_headless", r1.artifact_id
            # 短路决策
            artifact_id = r1.artifact_id
            if not artifact_id:
                artifact_id = await asyncio.to_thread(
                    artifacts.save_artifact,
                    url=url,
                    reason=f"patchright-detect:{det.verdict.value}",
                    success=False,
                    final_url=r1.final_url or url,
                    http_status=r1.http_status,
                    gear_used="patchright_headless",
                    session_id=session_id or "",
                    html=r1.html,
                    console_messages=r1.console_messages,
                    request_failures=r1.request_failures,
                    extra={
                        "detect_verdict": det.verdict.value,
                        "detect_reason": det.reason,
                        "shortcircuit": det.should_shortcircuit,
                    },
                )
            sc = det.should_shortcircuit
            if sc == "jina":
                if artifact_id:
                    return f"__ARTIFACT__:{artifact_id}"
                log.info("shortcircuit patchright→jina: %s", det.reason)
                return None  # 走 jina 档
            elif sc == "hitl":
                if artifact_id:
                    return f"__SHORTCIRCUIT_HITL__:{artifact_id}"
                log.info("shortcircuit patchright→hitl: %s", det.reason)
                return "__SHORTCIRCUIT_HITL__"  # 上层直接跳 HITL
            else:
                log.info("patchright verdict %s, fallback", det.verdict.value)
                if det.verdict == detect_mod.Verdict.BLOCKED_GENERIC:
                    if artifact_id:
                        return f"__BLOCKED_BOT__:{artifact_id}"
                    return "__BLOCKED_BOT__"
                if det.verdict == detect_mod.Verdict.SPA_LOAD_INCOMPLETE:
                    # SPA 动态加载失败: 降级到下一档, 但不标 blocked-bot
                    # (不是反爬拦截, 标 blocked 会误触发 AIMD 减速该域)
                    log.warning("patchright SPA load incomplete for %s: %s, fallback", url, det.reason)
        if r1.artifact_id:
            return f"__ARTIFACT__:{r1.artifact_id}"
    except Exception as e:
        if isinstance(e, RateLimitError):
            raise
        log.warning("patchright rung failed: %s", e)
    return None


async def _try_curlcffi(
    url: str,
    bypass_l3: bool,
    use_proxy: bool,
    safe_ip: str | None = None,
    session_id: str | None = None,
    proxy_url: str = "",
    account_id: str = "",
) -> tuple[str, int, str, str] | str | None:
    """试 curl_cffi 档 (rung0, 最低成本)。

    用 curl_cffi 模拟 Chrome JA3/JA4 TLS 指纹 + HTTP/2 帧, 非 JS 站点秒级返回 HTML。
    detect 判 SPA/blocked → 返回 None 升 patchright; 判 OK → 返回 HTML 走 parser。
    needs_account 不在此处理 (上层 _gear_order 已跳过本档)。
    """
    if not curlcffi_rung.CURLCFFI_AVAILABLE:
        return None
    try:
        r0 = await curlcffi_rung.fetch(
            url,
            use_proxy=use_proxy,
            safe_ip=safe_ip,
            session_id=session_id,
            proxy_url=proxy_url,
            account_id=account_id,
        )
        if not r0.ok and r0.error.startswith("SSRF blocked:"):
            blocked_url = r0.error.removeprefix("SSRF blocked:").strip()
            blocked_url = blocked_url.removeprefix("redirect to ").strip()
            blocked_url = blocked_url.removeprefix("unresolved proxy target ").strip()
            blocked_url = blocked_url.removeprefix("unresolved proxy redirect target ").strip()
            return f"__SSRF_BLOCKED__:{ssrf.block_reason(blocked_url or url)}"
        if r0.ok:
            # SSRF 重定向二次检查
            if r0.final_url and await ssrf.is_blocked_async(r0.final_url):
                log.warning("SSRF redirect (curlcffi): %s → %s", url, r0.final_url)
                return f"__SSRF_BLOCKED__:{ssrf.block_reason(r0.final_url)}"
            det = await asyncio.to_thread(detect_mod.detect, r0.html, r0.http_status, bypass_l3=bypass_l3, final_url=r0.final_url)
            if det.is_ok:
                return r0.html, r0.http_status, r0.final_url, "curl_cffi"
            sc = det.should_shortcircuit
            if sc == "jina":
                log.info("shortcircuit curlcffi→patchright/jina: %s", det.reason)
                return None  # 升 patchright (patchright 会再判是否跳 jina)
            elif sc == "hitl":
                log.info("shortcircuit curlcffi→hitl: %s", det.reason)
                return None  # 升 patchright, patchright 会短路跳 hitl
            else:
                log.info("curlcffi verdict %s, fallback to patchright", det.verdict.value)
                if det.verdict == detect_mod.Verdict.BLOCKED_GENERIC:
                    return "__BLOCKED_BOT__"
                if det.verdict == detect_mod.Verdict.SPA_LOAD_INCOMPLETE:
                    # curl_cffi 不执行 JS, SPA 动态区本就拉不到 -> 升 patchright
                    log.info("curlcffi SPA load incomplete for %s, upgrade to patchright", url)
    except Exception as e:
        if isinstance(e, RateLimitError):
            raise
        log.warning("curlcffi rung failed: %s", e)
    return None


async def _try_jina(
    url: str,
    bypass_l3: bool,
    use_proxy: bool,
    needs_account: bool = False,
    proxy_url: str = "",
) -> tuple[str, int, str, str] | None:
    """试 jina 档。"""

    if needs_account:
        log.info("needs_account=True, skipping jina_reader for %s", url)
        return None

    try:
        jina_md = await jina_rung.fetch(
            url,
            needs_account=False,
            use_proxy=use_proxy,
            proxy_url=proxy_url,
        )
        if jina_md and jina_md.strip():
            # jina 返回 markdown, 但仍需检测 SPA 加载失败占位
            # (jina 服务端渲染也可能拿不到动态 API 数据, 如 4seas 的 app.sola.day)
            spa_fail = challenge_detect.has_spa_load_failure(jina_md)
            if spa_fail and not bypass_l3:
                log.warning("jina SPA load incomplete for %s: '%s', fallback", url, spa_fail)
                return None
            return jina_md, 200, url, "jina_reader"
    except Exception as e:
        if isinstance(e, RateLimitError):
            raise
        log.warning("jina_reader failed for %s: %s", url, e)
    return None


async def _try_hitl(
    url: str,
    *,
    domain: str,
    use_proxy: bool,
    conn,
    safe_ip: str | None = None,
    session_id: str | None = None,
    proxy_url: str = "",
    account_id: str = "",
    capture_artifact: bool = False,
) -> tuple[str | None, int, str, str] | tuple[str | None, int, str, str, str] | None:
    """HITL 档。无 GUI → 返回特殊标记。"""
    if not hitl_rung.has_display():
        return "__HITL_HEADLESS__", 0, "", ""

    try:
        r = await hitl_rung.fetch(
            url,
            domain=domain,
            profile_dir=account_vault.profile_dir(domain, account_id=account_id or None),
            use_proxy=use_proxy,
            safe_ip=safe_ip,
            session_id=session_id,
            proxy_url=proxy_url,
            account_id=account_id,
            capture_artifact=capture_artifact,
        )
        if r.ok:
            if r.final_url and await ssrf.is_blocked_async(r.final_url):
                log.warning("SSRF redirect detected in HITL: %s → %s", url, r.final_url)
                return f"__SSRF_BLOCKED__:{ssrf.block_reason(r.final_url)}"
            if account_id:
                try:
                    account_profiles.touch_verified(domain, account_id)
                except Exception:
                    pass
            return r.html, r.http_status, r.final_url, "hitl", r.artifact_id
        if r.error == "HITL_REQUIRED_BUT_HEADLESS":
            return "__HITL_HEADLESS__", 0, "", ""
        if r.error:
            return f"__HITL_UNAVAILABLE__:{r.error}", 0, "", "hitl", r.artifact_id
    except Exception as e:
        log.warning("hitl rung failed: %s", e)
    return None
