"""Site crawling orchestration backed by the persistent frontier."""

from __future__ import annotations

import asyncio
import json
import logging

from trawler import config, frontier, jobs
from trawler import db as db_mod
from trawler.crawl_budget import CrawlBudget
from trawler.crawl_policy import CrawlPolicy, drop_query
from trawler.crawl_url import crawl_url
from trawler.errors import format_error, format_ok
from trawler.raw_store import raw_path, read_metadata, strip_frontmatter
from trawler.seen import url_id

log = logging.getLogger("trawler.crawl_site")

# 鍚庡彴浣滀笟娉ㄥ唽琛?(杩涚▼鍐? 閲嶅惎鍚庨潬 SQLite crawl_jobs 琛ㄦ仮澶?鏍?failed)
_active_jobs: dict[str, asyncio.Task] = {}


def _retryable_error(result: str) -> bool:
    prefix = "__TRAWLER_ERROR__:"
    if not result.startswith(prefix):
        return False
    try:
        payload = json.loads(result[len(prefix):])
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("retryable") is True


def _retry_delay(retry_count: int) -> float:
    base = float(getattr(config, "FRONTIER_RETRY_BASE_SECONDS", 2.0))
    return min(base * (2 ** max(0, retry_count)), 300.0)


async def crawl_site(
    start_url: str,
    *,
    max_pages: int | None = None,
    same_domain_only: bool = True,
    use_proxy: bool = False,
    seed_urls: list[str] | None = None,
    max_depth: int = -1,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    include_subdomains: bool = False,
    ignore_query_parameters: bool = False,
    conn=None,
) -> dict:
    """Start a crawl-site job and return job metadata."""

    own_conn = conn is None
    if own_conn:
        conn = db_mod.connect()

    policy = CrawlPolicy.from_options(
        start_url,
        same_domain_only=same_domain_only,
        max_depth=max_depth,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        include_subdomains=include_subdomains,
        ignore_query_parameters=ignore_query_parameters,
    )
    start_url = policy.start_url
    max_pages = max(1, min(int(max_pages or config.MAX_PAGES), config.MAX_PAGES_HARD))

    job_id = jobs.create_job(conn, start_url, total=max_pages)
    frontier.enqueue(conn, job_id, start_url, depth=0, priority=100)
    normalized_seeds = policy.normalize_seed_urls(seed_urls or [], limit=max_pages)
    for seed_url in normalized_seeds:
        frontier.enqueue(conn, job_id, seed_url, depth=0, parent_url=start_url, priority=50)
    if own_conn:
        conn.close()

    # 鍚庡彴 task (鐢ㄧ嫭绔嬭繛鎺? 涓嶉樆濉炶皟鐢ㄦ柟)
    task = asyncio.create_task(
        _spider_loop(
            job_id,
            start_url,
            max_pages,
            same_domain_only,
            use_proxy,
            max_depth=max_depth,
            include_paths=include_paths or [],
            exclude_paths=exclude_paths or [],
            include_subdomains=include_subdomains,
            ignore_query_parameters=ignore_query_parameters,
        )
    )
    _active_jobs[job_id] = task

    return {
        "job_id": job_id,
        "status": "crawling",
        "max_pages": max_pages,
        "seed_count": len(normalized_seeds),
    }


def _normalize_seed_urls(
    seed_urls: list[str],
    start_url: str,
    *,
    same_domain_only: bool,
    limit: int,
    include_subdomains: bool = False,
    ignore_query_parameters: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    max_links: int = 200,
) -> list[str]:
    policy = CrawlPolicy.from_options(
        start_url,
        same_domain_only=same_domain_only,
        include_subdomains=include_subdomains,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        ignore_query_parameters=ignore_query_parameters,
    )
    return policy.normalize_seed_urls(seed_urls, limit=limit)


