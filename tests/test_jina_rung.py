import pytest

from trawler.fetcher import jina_rung


@pytest.mark.asyncio
async def test_jina_fetch_skipped():
    # Should skip if needs_account=True
    res = await jina_rung.fetch("https://example.com", needs_account=True, use_proxy=False)
    assert res == ""

    # Should skip if not public
    res = await jina_rung.fetch("http://localhost", needs_account=False, use_proxy=False)
    assert res == ""

    res = await jina_rung.fetch("http://127.0.0.1", needs_account=False, use_proxy=False)
    assert res == ""

    res = await jina_rung.fetch("http://169.254.169.254/latest", needs_account=False, use_proxy=False)
    assert res == ""

    res = await jina_rung.fetch("http://intranet", needs_account=False, use_proxy=False)
    assert res == ""

@pytest.mark.asyncio
async def test_jina_fetch_mocked(monkeypatch):
    class MockResponse:
        def __init__(self, text):
            self.text = text
        def raise_for_status(self):
            pass

    class MockAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        async def get(self, url, headers=None):
            return MockResponse("mocked jina response")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)

    res = await jina_rung.fetch("https://example.com", needs_account=False, use_proxy=False)
    assert res == "mocked jina response"
