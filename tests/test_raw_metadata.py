import json

import pytest


@pytest.mark.asyncio
async def test_get_raw_metadata_by_raw_id_and_url(tmp_db):
    from trawler import server
    from trawler.raw_store import save_raw
    from trawler.seen import url_id

    url = "https://example.com/docs"
    rid = url_id(url)
    save_raw(
        rid,
        url=url,
        final_url=url,
        title="Docs",
        markdown="# Docs",
        gear_used="curl_cffi",
        metadata={"char_count": 6, "link_count": 0},
    )

    by_id = await server.get_raw_metadata(rid)
    assert by_id.startswith("__TRAWLER_OK__:")
    payload = json.loads(by_id[len("__TRAWLER_OK__:\n\n"):])
    assert payload["raw_id"] == rid
    assert payload["metadata"]["title"] == "Docs"
    assert payload["metadata"]["char_count"] == 6

    by_url = await server.get_raw_metadata(url + "?utm_source=x")
    payload = json.loads(by_url[len("__TRAWLER_OK__:\n\n"):])
    assert payload["raw_id"] == rid


@pytest.mark.asyncio
async def test_get_raw_metadata_reads_blocked_and_rejects_bad_identifier(tmp_db):
    from trawler import server
    from trawler.raw_store import save_blocked

    save_blocked("blocked-1", url="https://example.com/", reason="blocked")

    blocked = await server.get_raw_metadata("blocked-1")
    payload = json.loads(blocked[len("__TRAWLER_OK__:\n\n"):])
    assert payload["metadata"]["status"] == "blocked"
    assert payload["metadata"]["reason"] == "blocked"

    bad = await server.get_raw_metadata("../secret")
    assert bad.startswith("__TRAWLER_ERROR__:")
    error = json.loads(bad[len("__TRAWLER_ERROR__:"):])
    assert error["errorType"] == "invalid-url"


def test_raw_store_rejects_unsafe_raw_ids(tmp_path, monkeypatch):
    from trawler import config, raw_store

    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")

    assert raw_store.raw_path("abc-123_DEF").name == "abc-123_DEF.md"
    with pytest.raises(ValueError):
        raw_store.raw_path("../secret")
    with pytest.raises(ValueError):
        raw_store.blocked_path("bad/name")


def test_raw_metadata_does_not_follow_symlink_outside_raw_dir(tmp_path, monkeypatch):
    import os

    from trawler import config, raw_store

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntitle: Secret\n---\n\nsecret", encoding="utf-8")
    monkeypatch.setattr(config, "RAW_DIR", raw_dir)
    try:
        os.symlink(outside, raw_dir / "safe.md")
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"symlink unavailable: {e}")

    assert raw_store.read_metadata("safe") == {}
