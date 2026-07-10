import pytest

from trawler.errors import format_ok


def test_gear_order_supports_authorized_browser_policy():
    from trawler.crawl_url import _gear_order

    assert _gear_order(None, False, None, "example.com", human_assist="required") == ["hitl"]
    assert "hitl" not in _gear_order(None, False, None, "example.com", human_assist="off")
    assert _gear_order(
        "curl_cffi",
        False,
        "state.json",
        "example.com",
        browser_first=True,
        allow_jina=False,
    ) == ["patchright_headless", "curl_cffi", "hitl"]
    assert _gear_order(
        None,
        True,
        None,
        "example.com",
        human_assist="off",
    ) == []


def test_effective_cache_mode_for_browser_promises():
    from trawler.page_retrieval import effective_cache_mode

    assert effective_cache_mode(
        access_mode="user_authorized",
        extract_mode="page",
        cache_mode="enabled",
    ) == "write_only"
    assert effective_cache_mode(
        access_mode="standard",
        extract_mode="screenshot",
        cache_mode="read_only",
    ) == "write_only"
    assert effective_cache_mode(
        access_mode="standard",
        extract_mode="page",
        cache_mode="enabled",
    ) == "enabled"


@pytest.mark.asyncio
async def test_retrieve_page_forwards_authorized_intent(monkeypatch):
    from trawler import page_retrieval

    captured = {}

    async def fake_crawl_url(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return format_ok("# OK")

    monkeypatch.setattr(page_retrieval, "crawl_url", fake_crawl_url)

    result = await page_retrieval.retrieve_page(
        "https://example.com/private",
        access_mode="user_authorized",
        account_id="work",
        human_assist="auto",
        extract_mode="selector",
        selector=".post",
    )

    assert result.structured["ok"] is True
    assert result.structured["access_mode"] == "user_authorized"
    assert captured["args"] == ("https://example.com/private",)
    assert captured["kwargs"]["cache_mode"] == "write_only"
    assert captured["kwargs"]["user_authorized_access"] is True
    assert captured["kwargs"]["account_id"] == "work"
    assert captured["kwargs"]["human_assist"] == "auto"
    assert captured["kwargs"]["selector"] == ".post"
    assert captured["kwargs"]["capture_artifact"] is False


@pytest.mark.asyncio
async def test_retrieve_page_screenshot_warns_when_no_artifact(monkeypatch):
    from trawler import page_retrieval

    async def fake_crawl_url(*args, **kwargs):
        return format_ok("# OK")

    monkeypatch.setattr(page_retrieval, "crawl_url", fake_crawl_url)

    result = await page_retrieval.retrieve_page(
        "https://example.com/",
        extract_mode="screenshot",
    )

    assert result.screenshot is None
    assert result.structured["extract_mode"] == "screenshot"
    assert result.structured["screenshot_error"].startswith("screenshot unavailable")


@pytest.mark.asyncio
async def test_crawl_url_skips_expired_account_storage_state(tmp_db, monkeypatch):
    from cryptography.fernet import Fernet

    from trawler import account_profiles, account_vault, crawl_url

    async def fake_resolve(url):
        return False, "93.184.216.34"

    captured = {}

    async def fake_fetch_ladder(*args, **kwargs):
        captured.update(kwargs)
        return (
            "<html><body><h1>Hello</h1><p>This authorized page has enough text "
            "for parser extraction and should not use expired state.</p></body></html>",
            200,
            "https://example.com/private",
            "hitl",
            "",
        )

    monkeypatch.setenv("TRAWLER_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(account_vault, "_fernet", None)
    monkeypatch.setattr(crawl_url.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(crawl_url, "_fetch_ladder", fake_fetch_ladder)

    account_profiles.register_profile("example.com", account_id="work", make_default=True)
    account_vault.save_storage_state(
        "example.com",
        {"cookies": [{"name": "sid", "value": "old"}], "origins": []},
        account_id="work",
    )
    account_profiles.mark_profile_status("example.com", "work", "expired")

    result = await crawl_url.crawl_url(
        "https://example.com/private",
        cache_mode="disabled",
        user_authorized_access=True,
        account_id="work",
        bypass_l3=True,
    )

    assert result.startswith("__TRAWLER_OK__:")
    assert captured["storage_state"] is None
    assert captured["needs_account"] is True
