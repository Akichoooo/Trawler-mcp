"""test_agent_features.py — 测试为 AI Agent 增加的 TOC/Section 切片、Cookie 回流及策略自适应功能。
"""

import json

import pytest

from trawler import account_vault, db, rules, site_rules
from trawler.parser import chunker


def test_chunker_generate_toc_and_slice():
    sample_md = """# Title 1: Introduction
Here is intro text.

## Subsection 1.1: Architecture
Architecture details go here.

## Subsection 1.2: Implementation
Implementation details go here.
"""
    toc = chunker.generate_toc(sample_md)
    assert "📍 页面目录索引" in toc
    assert "[Section 1]" in toc
    assert "Title 1: Introduction" in toc
    assert "[Section 2]" in toc
    assert "Subsection 1.1: Architecture" in toc

    # Slice Section 2
    sec2 = chunker.slice_by_section(sample_md, "Section 2")
    assert "Architecture" in sec2
    assert "Architecture details go here." in sec2
    assert "Implementation details go here." not in sec2

    # Slice by tokens
    chunk = chunker.slice_by_tokens(sample_md, chunk_index=1, chunk_size=50)
    assert "Chunk 1" in chunk


def test_chunker_missing_section_returns_json_error():
    sample_md = "# Intro\n\nBody\n\n## Details\n\nMore"
    result = chunker.slice_by_section(sample_md, "Section 99")
    assert result.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(result[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "section-not-found"


def test_chunker_out_of_range_and_last_chunk_metadata():
    sample_md = "a" * 9000

    missing = chunker.slice_by_tokens(sample_md, chunk_index=99, chunk_size=4000)
    assert missing.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(missing[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "chunk-not-found"

    last = chunker.slice_by_tokens(sample_md, chunk_index=3, chunk_size=4000)
    assert "has_next=false" in last
    assert "next_chunk_index" not in last


def test_account_vault_auto_cookies(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr("trawler.config.VAULT_DIR", tmp_path / "vault")
    monkeypatch.setenv("TRAWLER_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(account_vault, "_fernet", None)
    domain = "example.com"
    
    cookies = [
        {"name": "cf_clearance", "value": "xyz123abc", "domain": domain, "path": "/", "httpOnly": True},
        {"name": "session_id", "value": "sess999", "domain": domain, "path": "/", "secure": True},
    ]
    account_vault.save_auto_cookies(domain, cookies)
    
    loaded = account_vault.get_auto_cookies(domain)
    assert loaded.get("cf_clearance") == "xyz123abc"
    assert loaded.get("session_id") == "sess999"
    assert account_vault.auto_cookies_path(domain).exists()
    assert not (account_vault.domain_dir(domain) / "auto_cookies.json").exists()
    assert "xyz123abc" not in account_vault.auto_cookies_path(domain).read_text(encoding="utf-8")


def test_account_vault_auto_cookies_are_session_scoped(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr("trawler.config.VAULT_DIR", tmp_path / "vault")
    monkeypatch.setenv("TRAWLER_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(account_vault, "_fernet", None)
    domain = "example.com"

    account_vault.save_auto_cookies(
        domain,
        [{"name": "cf_clearance", "value": "aaa", "domain": domain, "path": "/"}],
        session_id="session-a",
    )
    account_vault.save_auto_cookies(
        domain,
        [{"name": "cf_clearance", "value": "bbb", "domain": domain, "path": "/"}],
        session_id="session-b",
    )

    assert account_vault.get_auto_cookies(domain, session_id="session-a")["cf_clearance"] == "aaa"
    assert account_vault.get_auto_cookies(domain, session_id="session-b")["cf_clearance"] == "bbb"
    assert account_vault.auto_cookies_path(domain, session_id="session-a").exists()
    assert account_vault.auto_cookies_path(domain, session_id="session-b").exists()


def test_account_vault_multi_account_paths_are_isolated(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr("trawler.config.VAULT_DIR", tmp_path / "vault")
    monkeypatch.setenv("TRAWLER_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(account_vault, "_fernet", None)
    domain = "example.com"

    account_vault.save_storage_state(
        domain,
        {"cookies": [{"name": "sid", "value": "default"}], "origins": []},
    )
    account_vault.save_storage_state(
        domain,
        {"cookies": [{"name": "sid", "value": "work"}], "origins": []},
        account_id="work",
    )
    account_vault.save_auto_cookies(
        domain,
        [{"name": "cf_clearance", "value": "aaa", "domain": domain, "path": "/"}],
        account_id="work",
    )

    assert '"default"' in account_vault.get_storage_state(domain)
    assert '"work"' in account_vault.get_storage_state(domain, account_id="work")
    assert account_vault.storage_state_path(domain).parent == account_vault.domain_dir(domain)
    work_state_path = account_vault.storage_state_path(domain, account_id="work")
    assert work_state_path.parent.name == "work"
    assert work_state_path.parent.parent.name == "accounts"
    assert account_vault.get_auto_cookies(domain, account_id="work")["cf_clearance"] == "aaa"


def test_account_vault_named_account_does_not_read_default_legacy_state(tmp_path, monkeypatch):
    monkeypatch.setattr("trawler.config.VAULT_DIR", tmp_path / "vault")
    monkeypatch.setattr("trawler.config.ALLOW_LEGACY_PLAINTEXT_VAULT", True)
    monkeypatch.delenv("TRAWLER_VAULT_KEY", raising=False)
    domain = "example.com"

    default_legacy = account_vault.domain_dir(domain) / "storage_state.json"
    default_legacy.parent.mkdir(parents=True, exist_ok=True)
    default_legacy.write_text('{"cookies":[{"name":"sid","value":"default"}]}', encoding="utf-8")

    assert "default" in account_vault.get_storage_state(domain)
    assert account_vault.get_storage_state(domain, account_id="work") is None


def test_account_vault_rejects_unsafe_path_keys(tmp_path, monkeypatch):
    monkeypatch.setattr("trawler.config.VAULT_DIR", tmp_path / "vault")

    assert account_vault.domain_dir("Example.COM").name == "example.com"
    assert account_vault.domain_dir("例子.com").name.startswith("xn--")

    with pytest.raises(ValueError):
        account_vault.domain_dir("../secret")
    with pytest.raises(ValueError):
        account_vault.auto_cookies_path("example.com", session_id="../secret")
    with pytest.raises(ValueError):
        account_vault.auto_cookies_path("example.com", session_id="bad\\secret")
    with pytest.raises(ValueError):
        account_vault.profile_dir("example.com", account_id="../secret")
    with pytest.raises(ValueError):
        account_vault.storage_state_path("example.com", account_id="bad\\secret")


def test_site_rules_promote_domain():
    domain = "anti-bot-site.org"
    rule_before = site_rules.load(domain)
    assert rule_before is None or rule_before.gear_hint != "patchright"

    site_rules.promote_domain(domain, gear_hint="patchright", ttl=60.0)
    
    rule_after = site_rules.load(domain)
    assert rule_after is not None
    assert rule_after.gear_hint == "patchright"


def test_site_intelligence_profile_loads_for_subdomain():
    payload = site_rules.site_profile_payload("www.xiaohongshu.com")

    assert payload["ok"] is True
    assert payload["matched_domain"] == "xiaohongshu.com"
    profile = payload["profile"]
    assert profile["profile_name"] == "Site Intelligence Profile"
    assert profile["observed_at"] == "2026-07-07"
    assert "bundle" in profile["recommended_extract_modes"]
    assert "visible_blocks" in profile["recommended_extract_modes"]
    assert profile["validation"]["last_verified_at"] == "2026-07-07"


def test_mark_needs_account_upserts(tmp_db):
    conn = db.connect()
    try:
        rules.mark_needs_account(conn, "needs-login.example")
        rule = rules.get(conn, "needs-login.example")
        assert rule is not None
        assert rule.needs_account is True
    finally:
        conn.close()