async def map_site(
    start_url: str,
    *,
    max_links: int = 200,
    same_domain_only: bool = True,
    use_proxy: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    include_subdomains: bool = False,
    ignore_query_parameters: bool = False,
    conn=None,
) -> dict:
    """Fetch one page and return its DOM-discovered links."""
    max_links = max(1, int(max_links))
    policy = CrawlPolicy.from_options(
        start_url,
        same_domain_only=same_domain_only,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        include_subdomains=include_subdomains,
        ignore_query_parameters=ignore_query_parameters,
    )
    start_url = policy.start_url
    start_domain = policy.start_domain
    md = await crawl_url(
        start_url,
        use_proxy=use_proxy,
        allowed_domain=policy.allowed_domain,
        include_subdomains=policy.include_subdomains,
        include_paths=list(policy.include_paths),
        exclude_paths=list(policy.exclude_paths),
        ignore_query_parameters=policy.ignore_query_parameters,
        conn=conn,
    )
    if not md.startswith("__TRAWLER_OK__:"):
        return {"ok": False, "error": md, "links": []}

    links = _extract_links_from_result(
        start_url,
        md,
        same_domain_only,
        start_domain,
        include_subdomains=include_subdomains,
        ignore_query_parameters=ignore_query_parameters,
        include_paths=include_paths or [],
        exclude_paths=exclude_paths or [],
        max_links=max_links,
    )
    return {
        "ok": True,
        "url": start_url,
        "link_count": len(links),
        "links": links[:max_links],
    }


async def _frontier_spider_loop(
    job_id: str,
    start_url: str,
    max_pages: int,
    same_domain_only: bool,
    use_proxy: bool = False,
    max_depth: int = -1,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    include_subdomains: bool = False,
    ignore_query_parameters: bool = False,
) -> None:
    """Background spider loop backed by the persistent frontier."""
    conn = db_mod.connect()
    budget = CrawlBudget.from_config(max_pages)
    policy = CrawlPolicy.from_options(
        start_url,
        same_domain_only=same_domain_only,
        max_depth=max_depth,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        include_subdomains=include_subdomains,
        ignore_query_parameters=ignore_query_parameters,
    )
    start_domain = policy.start_domain

    try:
        while True:
            job = jobs.get_job(conn, job_id)
            if job is None or job["status"] == "cancelled":
                break

            frontier.release_expired_leases(conn, job_id)
            counts = frontier.counts(conn, job_id)
            stop_reason = budget.stop_reason(counts)
            if stop_reason:
                jobs.complete_job(conn, job_id, budget.status_for_stop_reason(stop_reason))
                break

            request = frontier.lease_next(conn, job_id)
            if request is None:
                if frontier.has_pending(conn, job_id):
                    await asyncio.sleep(config.JOB_POLL_INTERVAL)
                    continue
                jobs.complete_job(conn, job_id, "completed")
                break

            url = request["url"]
            depth = int(request.get("depth") or 0)

            await asyncio.sleep(config.SAME_DOMAIN_INTERVAL)
            md = await crawl_url(
                url,
                use_proxy=use_proxy,
                allowed_domain=policy.allowed_domain,
                include_subdomains=policy.include_subdomains,
                include_paths=list(policy.include_paths),
                exclude_paths=list(policy.exclude_paths),
                ignore_query_parameters=policy.ignore_query_parameters,
                conn=conn,
            )

            if md.startswith("__TRAWLER_OK__:"):
                frontier.mark_fetched(conn, job_id, url, raw_id=url_id(url))
                if policy.should_expand_depth(depth):
                    new_links = _extract_links_from_result(
                        url,
                        md,
                        same_domain_only,
                        start_domain,
                        include_subdomains=policy.include_subdomains,
                        ignore_query_parameters=policy.ignore_query_parameters,
                        include_paths=list(policy.include_paths),
                        exclude_paths=list(policy.exclude_paths),
                        max_links=budget.max_links_per_page,
                    )
                    for link in new_links:
                        frontier.enqueue(conn, job_id, link, depth=depth + 1, parent_url=url)
            else:
                retry_count = int(request.get("retry_count") or 0)
                if _retryable_error(md) and retry_count < config.FRONTIER_MAX_RETRIES:
                    frontier.mark_retry(
                        conn,
                        job_id,
                        url,
                        md,
                        delay_seconds=_retry_delay(retry_count),
                    )
                else:
                    frontier.mark_error(conn, job_id, url, md)

            counts = frontier.counts(conn, job_id)
            jobs.update_progress(
                conn,
                job_id,
                visited=frontier.done_urls(conn, job_id, limit=max_pages),
                queue=frontier.queued_urls(conn, job_id),
                completed=counts["terminal"],
            )
            log.info("job %s: %d/%d %s", job_id, counts["terminal"], max_pages, url)
    except asyncio.CancelledError:
        jobs.complete_job(conn, job_id, "cancelled")
    except Exception:
        log.exception("spider job %s failed", job_id)
        jobs.complete_job(conn, job_id, "failed")
    finally:
        conn.close()
        _active_jobs.pop(job_id, None)


