from trawler import link_map


def test_extract_links_from_dom_normalizes_and_filters():
    html = """
    <html><body>
      <a href="/docs/?utm_source=x">Docs</a>
      <a href="https://example.com/docs">Duplicate</a>
      <a href="https://other.example/path">Other</a>
      <a href="mailto:test@example.com">Mail</a>
      <a href="javascript:alert(1)">JS</a>
    </body></html>
    """

    links = link_map.extract_links(
        html,
        "https://example.com/start",
        same_domain_only=True,
    )

    assert [item["url"] for item in links] == ["https://example.com/docs"]
    assert links[0]["text"] == "Docs"
    assert links[0]["same_domain"] is True
