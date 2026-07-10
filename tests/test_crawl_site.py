import pytest

from trawler.crawl_site import (
    _aggregate_job_results,
    _extract_links_from_result,
    _normalize_seed_urls,
)
from trawler.raw_store import save_raw
from trawler.seen import url_id


@pytest.mark.asyncio
async def test_aggregate_job_results_success_contract():
    job = {
        "job_id": "job123",
        "start_url": "https://example.com/",
        "status": "completed",
        "completed": 0,
        "total": 0,
        "visited_json": "[]",
    }

    result = await _aggregate_job_results(job, conn=None)

    assert result.startswith("__TRAWLER_OK__:\n\n")
    assert "No pages crawled." in result


def test_extract_links_prefers_raw_metadata(tmp_db):
    base_url = "https://example.com/start"
    save_raw(
        url_id(base_url),
        url=base_url,
        final_url=base_url,
        title="Start",
        markdown="# Start\n\nNo markdown links here.",
        gear_used="curl_cffi",
        metadata={
            "links": [
                {"url": "https://example.com/docs", "text": "Docs"},
                {"url": "https://other.test/out", "text": "Out"},
            ]
        },
    )

    links = _extract_links_from_result(
        base_url,
        "__TRAWLER_OK__:\n\n# Start\n\nNo markdown links here.",
        same_domain_only=True,
        start_domain="example.com",
    )

    assert links == ["https://example.com/docs"]


@pytest.mark.asyncio
async def test_aggregate_job_results_strips_frontmatter_and_summarizes_errors(tmp_db):
    from trawler import db, frontier, jobs

    conn = db.connect()
    try:
        job_id = jobs.create_job(conn, "https://example.com/", total=2)
        ok_url = "https://example.com/ok"
        err_url = "https://example.com/err"
        frontier.enqueue(conn, job_id, ok_url)
        frontier.enqueue(conn, job_id, err_url)
        save_raw(
            url_id(ok_url),
            url=ok_url,
            final_url=ok_url,
            title="OK",
            markdown="# OK\n\nBody",
            gear_used="curl_cffi",
        )
        frontier.mark_fetched(conn, job_id, ok_url, raw_id=url_id(ok_url))
        frontier.mark_error(conn, job_id, err_url, "__TRAWLER_ERROR__:boom")
        jobs.update_progress(conn, job_id, visited=[ok_url, err_url], queue=[], completed=2)
        job = jobs.get_job(conn, job_id)
        assert job is not None
        result = await _aggregate_job_results(job, conn)
    finally:
        conn.close()

    assert "# OK" in result
    assert "gear_used: curl_cffi" not in result
    assert "raw missing" not in result
    assert "## Errors" in result
    assert err_url in result


def test_normalize_seed_urls_filters_duplicates_and_cross_domain():
    seeds = _normalize_seed_urls(
        [
            "https://example.com/docs?utm_source=x",
            "https://example.com/docs",
            "https://other.example/out",
            "https://example.com/a",
        ],
        "https://example.com/",
        same_domain_only=True,
        limit=10,
    )

    assert seeds == ["https://example.com/docs", "https://example.com/a"]


def test_normalize_seed_urls_applies_path_and_query_policy():
    seeds = _normalize_seed_urls(
        [
            "https://example.com/docs/a?version=1",
            "https://example.com/docs/a?version=2",
            "https://example.com/docs/private/secret",
            "https://example.com/blog/post",
        ],
        "https://example.com/",
        same_domain_only=True,
        limit=10,
        include_paths=["/docs/*"],
        exclude_paths=["/docs/private/*"],
        ignore_query_parameters=True,
    )

    assert seeds == ["https://example.com/docs/a"]


def test_extract_links_applies_path_policy():
    links = _extract_links_from_result(
        "https://example.com/start",
        "__TRAWLER_OK__:\n\n"
        "[Docs](https://example.com/docs/a)\n"
        "[Private](https://example.com/docs/private/secret)\n"
        "[Blog](https://example.com/blog/post)\n",
        same_domain_only=True,
        start_domain="example.com",
        include_paths=["/docs/*"],
        exclude_paths=["/docs/private/*"],
    )

    assert links == ["https://example.com/docs/a"]


def test_extract_links_can_include_subdomains():
    md = (
        "__TRAWLER_OK__:\n\n"
        "[Guide](https://docs.example.com/guide)\n"
        "[Other](https://other.test/guide)\n"
    )

    strict_links = _extract_links_from_result(
        "https://example.com/start",
        md,
        same_domain_only=True,
        start_domain="example.com",
    )
    subdomain_links = _extract_links_from_result(
        "https://example.com/start",
        md,
        same_domain_only=True,
        start_domain="example.com",
        include_subdomains=True,
    )

    assert strict_links == []
    assert subdomain_links == ["https://docs.example.com/guide"]


def test_extract_links_can_ignore_query_parameters():
    links = _extract_links_from_result(
        "https://example.com/start",
        "__TRAWLER_OK__:\n\n"
        "[A](https://example.com/docs?page=1)\n"
        "[B](https://example.com/docs?page=2)\n",
        same_domain_only=True,
        start_domain="example.com",
        ignore_query_parameters=True,
    )

    assert links == ["https://example.com/docs"]


