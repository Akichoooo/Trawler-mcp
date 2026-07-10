from trawler.crawl_policy import CrawlPolicy


def test_crawl_policy_normalizes_and_filters_page_urls():
    policy = CrawlPolicy.from_options(
        "https://example.com/?utm_source=x",
        include_paths=["/docs/*"],
        exclude_paths=["/docs/private/*"],
        ignore_query_parameters=True,
    )

    assert policy.start_url == "https://example.com/"
    assert policy.normalize_page_url("https://example.com/docs/a?version=1") == "https://example.com/docs/a"
    assert policy.normalize_page_url("https://example.com/blog/a") == ""
    assert policy.normalize_page_url("https://example.com/docs/private/a") == ""
    assert policy.normalize_page_url("https://other.test/docs/a") == ""


def test_crawl_policy_allows_subdomains_when_configured():
    strict = CrawlPolicy.from_options("https://example.com/")
    relaxed = CrawlPolicy.from_options("https://example.com/", include_subdomains=True)

    assert strict.normalize_page_url("https://docs.example.com/a") == ""
    assert relaxed.normalize_page_url("https://docs.example.com/a") == "https://docs.example.com/a"


def test_crawl_policy_index_urls_skip_path_filters():
    policy = CrawlPolicy.from_options(
        "https://example.com/",
        include_paths=["/docs/*"],
    )

    assert policy.normalize_index_url("https://example.com/sitemap.xml") == "https://example.com/sitemap.xml"
    assert policy.normalize_page_url("https://example.com/sitemap.xml") == ""


def test_crawl_policy_depth_and_payload():
    policy = CrawlPolicy.from_options(
        "https://example.com/",
        max_depth=1,
        include_paths=["/docs/*"],
        ignore_query_parameters=True,
    )

    assert policy.should_expand_depth(0) is True
    assert policy.should_expand_depth(1) is False
    assert policy.payload()["max_depth"] == 1
    assert policy.payload()["include_paths"] == ["/docs/*"]
