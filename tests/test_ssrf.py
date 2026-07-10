"""测试集 #2 — SSRF 拦截。

crawl_url("http://127.0.0.1:8080") 必须立即被拦, 返回 __TRAWLER_ERROR__:{json} (blocked-ssrf)。
"""

import json

import pytest

from trawler import ssrf


def test_ssrf_blocks_loopback_ipv4():
    assert ssrf.is_blocked("http://127.0.0.1:8080/") is True
    assert ssrf.is_blocked("http://localhost:8080/") is True


def test_ssrf_blocks_private_10():
    assert ssrf.is_blocked("http://10.0.0.1/") is True


def test_ssrf_blocks_private_192168():
    assert ssrf.is_blocked("http://192.168.1.1/") is True


def test_ssrf_blocks_cloud_metadata():
    """169.254.169.254 = 云厂商元数据 (AWS/GCP)。必拦。"""
    assert ssrf.is_blocked("http://169.254.169.254/latest/meta-data/") is True


def test_ssrf_blocks_ipv6_loopback():
    assert ssrf.is_blocked("http://[::1]:8080/") is True


def test_ssrf_blocks_non_global_ranges():
    assert ssrf.is_blocked("http://192.0.2.10/") is True
    assert ssrf.is_blocked("http://224.0.0.1/") is True
    assert ssrf.is_blocked("http://0.0.0.0/") is True


def test_ssrf_allows_public():
    assert ssrf.is_blocked("https://93.184.216.34/") is False


