import pytest


def test_cdp_endpoint_defaults_to_localhost(monkeypatch):
    from trawler import browser_adapter, config

    monkeypatch.setattr(config, "ALLOW_REMOTE_CDP", False)

    assert browser_adapter.is_allowed_cdp_endpoint("http://127.0.0.1:9222")
    assert browser_adapter.is_allowed_cdp_endpoint("ws://localhost:9222/devtools/browser/1")
    assert not browser_adapter.is_allowed_cdp_endpoint("http://192.0.2.10:9222")
    assert not browser_adapter.is_allowed_cdp_endpoint("file:///tmp/browser")


def test_cdp_endpoint_allows_remote_only_by_opt_in(monkeypatch):
    from trawler import browser_adapter, config

    monkeypatch.setattr(config, "ALLOW_REMOTE_CDP", True)

    assert browser_adapter.is_allowed_cdp_endpoint("http://192.0.2.10:9222")


@pytest.mark.asyncio
async def test_route_guard_reports_unavailable_context():
    from trawler import browser_adapter

    class NoRouteContext:
        async def route(self, pattern, handler):
            raise RuntimeError("route unavailable")

    assert await browser_adapter.install_ssrf_route_guard(NoRouteContext()) is False
