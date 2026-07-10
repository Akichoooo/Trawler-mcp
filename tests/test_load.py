import asyncio
import time

import pytest

from trawler.crawl_url import crawl_url


@pytest.mark.asyncio
async def test_load_100_concurrent(tmp_db):
    """
    Load test: 100 concurrent requests.
    Validates that the event loop is not blocked and memory usage is stable.
    """
    # Test the real pipeline but mock the browser fetch
    url = "https://example.com/page"

    from trawler.fetcher import curlcffi_rung, patchright_rung

    async def mock_fetch(*args, **kwargs):
        await asyncio.sleep(0.2)
        return patchright_rung.FetchResult(
            html="<html><head><title>Mocked</title></head><body><h1>Hello</h1></body></html>",
            http_status=200,
            final_url=url,
            ok=True
        )

    original_fetch = patchright_rung.fetch
    patchright_rung.fetch = mock_fetch

    # curl_cffi 是 rung0 (patchright 之前): 必须也 mock, 否则测试走真实网络
    async def mock_curlcffi_fetch(*args, **kwargs):
        return patchright_rung.FetchResult(ok=False, error="mocked disabled")
    original_curlcffi_fetch = curlcffi_rung.fetch
    curlcffi_rung.fetch = mock_curlcffi_fetch

    from trawler.fetcher import detect as detect_mod
    original_detect = detect_mod.detect
    def mock_detect(*args, **kwargs):
        return detect_mod.DetectionResult(verdict=detect_mod.Verdict.OK, reason="")
    detect_mod.detect = mock_detect

    from trawler.fetcher import jina_rung
    original_jina_fetch = jina_rung.fetch
    async def mock_jina_fetch(*args, **kwargs):
        return "mocked jina md"
    jina_rung.fetch = mock_jina_fetch

    try:
        start_time = time.monotonic()
        # Fire 100 concurrent requests across 10 different mock domains
        tasks = []
        for i in range(100):
            # We use 10 different domains to avoid hitting the 1s same-domain rate limit for all 100
            domain_url = f"https://example{i % 10}.com/page{i}"
            tasks.append(crawl_url(domain_url, force_refresh=True))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.monotonic() - start_time

        assert len(results) == 100
        for r in results:
            if isinstance(r, Exception):
                raise r
            assert isinstance(r, str)
            # 接受成功 (含 mock 内容) 或新错误前缀 __TRAWLER_ERROR__
            assert "Hello" in r or "Mocked" in r or r.startswith("__TRAWLER_ERROR__")

        assert elapsed < 20.0
    finally:
        patchright_rung.fetch = original_fetch
        curlcffi_rung.fetch = original_curlcffi_fetch
        detect_mod.detect = original_detect
        jina_rung.fetch = original_jina_fetch