def test_ssrf_fake_ip_dns_requires_explicit_opt_in(monkeypatch):
    import socket

    from trawler import config

    def fake_getaddrinfo(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.10", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(config, "SSRF_FAKE_IP_CIDRS", "198.18.0.0/15")

    monkeypatch.setattr(config, "SSRF_ALLOW_FAKE_IP_DNS", False)
    assert ssrf.resolve_and_check("https://fake-ip.example/") == (True, None)

    monkeypatch.setattr(config, "SSRF_ALLOW_FAKE_IP_DNS", True)
    assert ssrf.resolve_and_check("https://fake-ip.example/") == (False, None)
    assert ssrf.is_blocked("https://198.18.0.10/") is True


def test_ssrf_optin_allows_local(monkeypatch):
    """TRAWLER_ALLOW_LOCAL=1 时放行内网。"""
    from trawler import config
    monkeypatch.setattr(config, "ALLOW_LOCAL", True)
    assert ssrf.is_blocked("http://127.0.0.1:8080/") is False


def test_block_reason_message():
    msg = ssrf.block_reason("http://127.0.0.1/")
    assert "Blocked non-public IP" in msg
    assert "TRAWLER_ALLOW_LOCAL" in msg


def test_block_reason_suggests_fake_ip_dns_opt_in(monkeypatch):
    import socket

    def fake_getaddrinfo(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.10", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    msg = ssrf.block_reason("https://fake-ip.example/")

    assert "TRAWLER_SSRF_ALLOW_FAKE_IP_DNS" in msg
    assert "TRAWLER_ALLOW_LOCAL" not in msg


@pytest.mark.asyncio
async def test_curlcffi_redirect_ssrf_returns_special_marker(monkeypatch):
    from trawler.crawl_url import _try_curlcffi
    from trawler.fetcher import curlcffi_rung
    from trawler.fetcher.patchright_rung import FetchResult

    async def fake_fetch(*args, **kwargs):
        return FetchResult(ok=False, error="SSRF blocked: redirect to http://127.0.0.1/")

    monkeypatch.setattr(curlcffi_rung, "CURLCFFI_AVAILABLE", True)
    monkeypatch.setattr(curlcffi_rung, "fetch", fake_fetch)

    result = await _try_curlcffi("https://example.com/", bypass_l3=False, use_proxy=False)

    assert isinstance(result, str)
    assert result.startswith("__SSRF_BLOCKED__:")


@pytest.mark.asyncio
async def test_curlcffi_proxy_ssrf_returns_special_marker(monkeypatch):
    from trawler.crawl_url import _try_curlcffi
    from trawler.fetcher import curlcffi_rung
    from trawler.fetcher.patchright_rung import FetchResult

    async def fake_fetch(*args, **kwargs):
        return FetchResult(
            ok=False,
            error="SSRF blocked: unresolved proxy target https://example.com/",
        )

    monkeypatch.setattr(curlcffi_rung, "CURLCFFI_AVAILABLE", True)
    monkeypatch.setattr(curlcffi_rung, "fetch", fake_fetch)

    result = await _try_curlcffi("https://example.com/", bypass_l3=False, use_proxy=True)

    assert isinstance(result, str)
    assert result.startswith("__SSRF_BLOCKED__:")


@pytest.mark.asyncio
async def test_crawl_url_redirect_ssrf_error_contract(tmp_db, monkeypatch):
    from trawler import config
    from trawler import crawl_url as crawl_url_mod

    async def fake_resolve(*args, **kwargs):
        return False, "93.184.216.34"

    async def fake_ladder(*args, **kwargs):
        return "blocked", 0, "", "__SSRF_BLOCKED__"

    monkeypatch.setattr(config, "RESPECT_ROBOTS", False)
    monkeypatch.setattr(crawl_url_mod.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(crawl_url_mod, "_fetch_ladder", fake_ladder)

    result = await crawl_url_mod.crawl_url("https://example.com/", force_refresh=True)

    assert result.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(result[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "blocked-ssrf-redirect"


@pytest.mark.asyncio
async def test_curlcffi_redirect_rebuilds_dns_pin_for_each_hop(monkeypatch):
    from trawler.fetcher import curlcffi_rung

    class FakeCookies:
        def set(self, *args, **kwargs):
            return None

    class FakeResponse:
        def __init__(self, status_code, url, headers=None, text=""):
            self.status_code = status_code
            self.url = url
            self.headers = headers or {}
            self.text = text

    class FakeSession:
        def __init__(self):
            self.cookies = FakeCookies()
            self.calls = []

        async def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if len(self.calls) == 1:
                return FakeResponse(
                    302,
                    url,
                    headers={"Location": "https://next.example/final"},
                )
            return FakeResponse(200, url, text="<html><body>ok</body></html>")

    fake_session = FakeSession()
    resolved = []

    async def fake_get_session(*args, **kwargs):
        return fake_session

    async def fake_resolve(url, *args, **kwargs):
        resolved.append(url)
        return False, "203.0.113.10"

    monkeypatch.setattr(curlcffi_rung, "CURLCFFI_AVAILABLE", True)
    monkeypatch.setattr(curlcffi_rung, "_get_session", fake_get_session)

    # Import path used inside fetch().
    import trawler.ssrf as ssrf_mod

    monkeypatch.setattr(ssrf_mod, "resolve_and_check_async", fake_resolve)

    result = await curlcffi_rung.fetch(
        "https://example.com/start",
        safe_ip="93.184.216.34",
    )

    assert result.ok is True
    assert resolved == ["https://next.example/final"]
    assert fake_session.calls[0][1]["resolve"] == ["example.com:443:93.184.216.34"]
    assert fake_session.calls[1][1]["resolve"] == ["next.example:443:203.0.113.10"]


@pytest.mark.asyncio
async def test_browser_route_check_does_not_cache_safe_dns(monkeypatch):
    from trawler import ssrf as ssrf_mod
    from trawler.fetcher import patchright_rung

    calls = []

    async def fake_resolve(url, *args, **kwargs):
        calls.append(url)
        return (len(calls) > 1), None

    patchright_rung._DNS_CACHE.clear()
    ssrf_mod._DNS_CACHE.clear()
    monkeypatch.setattr(ssrf_mod, "resolve_and_check_async", fake_resolve)

    first = await patchright_rung._check_hostname_blocked("https://rebind.example/a")
    second = await patchright_rung._check_hostname_blocked("https://rebind.example/b")

    assert first is False
    assert second is True
    assert calls == ["https://rebind.example/a", "https://rebind.example/b"]
