from cryptography.fernet import Fernet


def test_account_profiles_register_list_default_and_status(tmp_db, monkeypatch):
    from trawler import account_profiles, account_vault

    monkeypatch.setenv("TRAWLER_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(account_vault, "_fernet", None)

    work = account_profiles.register_profile(
        "Example.COM",
        account_id="work",
        label="Work login",
        login_method="manual_qr",
        notes="qr login only",
        risk_flags=["rate_limit_sensitive"],
        make_default=True,
    )
    account_profiles.register_profile(
        "example.com",
        account_id="personal",
        label="Personal login",
        login_method="manual_password",
        make_default=True,
    )

    assert work.domain == "example.com"
    assert work.account_id == "work"
    assert "accounts" in work.profile_dir
    assert "work" in work.profile_dir
    assert account_profiles.default_account_id("example.com") == "personal"

    profiles = account_profiles.list_profiles("example.com")
    defaults = [profile.account_id for profile in profiles if profile.is_default]
    assert defaults == ["personal"]

    blocked = account_profiles.mark_profile_status(
        "example.com",
        "work",
        "blocked",
        notes="site blocked this login",
        expires_at="2026-12-31T00:00:00Z",
    )

    assert blocked.status == "blocked"
    assert blocked.notes == "site blocked this login"
    assert blocked.expires_at == "2026-12-31T00:00:00Z"
    assert account_profiles.get_profile("example.com", "work").risk_flags == [
        "rate_limit_sensitive"
    ]

    verified = account_profiles.touch_verified("example.com", "work")
    assert verified.status == "active"
    assert verified.last_verified_at


def test_account_profiles_default_selection_skips_unusable_profiles(tmp_db):
    from trawler import account_profiles

    account_profiles.register_profile("example.com", account_id="blocked", make_default=True)
    account_profiles.mark_profile_status("example.com", "blocked", "blocked")
    account_profiles.register_profile("example.com", account_id="expired")
    account_profiles.mark_profile_status("example.com", "expired", "expired")
    account_profiles.register_profile("example.com", account_id="active")

    assert account_profiles.default_account_id("example.com") == "active"
    assert account_profiles.is_usable_for_automation(
        account_profiles.get_profile("example.com", "active")
    )
    assert not account_profiles.is_usable_for_automation(
        account_profiles.get_profile("example.com", "blocked")
    )


def test_account_profiles_reject_invalid_values(tmp_db):
    import pytest

    from trawler import account_profiles

    with pytest.raises(ValueError):
        account_profiles.register_profile("example.com", account_id="../secret")
    with pytest.raises(ValueError):
        account_profiles.register_profile("example.com", login_method="password_store")
    with pytest.raises(ValueError):
        account_profiles.mark_profile_status("example.com", "default", "unknown")
