def test_proxy_pool_selects_sticky_proxy(monkeypatch):
    from trawler import config, proxy_pool

    monkeypatch.setattr(config, "PROXY_POOL", "http://p1, http://p2, http://p3")
    monkeypatch.setattr(config, "HTTP_PROXY", "")
    monkeypatch.setattr(config, "HTTPS_PROXY", "")

    first = proxy_pool.select_proxy(True, domain="example.com", account_id="acct")
    second = proxy_pool.select_proxy(True, domain="example.com", account_id="acct")
    other = proxy_pool.select_proxy(True, domain="example.org", account_id="acct2")

    assert first == second
    assert first in {"http://p1", "http://p2", "http://p3"}
    assert other in {"http://p1", "http://p2", "http://p3"}


def test_proxy_pool_falls_back_to_env_proxy(monkeypatch):
    from trawler import config, proxy_pool

    monkeypatch.setattr(config, "PROXY_POOL", "")
    monkeypatch.setattr(config, "HTTPS_PROXY", "http://https-proxy")
    monkeypatch.setattr(config, "HTTP_PROXY", "http://http-proxy")

    assert proxy_pool.select_proxy(True, domain="example.com") == "http://https-proxy"
    assert proxy_pool.select_proxy(False, domain="example.com") == ""