async def _spider_loop(
    job_id: str,
    start_url: str,
    max_pages: int,
    same_domain_only: bool,
    use_proxy: bool = False,
    *,
    max_depth: int = -1,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    include_subdomains: bool = False,
    ignore_query_parameters: bool = False,
) -> None:
    """Run a frontier-backed background spider loop."""
    await _frontier_spider_loop(
        job_id,
        start_url,
        max_pages,
        same_domain_only,
        use_proxy,
        max_depth=max_depth,
        include_paths=include_paths or [],
        exclude_paths=exclude_paths or [],
        include_subdomains=include_subdomains,
        ignore_query_parameters=ignore_query_parameters,
    )


def _extract_links_from_result(
    base_url: str,
    md_or_failed: str,
    same_domain_only: bool,
    start_domain: str,
    *,
    include_subdomains: bool = False,
    ignore_query_parameters: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    max_links: int = 200,
) -> list[str]:
    """Extract same-domain links from raw metadata or markdown."""
    # crawl_url 鐜板湪杩斿洖 __TRAWLER_OK__:/__TRAWLER_ERROR__: 鍓嶇紑, 澶辫触涓嶆彁閾炬帴
    if not md_or_failed or not md_or_failed.startswith("__TRAWLER_OK__:"):
        return []

    policy = CrawlPolicy.from_options(
        f"https://{start_domain}/" if start_domain else base_url,
        same_domain_only=same_domain_only,
        include_subdomains=include_subdomains,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        ignore_query_parameters=ignore_query_parameters,
    )
    links: list[str] = []
    seen: set[str] = set()
    metadata = read_metadata(url_id(base_url))
    raw_links = metadata.get("links")
    if isinstance(raw_links, list):
        for item in raw_links:
            if isinstance(item, dict):
                raw_link = item.get("url")
            else:
                raw_link = item
            if not isinstance(raw_link, str):
                continue
            link = policy.normalize_page_url(raw_link)
            if not link:
                continue
            if link not in seen:
                seen.add(link)
                links.append(link)
                if len(links) >= max_links:
                    return links
        if links:
            return links

    try:
        import re
        # 鎻愭墍鏈?markdown 閾炬帴 [text](url) 鈥?url 鍙兘鏄粷瀵规垨鐩稿
        # 鏀硅繘: 鏀寔 URL 鍚嫭鍙?(濡?Wikipedia /wiki/Foo_(bar)), 鍏佽宓屽涓€灞?()
        for m in re.finditer(r"\[[^\]]*\]\(((?:[^\s()]|\([^\s()]*\))+)\)", md_or_failed):
            raw_link = m.group(1).strip()
            if not raw_link or raw_link.startswith("#"):
                continue
            # 鐩稿閾炬帴鐢?base_url 琛ュ叏
            if raw_link.startswith("http://") or raw_link.startswith("https://"):
                link = policy.normalize_page_url(raw_link)
            else:
                link = policy.normalize_page_url(raw_link, base_url=base_url)
            if not link:
                continue
            if link not in seen:
                seen.add(link)
                links.append(link)
                if len(links) >= max_links:
                    return links
    except Exception:
        pass
    return links


def _domain_allowed(url: str, start_domain: str, *, include_subdomains: bool) -> bool:
    policy = CrawlPolicy.from_options(
        f"https://{start_domain}/",
        same_domain_only=True,
        include_subdomains=include_subdomains,
    )
    return policy.domain_allowed(url)


def _path_allowed(url: str, include_paths: list[str], exclude_paths: list[str]) -> bool:
    policy = CrawlPolicy.from_options(
        url,
        same_domain_only=False,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
    )
    return policy.path_allowed(url)


def _drop_query(url: str) -> str:
    return drop_query(url)