def test_extract_links_respects_max_links_budget():
    links = _extract_links_from_result(
        "https://example.com/start",
        "__TRAWLER_OK__:\n\n"
        "[A](https://example.com/a)\n"
        "[B](https://example.com/b)\n"
        "[C](https://example.com/c)\n",
        same_domain_only=True,
        start_domain="example.com",
        max_links=2,
    )

    assert links == ["https://example.com/a", "https://example.com/b"]


@pytest.mark.asyncio
async def test_map_site_forwards_large_max_links(tmp_db, monkeypatch):
    from trawler import crawl_site as crawl_site_mod

    async def fake_crawl_url(*args, **kwargs):
        links = "\n".join(f"[Link {i}](https://example.com/page-{i})" for i in range(250))
        return f"__TRAWLER_OK__:\n\n{links}"

    monkeypatch.setattr(crawl_site_mod, "crawl_url", fake_crawl_url)

    result = await crawl_site_mod.map_site("https://example.com/", max_links=250)

    assert result["ok"] is True
    assert result["link_count"] == 250
    assert len(result["links"]) == 250


@pytest.mark.asyncio
async def test_crawl_site_clamps_max_pages_to_minimum(tmp_db, monkeypatch):
    from trawler import crawl_site as crawl_site_mod

    captured = {}

    async def fake_spider_loop(job_id, start_url, max_pages, *args, **kwargs):
        captured["job_id"] = job_id
        captured["max_pages"] = max_pages

    monkeypatch.setattr(crawl_site_mod, "_spider_loop", fake_spider_loop)

    result = await crawl_site_mod.crawl_site("https://example.com/", max_pages=-10)
    await __import__("asyncio").sleep(0)

    assert result["max_pages"] == 1
    assert captured["job_id"] == result["job_id"]
    assert captured["max_pages"] == 1


@pytest.mark.asyncio
async def test_spider_loop_honors_max_depth(tmp_db, monkeypatch):
    from trawler import config, db, frontier, jobs
    from trawler import crawl_site as crawl_site_mod

    conn = db.connect()
    try:
        job_id = jobs.create_job(conn, "https://example.com/", total=5)
        frontier.enqueue(conn, job_id, "https://example.com/", depth=0)
    finally:
        conn.close()

    async def fake_crawl_url(*args, **kwargs):
        return "__TRAWLER_OK__:\n\n# Home\n\n[Child](https://example.com/child)"

    monkeypatch.setattr(config, "SAME_DOMAIN_INTERVAL", 0.0)
    monkeypatch.setattr(crawl_site_mod, "crawl_url", fake_crawl_url)

    await crawl_site_mod._frontier_spider_loop(
        job_id,
        "https://example.com/",
        max_pages=5,
        same_domain_only=True,
        max_depth=0,
    )

    conn = db.connect()
    try:
        queued = frontier.queued_urls(conn, job_id)
        page = frontier.result_page(conn, job_id)
    finally:
        conn.close()

    assert queued == []
    assert [item["url"] for item in page["items"]] == ["https://example.com/"]


@pytest.mark.asyncio
async def test_crawl_url_blocks_final_url_outside_scope(tmp_db, monkeypatch):
    import json

    from trawler import config
    from trawler import crawl_url as crawl_url_mod

    async def fake_resolve(*args, **kwargs):
        return False, "93.184.216.34"

    async def fake_ladder(*args, **kwargs):
        return (
            "<html><body><article><h1>Outside</h1><p>body text</p></article></body></html>",
            200,
            "https://other.test/out",
            "curl_cffi",
            "",
        )

    monkeypatch.setattr(config, "RESPECT_ROBOTS", False)
    monkeypatch.setattr(crawl_url_mod.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(crawl_url_mod, "_fetch_ladder", fake_ladder)

    result = await crawl_url_mod.crawl_url(
        "https://example.com/start",
        allowed_domain="example.com",
    )

    assert result.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(result[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "blocked-scope"
    assert "https://other.test/out" in payload["message"]


@pytest.mark.asyncio
async def test_spider_loop_retries_retryable_errors(tmp_db, monkeypatch):
    from trawler import config, db, frontier, jobs
    from trawler import crawl_site as crawl_site_mod

    conn = db.connect()
    try:
        job_id = jobs.create_job(conn, "https://example.com/", total=1)
        frontier.enqueue(conn, job_id, "https://example.com/", depth=0)
    finally:
        conn.close()

    calls = 0

    async def fake_crawl_url(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                '__TRAWLER_ERROR__:{"errorType":"timeout","message":"slow",'
                '"retryable":true,"suggestedAction":"retry"}'
            )
        return "__TRAWLER_OK__:\n\n# OK"

    monkeypatch.setattr(config, "FRONTIER_RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(config, "FRONTIER_MAX_RETRIES", 2)
    monkeypatch.setattr(config, "JOB_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(crawl_site_mod, "crawl_url", fake_crawl_url)

    await crawl_site_mod._frontier_spider_loop(
        job_id,
        "https://example.com/",
        max_pages=1,
        same_domain_only=True,
    )

    conn = db.connect()
    try:
        status = jobs.get_job(conn, job_id)
        page = frontier.result_page(conn, job_id)
    finally:
        conn.close()

    assert calls == 2
    assert status is not None
    assert status["status"] == "completed"
    assert page["items"][0]["status"] == "fetched"
