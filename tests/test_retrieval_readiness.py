from cryptography.fernet import Fernet


def test_retrieval_readiness_prefers_retrieve_when_active_state_exists(tmp_db, monkeypatch):
    from trawler import account_profiles, account_vault, retrieval_readiness

    monkeypatch.setenv("TRAWLER_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(account_vault, "_fernet", None)
    account_profiles.register_profile(
        "example.com",
        account_id="work",
        label="Work",
        make_default=True,
    )
    account_vault.save_storage_state(
        "example.com",
        {"cookies": [{"name": "sid", "value": "secret"}], "origins": []},
        account_id="work",
    )

    payload = retrieval_readiness.readiness_payload(
        "https://example.com/private",
        account_id="work",
    )

    assert payload["ok"] is True
    assert payload["domain"] == "example.com"
    assert payload["accounts"]["selected_account_id"] == "work"
    assert payload["accounts"]["selected_profile"]["usable_for_automation"] is True
    assert payload["vault"]["storage_state_present"] is True
    assert payload["recommendation"]["tool"] == "retrieve_page"
    assert payload["recommendation"]["next_call"]["account_id"] == "work"
    assert payload["policy_decision"]["allowed"] is True
    assert payload["policy_decision"]["tool"] == "retrieve_page"


def test_retrieval_readiness_routes_expired_profile_to_browser(tmp_db, monkeypatch):
    from trawler import account_profiles, account_vault, retrieval_readiness

    monkeypatch.setenv("TRAWLER_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(account_vault, "_fernet", None)
    account_profiles.register_profile("example.com", account_id="work", make_default=True)
    account_profiles.mark_profile_status(
        "example.com",
        "work",
        "expired",
        notes="login expired",
    )

    payload = retrieval_readiness.readiness_payload("example.com")

    assert payload["accounts"]["selected_account_id"] == "default"
    assert payload["recommendation"]["tool"] == "open_browser_session"
    assert "no_account_profile" in payload["recommendation"]["reasons"]


def test_retrieval_readiness_explicit_expired_profile_keeps_account_id(tmp_db, monkeypatch):
    from trawler import account_profiles, account_vault, retrieval_readiness

    monkeypatch.setenv("TRAWLER_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(account_vault, "_fernet", None)
    account_profiles.register_profile("example.com", account_id="work", make_default=True)
    account_profiles.mark_profile_status("example.com", "work", "needs_login")

    payload = retrieval_readiness.readiness_payload("example.com", account_id="work")

    assert payload["accounts"]["selected_account_id"] == "work"
    assert payload["accounts"]["selected_profile"]["usable_for_automation"] is False
    assert payload["recommendation"]["tool"] == "open_browser_session"
    assert "account_needs_login" in payload["recommendation"]["reasons"]


def test_retrieval_readiness_includes_policy_denial(tmp_db, monkeypatch):
    from trawler import config, retrieval_readiness

    monkeypatch.setattr(config, "ENABLE_LIVE_BROWSER", False)

    payload = retrieval_readiness.readiness_payload("example.com")

    assert payload["recommendation"]["tool"] == "open_browser_session"
    assert payload["policy_decision"]["allowed"] is False
    assert "live_browser_disabled" in payload["policy_decision"]["reasons"]
    assert "policy_denied" in payload["recommendation"]["issues"]