async def wait_for_job(job_id: str, *, timeout: int | None = None, progress_cb=None) -> str:
    """Wait for a crawl job and return aggregated markdown."""
    if timeout is None:
        timeout = config.WAIT_FOR_JOB_TIMEOUT
    from trawler import db as db_mod

    deadline = asyncio.get_event_loop().time() + timeout
    conn = db_mod.connect()

    try:
        while asyncio.get_event_loop().time() < deadline:
            job = jobs.get_job(conn, job_id)
            if job is None:
                return format_error("job-not-found", f"Job {job_id} not found")

            if job["status"] in ("completed", "failed", "cancelled"):
                # 瀹屾垚: 鑱氬悎 raw
                return await _aggregate_job_results(job, conn)

            # 杩樺湪璺? 蹇冭烦
            if progress_cb:
                try:
                    # 鍏煎 sync 鍜?async progress_cb
                    if asyncio.iscoroutinefunction(progress_cb):
                        await progress_cb(job["completed"], job["total"])
                    else:
                        progress_cb(job["completed"], job["total"])
                except Exception:
                    pass  # progress 鏄?best-effort
            await asyncio.sleep(config.JOB_POLL_INTERVAL)

        return format_error("job-timeout", f"Job {job_id} exceeded {timeout}s")
    finally:
        conn.close()


def get_job_status(job_id: str) -> dict | None:
    """Return non-blocking crawl job status."""
    from trawler import db as db_mod
    conn = db_mod.connect()
    try:
        job = jobs.get_status(conn, job_id)
        if job is None:
            return None
        job["frontier"] = frontier.counts(conn, job_id)
        return job
    finally:
        conn.close()


def cancel_job(job_id: str) -> bool:
    """Cancel a crawl job and request task cancellation if it is active."""
    from trawler import db as db_mod

    conn = db_mod.connect()
    try:
        changed = jobs.cancel_job(conn, job_id)
    finally:
        conn.close()

    task = _active_jobs.get(job_id)
    if task is not None:
        task.cancel()
    return changed or task is not None


def get_job_errors(job_id: str, *, limit: int = 50) -> list[dict]:
    from trawler import db as db_mod

    conn = db_mod.connect()
    try:
        return frontier.errors(conn, job_id, limit=limit)
    finally:
        conn.close()


def get_job_results(job_id: str, *, cursor: int = 0, limit: int = 20) -> dict:
    from trawler import db as db_mod

    conn = db_mod.connect()
    try:
        return frontier.result_page(conn, job_id, cursor=cursor, limit=limit)
    finally:
        conn.close()


async def _aggregate_job_results(job: dict, conn) -> str:
    import asyncio
    import json
    try:
        visited = json.loads(job.get("visited_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        visited = []
    if conn is not None:
        frontier_urls = frontier.done_urls(conn, job["job_id"])
        if frontier_urls:
            visited = frontier_urls

    if not visited:
        return format_ok(f"# Crawl job {job['job_id']}\n\nNo pages crawled.")

    parts = [f"# Crawl job {job['job_id']}\n\nStart: {job['start_url']}\n"
             f"Status: {job['status']}\nPages: {job['completed']}/{job['total']}\n"]

    from trawler.seen import url_id
    
    # 闄愬埗鏈€澶у苟鍙戝害锛岄槻姝㈠悓鏃跺紑鏁扮櫨涓枃浠舵弿杩扮鎵撶垎绯荤粺
    sem = asyncio.Semaphore(20)
    raw_ids: dict[str, str] = {}
    errors: list[dict] = []
    if conn is not None:
        rows = conn.execute(
            "SELECT url, status, raw_id, last_error FROM frontier_requests "
            "WHERE job_id = ? AND status IN ('fetched', 'error') ORDER BY updated_at ASC",
            (job["job_id"],),
        ).fetchall()
        raw_ids = {row["url"]: row["raw_id"] for row in rows if row["status"] == "fetched"}
        errors = [dict(row) for row in rows if row["status"] == "error"]

    async def _read_url(url: str) -> str:
        async with sem:
            rid = raw_ids.get(url) or url_id(url)
            p = raw_path(rid)
            try:
                if await asyncio.to_thread(p.exists):
                    content = await asyncio.to_thread(p.read_text, encoding="utf-8")
                    return f"\n---\n\n## Source: {url}\n\n{strip_frontmatter(content)}"
                return ""
            except Exception as e:
                return f"\n---\n\n## Source: {url}\n\n(read error: {e})"

    fetched_urls = [url for url in visited if not raw_ids or url in raw_ids]
    results = await asyncio.gather(*[_read_url(url) for url in fetched_urls])
    parts.extend(result for result in results if result)
    if errors:
        parts.append("\n---\n\n## Errors\n")
        for item in errors[:20]:
            parts.append(f"- {item['url']}: {item.get('last_error') or 'error'}")
    
    return format_ok("\n".join(parts))
