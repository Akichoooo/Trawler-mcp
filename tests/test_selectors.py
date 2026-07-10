import pytest


def test_apply_selectors_crops_matching_html():
    from trawler.parser import selectors

    html = """
    <html><body>
      <nav>Drop nav</nav>
      <main><h1>Keep</h1><p>Body</p></main>
      <footer>Drop footer</footer>
    </body></html>
    """

    cropped, report = selectors.apply_selectors(html, ["main"])

    assert "Keep" in cropped
    assert "Drop nav" not in cropped
    assert report["selector_used"] == "main"
    assert report["selector_match_count"] == 1


def test_apply_selectors_miss_or_invalid_selector_falls_back():
    from trawler.parser import selectors

    html = "<html><body><main>Keep all</main></body></html>"
    cropped, report = selectors.apply_selectors(html, ["???", ".missing"])

    assert cropped == html
    assert report["selector_match_count"] == 0
    assert report["selector_errors"]


@pytest.mark.asyncio
async def test_crawl_url_applies_site_rule_selectors(tmp_db, monkeypatch):
    from trawler import config
    from trawler import crawl_url as crawl_url_mod
    from trawler.parser import extract as parser_extract
    from trawler.raw_store import read_metadata
    from trawler.seen import url_id
    from trawler.site_rules import SiteRule

    monkeypatch.setattr(config, "RESPECT_ROBOTS", False)

    async def fake_resolve(*args, **kwargs):
        return False, "93.184.216.34"

    async def fake_ladder(*args, **kwargs):
        return (
            """
            <html><body>
              <nav>Drop nav</nav>
              <main><h1>Keep</h1><p>Body</p></main>
            </body></html>
            """,
            200,
            "https://example.com/",
            "curl_cffi",
            "",
        )

    def fake_extract(html, url=""):
        assert "Keep" in html
        assert "Drop nav" not in html
        return "# Keep\n\nBody"

    monkeypatch.setattr(crawl_url_mod.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(crawl_url_mod, "_fetch_ladder", fake_ladder)
    monkeypatch.setattr(
        crawl_url_mod.site_rules,
        "load",
        lambda domain: SiteRule(domain=domain, selectors=["main"]),
    )
    monkeypatch.setattr(parser_extract, "extract", fake_extract)

    result = await crawl_url_mod.crawl_url("https://example.com/", force_refresh=True)

    assert result.startswith("__TRAWLER_OK__:")
    metadata = read_metadata(url_id("https://example.com/"))
    assert metadata["selector_used"] == "main"
    assert metadata["selector_match_count"] == 1
    assert metadata["char_count"] == len("# Keep\n\nBody")
    assert metadata["heading_count"] == 1


@pytest.mark.asyncio
async def test_crawl_url_selector_miss_still_falls_back(tmp_db, monkeypatch):
    from trawler import config
    from trawler import crawl_url as crawl_url_mod
    from trawler.parser import extract as parser_extract
    from trawler.site_rules import SiteRule

    monkeypatch.setattr(config, "RESPECT_ROBOTS", False)

    async def fake_resolve(*args, **kwargs):
        return False, "93.184.216.34"

    async def fake_ladder(*args, **kwargs):
        return (
            "<html><body><main>Keep</main></body></html>",
            200,
            "https://example.com/",
            "curl_cffi",
            "",
        )

    monkeypatch.setattr(crawl_url_mod.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(crawl_url_mod, "_fetch_ladder", fake_ladder)
    monkeypatch.setattr(
        crawl_url_mod.site_rules,
        "load",
        lambda domain: SiteRule(domain=domain, selectors=[".missing"]),
    )
    monkeypatch.setattr(parser_extract, "extract", lambda html, url="": "# Keep")

    result = await crawl_url_mod.crawl_url("https://example.com/", force_refresh=True)

    assert result.startswith("__TRAWLER_OK__:")
