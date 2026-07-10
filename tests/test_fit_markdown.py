from trawler.parser.fit_markdown import extract_citations, fit_markdown


def test_fit_markdown_compacts_and_extracts_citations():
    markdown = """
    # Title


    Read [Docs](/docs) and [External](https://example.org/a).

    Read [Docs again](/docs).
    """ + "\n\nParagraph text. " * 200

    fitted = fit_markdown(markdown, max_chars=300, base_url="https://example.com/start")

    assert fitted.output_chars <= 300
    assert fitted.truncated is True
    assert fitted.markdown.count("\n\n\n") == 0
    assert fitted.citations == [
        {"label": "Docs", "url": "https://example.com/docs"},
        {"label": "External", "url": "https://example.org/a"},
    ]


def test_extract_citations_ignores_non_http_links():
    citations = extract_citations(
        "[mail](mailto:a@example.com) [hash](#top) [ok](https://example.com/path)",
    )

    assert citations == [{"label": "ok", "url": "https://example.com/path"}]
