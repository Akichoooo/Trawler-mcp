from pathlib import Path

from trawler.parser import extract

FIXTURES = Path(__file__).parent / "fixtures" / "pages"


def _render(name: str) -> str:
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return extract.extract(html, f"https://example.com/{name}")


def test_table_fixture_preserves_cells():
    md = _render("spec_table.html")

    assert "# Specs" in md
    assert "Latency" in md
    assert "35ms" in md
    assert "|" in md


def test_code_fixture_preserves_command():
    md = _render("code_doc.html")

    assert "# Install" in md
    assert "uv run trawler" in md


def test_docs_fixture_preserves_link_and_drops_nav():
    md = _render("docs_link.html")

    assert "# Guide" in md
    assert "Use crawl_url for a page." in md
    assert "[API](/api)" in md
    assert "Changelog" not in md
