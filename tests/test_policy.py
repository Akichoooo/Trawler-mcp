def test_policy_allows_default_public_retrieve(monkeypatch):
    from trawler import config, policy

    monkeypatch.setattr(config, "POLICY_MODE", "permissive")
    monkeypatch.setattr(config, "ALLOWED_DOMAINS", "")
    monkeypatch.setattr(config, "BLOCKED_DOMAINS", "")

    decision = policy.decide("retrieve_page", target_url="https://example.com/")

    assert decision.allowed is True
    assert decision.risk == "medium"
    assert decision.restrictions["target_domain"] == "example.com"


def test_policy_blocks_domain(monkeypatch):
    from trawler import config, policy

    monkeypatch.setattr(config, "BLOCKED_DOMAINS", "example.com")
    monkeypatch.setattr(config, "ALLOWED_DOMAINS", "")

    decision = policy.decide("retrieve_page", target_url="https://sub.example.com/private")

    assert decision.allowed is False
    assert "blocked_domain" in decision.reasons


def test_policy_strict_requires_allowed_domain(monkeypatch):
    from trawler import config, policy

    monkeypatch.setattr(config, "POLICY_MODE", "strict")
    monkeypatch.setattr(config, "ALLOWED_DOMAINS", "allowed.example")
    monkeypatch.setattr(config, "BLOCKED_DOMAINS", "")

    denied = policy.decide("crawl_url", target_url="https://example.com/")
    allowed = policy.decide("crawl_url", target_url="https://docs.allowed.example/")

    assert denied.allowed is False
    assert "domain_not_allowed" in denied.reasons
    assert allowed.allowed is True


def test_policy_disables_live_browser_and_cdp(monkeypatch):
    from trawler import config, policy

    monkeypatch.setattr(config, "ENABLE_LIVE_BROWSER", False)
    monkeypatch.setattr(config, "ENABLE_CDP", False)

    browser = policy.decide("open_browser_session", uses_live_browser=True)
    cdp = policy.decide("connect_browser_session", uses_live_browser=True, uses_cdp=True)

    assert browser.allowed is False
    assert "live_browser_disabled" in browser.reasons
    assert cdp.allowed is False
    assert "cdp_disabled" in cdp.reasons


def test_policy_disables_crawl_site_and_artifact_body(monkeypatch):
    from trawler import config, policy

    monkeypatch.setattr(config, "ENABLE_CRAWL_SITE", False)
    monkeypatch.setattr(config, "EXPOSE_ARTIFACT_BODIES", False)

    crawl = policy.decide("crawl_site", target_url="https://example.com/")
    artifact = policy.decide("get_artifact", reads_artifact_body=True)

    assert crawl.allowed is False
    assert "crawl_site_disabled" in crawl.reasons
    assert artifact.allowed is False
    assert "artifact_body_disabled" in artifact.reasons
