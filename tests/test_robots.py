import pytest

from trawler import config, robots


@pytest.mark.asyncio
async def test_robots_fetch_failure_fails_closed(monkeypatch):
    async def fake_fetch(domain: str, use_proxy: bool = False):
        return "__FETCH_FAILED__"

    robots.clear_cache()
    monkeypatch.setattr(config, "ROBOTS_FAIL_CLOSED", True)
    monkeypatch.setattr(robots, "_fetch_robots_txt", fake_fetch)

    assert await robots.is_allowed("https://example.com/private") is False


@pytest.mark.asyncio
async def test_robots_fetch_failure_can_fail_open(monkeypatch):
    async def fake_fetch(domain: str, use_proxy: bool = False):
        return "__FETCH_FAILED__"

    robots.clear_cache()
    monkeypatch.setattr(config, "ROBOTS_FAIL_CLOSED", False)
    monkeypatch.setattr(robots, "_fetch_robots_txt", fake_fetch)

    assert await robots.is_allowed("https://example.com/private") is True


@pytest.mark.asyncio
async def test_robots_404_allows_and_caches(monkeypatch):
    calls = 0

    async def fake_fetch(domain: str, use_proxy: bool = False):
        nonlocal calls
        calls += 1
        return None

    robots.clear_cache()
    monkeypatch.setattr(robots, "_fetch_robots_txt", fake_fetch)

    assert await robots.is_allowed("https://example.com/one") is True
    assert await robots.is_allowed("https://example.com/two") is True
    assert calls == 1


@pytest.mark.asyncio
async def test_robots_cache_is_origin_scoped(monkeypatch):
    fetched: list[str] = []

    async def fake_fetch(origin: str, use_proxy: bool = False):
        fetched.append(origin)
        if origin == "http://example.com:8080":
            return "User-agent: *\nDisallow: /private\n"
        return None

    robots.clear_cache()
    monkeypatch.setattr(robots, "_fetch_robots_txt", fake_fetch)

    assert await robots.is_allowed("http://example.com:8080/private") is False
    assert await robots.is_allowed("https://example.com/private") is True
    assert fetched == ["http://example.com:8080", "https://example.com"]


@pytest.mark.asyncio
async def test_crawl_url_robots_error_fail_closed(tmp_db, monkeypatch):
    from trawler import crawl_url as crawl_url_mod

    monkeypatch.setattr(config, "RESPECT_ROBOTS", True)
    monkeypatch.setattr(config, "ROBOTS_FAIL_CLOSED", True)

    async def fake_resolve(*args, **kwargs):
        return False, "93.184.216.34"

    async def fake_allowed(*args, **kwargs):
        raise RuntimeError("robots unreachable")

    monkeypatch.setattr(crawl_url_mod.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(robots, "is_allowed", fake_allowed)

    result = await crawl_url_mod.crawl_url("https://example.com/")

    assert result.startswith("__TRAWLER_ERROR__:")
    assert "blocked-robots" in result
    assert "robots unreachable" in result


@pytest.mark.asyncio
async def test_crawl_url_robots_error_fail_open_continues(tmp_db, monkeypatch):
    from trawler import crawl_url as crawl_url_mod

    monkeypatch.setattr(config, "RESPECT_ROBOTS", True)
    monkeypatch.setattr(config, "ROBOTS_FAIL_CLOSED", False)

    async def fake_resolve(*args, **kwargs):
        return False, "93.184.216.34"

    async def fake_allowed(*args, **kwargs):
        raise RuntimeError("robots unreachable")

    async def fake_ladder(*args, **kwargs):
        return (
            "<html><body><article><h1>OK</h1><p>body text</p></article></body></html>",
            200,
            "https://example.com/",
            "curl_cffi",
            "",
        )

    monkeypatch.setattr(crawl_url_mod.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(robots, "is_allowed", fake_allowed)
    monkeypatch.setattr(crawl_url_mod, "_fetch_ladder", fake_ladder)

    result = await crawl_url_mod.crawl_url(
        "https://example.com/",
        force_refresh=True,
        bypass_l3=True,
    )

    assert result.startswith("__TRAWLER_OK__:")


@pytest.mark.asyncio
async def test_crawl_url_user_authorized_access_skips_robots_precheck(tmp_db, monkeypatch):
    from trawler import crawl_url as crawl_url_mod

    monkeypatch.setattr(config, "RESPECT_ROBOTS", True)

    async def fake_resolve(*args, **kwargs):
        return False, "93.184.216.34"

    async def fake_allowed(*args, **kwargs):
        return False

    async def fake_ladder(*args, **kwargs):
        return (
            "<html><body><article><h1>OK</h1><p>body text</p></article></body></html>",
            200,
            "https://example.com/",
            "curl_cffi",
            "",
        )

    monkeypatch.setattr(crawl_url_mod.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(robots, "is_allowed", fake_allowed)
    monkeypatch.setattr(crawl_url_mod, "_fetch_ladder", fake_ladder)

    blocked = await crawl_url_mod.crawl_url("https://example.com/", force_refresh=True)
    allowed = await crawl_url_mod.crawl_url(
        "https://example.com/",
        force_refresh=True,
        user_authorized_access=True,
        bypass_l3=True,
    )

    assert blocked.startswith("__TRAWLER_ERROR__:")
    assert "user_authorized_access=true" in blocked
    assert allowed.startswith("__TRAWLER_OK__:")
