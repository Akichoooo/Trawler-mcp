import json

import pytest

from trawler.errors import format_error, format_ok


def test_crawl_result_payload_includes_metadata(tmp_db):
    from trawler import structured
    from trawler.raw_store import save_raw
    from trawler.seen import url_id

    url = "https://example.com/docs?utm_source=noise"
    canonical = "https://example.com/docs"
    raw_id = url_id(canonical)
    save_raw(
        raw_id,
        url=canonical,
        final_url=canonical,
        title="Docs",
        markdown="# Docs\n\nHello",
        gear_used="curl_cffi",
        metadata={
            "artifact_id": "art-123",
            "links": [{"url": "https://example.com/next", "text": "Next"}],
            "link_count": 1,
        },
    )

    payload = structured.crawl_result_payload(
        input_url=url,
        result=format_ok("# Docs\n\nHello"),
        cache_mode="enabled",
        force_refresh=True,
        mode="full",
    )

    assert payload.ok is True
    assert payload.text == "# Docs\n\nHello"
    assert payload.raw_id == raw_id
    assert payload.canonical_url == canonical
    assert payload.cache_mode == "write_only"
    assert payload.artifact_id == "art-123"
    assert payload.link_count == 1
    assert payload.links[0]["url"] == "https://example.com/next"


def test_crawl_result_payload_parses_errors():
    from trawler import structured

    payload = structured.crawl_result_payload(
        input_url="https://example.com/",
        result=format_error("empty-content", "No text", artifact_id="art-err"),
        cache_mode="enabled",
        force_refresh=False,
        mode="full",
    )

    assert payload.ok is False
    assert payload.error is not None
    assert payload.error["errorType"] == "empty-content"
    assert payload.artifact_id == "art-err"
    assert payload.text == ""


def test_crawl_result_payload_reads_blocked_metadata(tmp_db):
    from trawler import structured
    from trawler.raw_store import save_blocked
    from trawler.seen import url_id

    url = "https://example.com/blocked"
    save_blocked(
        url_id(url),
        url=url,
        reason="parsers extracted no text",
        metadata={"artifact_id": "art-blocked"},
    )

    payload = structured.crawl_result_payload(
        input_url=url,
        result=format_error("empty-content", "No text", artifact_id="art-blocked"),
        cache_mode="enabled",
        force_refresh=False,
        mode="full",
    )

    assert payload.ok is False
    assert payload.metadata["status"] == "blocked"
    assert payload.metadata["reason"] == "parsers extracted no text"
    assert payload.metadata["artifact_id"] == "art-blocked"


def test_crawl_result_payload_error_prefers_blocked_metadata_over_stale_raw(tmp_db):
    from trawler import structured
    from trawler.raw_store import save_blocked, save_raw
    from trawler.seen import url_id

    url = "https://example.com/stale"
    raw_id = url_id(url)
    save_raw(
        raw_id,
        url=url,
        final_url=url,
        title="Old OK",
        markdown="# Old OK",
        gear_used="curl_cffi",
    )
    save_blocked(
        raw_id,
        url=url,
        reason="blocked now",
        metadata={"artifact_id": "art-new"},
    )

    payload = structured.crawl_result_payload(
        input_url=url,
        result=format_error("empty-content", "No text", artifact_id="art-new"),
        cache_mode="enabled",
        force_refresh=False,
        mode="full",
    )

    assert payload.ok is False
    assert payload.metadata["status"] == "blocked"
    assert payload.metadata["reason"] == "blocked now"
    assert payload.metadata["artifact_id"] == "art-new"


def test_map_result_call_tool_result_dual_tracks():
    from trawler import structured

    map_payload = {
        "ok": True,
        "url": "https://example.com/",
        "link_count": 1,
        "links": [{"url": "https://example.com/a"}],
    }
    legacy = format_ok(json.dumps(map_payload))

    result = structured.map_result_to_call_result(
        start_url="https://example.com/",
        legacy_text=legacy,
        result=map_payload,
    )

    assert result.content[0].text == legacy
    assert result.structuredContent is not None
    assert result.structuredContent["ok"] is True
    assert result.structuredContent["link_count"] == 1


@pytest.mark.asyncio
async def test_crawl_url_structured_tool_wraps_legacy_result(monkeypatch):
    from trawler import server

    async def fake_crawl_url(*args, **kwargs):
        return format_ok("hello")

    monkeypatch.setattr(server, "_crawl_url", fake_crawl_url)

    result = await server.crawl_url_structured("https://example.com/")

    assert result.content[0].text == format_ok("hello")
    assert result.structuredContent is not None
    assert result.structuredContent["ok"] is True
    assert result.structuredContent["text"] == "hello"
