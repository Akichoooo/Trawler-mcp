import json

import pytest


@pytest.mark.asyncio
async def test_get_raw_supports_bounded_modes(tmp_db):
    from trawler import server
    from trawler.raw_store import save_raw

    save_raw(
        "raw-1",
        url="https://example.com/",
        final_url="https://example.com/",
        title="Example",
        markdown="# Intro\n\nHello\n\n## Details\n\nMore detail\n\n## Tail\n\nDone",
        gear_used="curl_cffi",
    )

    full = await server.get_raw("raw-1")
    assert full.startswith("__TRAWLER_OK__:")
    assert "gear_used: curl_cffi" in full

    no_frontmatter = await server.get_raw("raw-1", include_frontmatter=False)
    assert no_frontmatter.startswith("__TRAWLER_OK__:")
    assert "gear_used: curl_cffi" not in no_frontmatter
    assert "# Intro" in no_frontmatter

    toc = await server.get_raw("raw-1", mode="toc")
    assert "[Section 1]" in toc
    assert "Details" in toc

    section = await server.get_raw("raw-1", mode="section", section_id="Section 2")
    assert "More detail" in section
    assert "Done" not in section

    chunk = await server.get_raw("raw-1", mode="chunk", chunk_index=1)
    assert "# Intro" in chunk
    assert "More detail" in chunk


@pytest.mark.asyncio
async def test_get_raw_section_missing_and_path_safety(tmp_db, tmp_path):
    from trawler import server
    from trawler.raw_store import save_raw

    save_raw(
        "raw-2",
        url="https://example.com/",
        final_url="https://example.com/",
        title="Example",
        markdown="# Intro\n\nHello",
        gear_used="curl_cffi",
    )

    missing = await server.get_raw("raw-2", mode="section", section_id="Section 99")
    assert missing.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(missing[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "section-not-found"

    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    denied = await server.get_raw(str(outside))
    assert denied.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(denied[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "permission-denied"
