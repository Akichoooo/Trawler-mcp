import json

import pytest


class _DummyTask:
    def cancel(self):
        return None


@pytest.mark.asyncio
async def test_crawl_site_indexed_seeds_frontier(tmp_db, monkeypatch):
    from trawler import db, frontier, server

    async def fake_discover(*args, **kwargs):
        return {
            "ok": True,
            "urls": [
                "https://example.com/docs?version=1",
                "https://example.com/docs?version=2",
                "https://example.com/docs/private/secret",
                "https://example.com/blog",
                "https://other.example/out",
            ],
            "url_count": 5,
            "sitemap_count": 1,
            "feed_count": 0,
        }

    def fake_create_task(coro):
        coro.close()
        return _DummyTask()

    import trawler.crawl_site as crawl_site_mod

    monkeypatch.setattr(server, "_discover_site_index", fake_discover)
    monkeypatch.setattr(crawl_site_mod.asyncio, "create_task", fake_create_task)

    result = await server.crawl_site_indexed(
        "https://example.com/",
        max_pages=5,
        same_domain_only=True,
        include_paths=["/docs*"],
        exclude_paths=["/docs/private/*"],
        ignore_query_parameters=True,
    )

    assert result.startswith("__TRAWLER_OK__:")
    payload = json.loads(result[len("__TRAWLER_OK__:\n\n"):])
    assert payload["seed_count"] == 1
    assert payload["discovered_url_count"] == 5

    conn = db.connect()
    try:
        queued = frontier.queued_urls(conn, payload["job_id"], limit=10)
    finally:
        conn.close()

    assert queued == [
        "https://example.com/",
        "https://example.com/docs",
    ]
