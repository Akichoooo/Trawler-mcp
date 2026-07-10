"""测试集 #1 — 断网超时。

mock 网络, crawl_url 必须在墙钟 (≤35s) 内返回 __TRAWLER_ERROR__:{json} 字符串,
绝不抛 Python stacktrace 到 stdout。
"""
import asyncio
import json
import time
from unittest.mock import patch

import pytest

from trawler import config


@pytest.mark.asyncio
async def test_crawl_timeout_returns_string(tmp_db):
    """crawl_url 超时返回 __TRAWLER_ERROR__ 字符串, 不抛异常。"""
    from trawler import crawl_url

    # mock patchright fetch 永远 hang (模拟断网/超时)
    async def hang(*a, **kw):
        await asyncio.sleep(100)
        return None

    with patch("trawler.fetcher.patchright_rung.fetch", new=hang):
        with patch("trawler.fetcher.jina_rung.fetch", new=hang):
            with patch("trawler.fetcher.curlcffi_rung.fetch", new=hang):
                # 把墙钟调小加速测试
                with patch.object(config, "CRAWL_TIMEOUT", 3):
                    start = time.time()
                    result = await crawl_url.crawl_url("https://example.com/")
                    elapsed = time.time() - start
    # 必须返回字符串
    assert isinstance(result, str)
    # 返回 __TRAWLER_ERROR__:{json} 错误串
    assert result.startswith("__TRAWLER_ERROR__")
    # 在墙钟内
    assert elapsed < 10, f"took {elapsed}s, expected <10s"


@pytest.mark.asyncio
async def test_crawl_no_stacktrace_on_failure(tmp_db, capsys):
    """失败时不泄 Python stacktrace 到 stdout (会毁 MCP JSON-RPC)。"""
    from trawler import crawl_url, fetcher

    # mock 所有 fetcher 抛异常 (确保走 __TRAWLER_ERROR__ 路径)
    async def boom(*a, **kw):
        raise RuntimeError("simulated internal error")

    # patch 真正被 crawl_url 调用的引用 (含 curl_cffi rung0)
    with patch.object(fetcher.patchright_rung, "fetch", boom):
        with patch.object(fetcher.jina_rung, "fetch", boom):
            with patch.object(fetcher.hitl_rung, "fetch", boom):
                with patch.object(fetcher.curlcffi_rung, "fetch", boom):
                    result = await crawl_url.crawl_url("https://example.com/")

    assert isinstance(result, str)
    # 返回 __TRAWLER_ERROR__:{json} 错误串
    assert result.startswith("__TRAWLER_ERROR__")
    # stdout 不含 Python traceback
    captured = capsys.readouterr()
    assert "Traceback (most recent call last)" not in captured.out


@pytest.mark.asyncio
async def test_invalid_url_returns_failed(tmp_db):
    """无效 URL 立即返回 __TRAWLER_ERROR__。"""
    from trawler import crawl_url
    result = await crawl_url.crawl_url("")
    # 返回 __TRAWLER_ERROR__:{json} 错误串
    assert result.startswith("__TRAWLER_ERROR__")


@pytest.mark.asyncio
async def test_invalid_mode_returns_failed(tmp_db):
    from trawler import crawl_url

    result = await crawl_url.crawl_url("https://example.com/", mode="unknown")

    assert result.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(result[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "invalid-mode"


@pytest.mark.asyncio
async def test_invalid_cache_mode_returns_failed(tmp_db):
    from trawler import crawl_url

    result = await crawl_url.crawl_url("https://example.com/", cache_mode="mystery")

    assert result.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(result[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "invalid-mode"


@pytest.mark.asyncio
async def test_cache_read_only_miss_does_not_fetch(tmp_db, monkeypatch):
    from trawler import config
    from trawler import crawl_url as crawl_url_mod

    async def fake_resolve(*args, **kwargs):
        return False, "93.184.216.34"

    async def fail_fetch(*args, **kwargs):
        raise AssertionError("fetch ladder should not be called in read_only cache miss")

    monkeypatch.setattr(config, "RESPECT_ROBOTS", False)
    monkeypatch.setattr(crawl_url_mod.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(crawl_url_mod, "_fetch_ladder", fail_fetch)

    result = await crawl_url_mod.crawl_url(
        "https://example.com/cache-miss",
        cache_mode="read_only",
    )

    assert result.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(result[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "cache-miss"
