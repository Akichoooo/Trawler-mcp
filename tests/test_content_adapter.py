from trawler.parser import content_adapter, extract


def test_json_response_adapts_to_markdown():
    md = content_adapter.adapt_text_response('{"name":"Trawler","items":[1,2]}')

    assert md.startswith("# JSON response")
    assert "```json" in md
    assert '"name": "Trawler"' in md


def test_feed_response_adapts_to_links():
    rss = """
    <rss><channel>
      <item><link>https://example.com/a</link></item>
      <item><link>https://example.com/b</link></item>
    </channel></rss>
    """

    md = content_adapter.adapt_text_response(rss, "https://example.com/feed.xml")

    assert md.startswith("# RSS feed")
    assert "- https://example.com/a" in md
    assert "- https://example.com/b" in md


def test_plain_text_response_adapts_without_html_pipeline():
    text = "plain text response\nsecond line"

    assert content_adapter.adapt_text_response(text) == text
    assert extract.extract(text) == text


def test_html_response_uses_existing_pipeline():
    html = "<html><body><article><h1>Hello</h1><p>World</p></article></body></html>"

    assert content_adapter.adapt_text_response(html) == ""
