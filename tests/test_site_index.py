import httpx
import pytest


def test_parse_sitemap_urlset_filters_and_dedupes():
    from trawler import site_index

    xml = """
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/a?utm_source=x</loc></url>
      <url><loc>https://example.com/a</loc></url>
      <url><loc>https://other.example/b</loc></url>
    </urlset>
    """

    result = site_index.parse_index_document(
        xml,
        "https://example.com/",
        same_domain_only=True,
    )

    assert result["kind"] == "urlset"
    assert result["urls"] == ["https://example.com/a"]


def test_parse_sitemap_urlset_applies_crawl_policy():
    from trawler import site_index

    xml = """
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://docs.example.com/docs/a?version=1</loc></url>
      <url><loc>https://docs.example.com/docs/a?version=2</loc></url>
      <url><loc>https://docs.example.com/docs/private/secret</loc></url>
      <url><loc>https://example.com/blog/post</loc></url>
      <url><loc>https://other.test/docs/out</loc></url>
    </urlset>
    """

    result = site_index.parse_index_document(
        xml,
        "https://example.com/",
        same_domain_only=True,
        include_subdomains=True,
        include_paths=["/docs/*"],
        exclude_paths=["/docs/private/*"],
        ignore_query_parameters=True,
    )

    assert result["kind"] == "urlset"
    assert result["urls"] == ["https://docs.example.com/docs/a"]


def test_parse_sitemap_index_and_feeds():
    from trawler import site_index

    sitemap_index = """
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>/posts.xml</loc></sitemap>
    </sitemapindex>
    """
    parsed_index = site_index.parse_index_document(sitemap_index, "https://example.com/root.xml")
    assert parsed_index["sitemap_urls"] == ["https://example.com/posts.xml"]

    rss = """
    <rss><channel>
      <item><link>https://example.com/post-1</link></item>
    </channel></rss>
    """
    parsed_rss = site_index.parse_index_document(rss, "https://example.com/feed.xml")
    assert parsed_rss["kind"] == "rss"
    assert parsed_rss["urls"] == ["https://example.com/post-1"]

    atom = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><link href="https://example.com/post-2"/></entry>
    </feed>
    """
    parsed_atom = site_index.parse_index_document(atom, "https://example.com/atom.xml")
    assert parsed_atom["kind"] == "atom"
    assert parsed_atom["urls"] == ["https://example.com/post-2"]


def test_parse_robots_and_html_feed_links():
    from trawler import site_index

    robots = """
    User-agent: *
    Sitemap: https://example.com/sitemap.xml
    Sitemap: https://other.example/sitemap.xml
    """
    assert site_index.parse_robots_sitemaps(robots, "https://example.com/") == [
        "https://example.com/sitemap.xml"
    ]

    html = """
    <html><head>
      <link rel="alternate" type="application/rss+xml" href="/feed.xml">
      <link rel="stylesheet" href="/style.css">
    </head></html>
    """
    assert site_index.parse_html_feed_links(html, "https://example.com/") == [
        "https://example.com/feed.xml"
    ]


@pytest.mark.asyncio
async def test_discover_site_index_expands_robots_and_child_sitemap(monkeypatch):
    from trawler import site_index

    async def fake_fetch_text(client, url):
        if url == "https://example.com/robots.txt":
            return "Sitemap: https://example.com/sitemap-index.xml", url, ""
        if url == "https://example.com/":
            return '<link rel="alternate" type="application/rss+xml" href="/feed.xml">', url, ""
        if url == "https://example.com/sitemap-index.xml":
            return (
                "<sitemapindex><sitemap><loc>https://example.com/posts.xml</loc></sitemap></sitemapindex>",
                url,
                "",
            )
        if url == "https://example.com/posts.xml":
            return (
                "<urlset><url><loc>https://example.com/post</loc></url></urlset>",
                url,
                "",
            )
        if url == "https://example.com/feed.xml":
            return "<rss><channel><item><link>https://example.com/feed-post</link></item></channel></rss>", url, ""
        return "", url, "http-404"

    monkeypatch.setattr(site_index, "_fetch_text", fake_fetch_text)

    result = await site_index.discover_site_index("https://example.com/", max_urls=10)

    assert result["ok"] is True
    assert "https://example.com/sitemap-index.xml" in result["sitemap_urls"]
    assert "https://example.com/feed.xml" in result["feed_urls"]
    assert "https://example.com/post" in result["urls"]
    assert "https://example.com/feed-post" in result["urls"]


@pytest.mark.asyncio
async def test_discover_site_index_path_policy_does_not_filter_sitemap_documents(monkeypatch):
    from trawler import site_index

    async def fake_fetch_text(client, url):
        if url == "https://example.com/robots.txt":
            return "Sitemap: https://example.com/sitemap.xml", url, ""
        if url == "https://example.com/":
            return "", url, ""
        if url == "https://example.com/sitemap.xml":
            return (
                "<urlset>"
                "<url><loc>https://example.com/docs/a</loc></url>"
                "<url><loc>https://example.com/blog/b</loc></url>"
                "</urlset>",
                url,
                "",
            )
        return "", url, "http-404"

    monkeypatch.setattr(site_index, "_fetch_text", fake_fetch_text)

    result = await site_index.discover_site_index(
        "https://example.com/",
        max_urls=10,
        include_paths=["/docs/*"],
    )

    assert result["urls"] == ["https://example.com/docs/a"]
    assert "https://example.com/sitemap.xml" in result["sitemap_urls"]


@pytest.mark.asyncio
async def test_discover_site_index_robots_fetch_failure_fails_closed(monkeypatch):
    from trawler import config, site_index

    calls: list[str] = []

    async def fake_fetch_text(client, url):
        calls.append(url)
        if url == "https://example.com/robots.txt":
            return "", url, "http-503"
        return "should not fetch", url, ""

    monkeypatch.setattr(config, "ROBOTS_FAIL_CLOSED", True)
    monkeypatch.setattr(site_index, "_fetch_text", fake_fetch_text)

    result = await site_index.discover_site_index("https://example.com/", max_urls=10)

    assert result["ok"] is False
    assert result["error"].startswith("__TRAWLER_ERROR__:")
    assert calls == ["https://example.com/robots.txt"]


@pytest.mark.asyncio
async def test_fetch_text_blocks_redirect_to_internal_before_request(monkeypatch):
    from trawler import site_index

    requested: list[str] = []

    def handler(request):
        requested.append(str(request.url))
        if str(request.url) == "https://example.com/sitemap.xml":
            return httpx.Response(
                302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
            )
        return httpx.Response(200, text="should not be requested")

    async def fake_resolve(url, *args, **kwargs):
        return "169.254.169.254" in url, "93.184.216.34"

    async def fake_is_blocked(url, *args, **kwargs):
        return "169.254.169.254" in url

    monkeypatch.setattr(site_index.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(site_index.ssrf, "is_blocked_async", fake_is_blocked)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        text, final_url, error = await site_index._fetch_text(
            client,
            "https://example.com/sitemap.xml",
        )

    assert text == ""
    assert final_url == "http://169.254.169.254/latest/meta-data"
    assert error.startswith("__TRAWLER_ERROR__:")
    assert requested == ["https://example.com/sitemap.xml"]
