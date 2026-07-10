import pytest


@pytest.mark.asyncio
async def test_curlcffi_session_pool_is_session_scoped(monkeypatch):
    from trawler.fetcher import curlcffi_rung

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    curlcffi_rung._session_pool.clear()
    monkeypatch.setattr(curlcffi_rung, "CURLCFFI_AVAILABLE", True)
    monkeypatch.setattr(curlcffi_rung, "AsyncSession", FakeSession)

    first = await curlcffi_rung._get_session("chrome131", None, "session-a")
    second = await curlcffi_rung._get_session("chrome131", None, "session-b")
    again = await curlcffi_rung._get_session("chrome131", None, "session-a")

    assert first is again
    assert first is not second
