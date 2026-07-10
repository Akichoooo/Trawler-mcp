"""Retrieval readiness report for agents.

This module combines site intelligence, account profile status, vault presence,
and a conservative next-step recommendation before an agent touches a page.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trawler import account_profiles, account_vault, policy, site_rules, urlnorm
from trawler.urlnorm import domain_of


def _domain_from_target(target: str) -> tuple[str, str]:
    raw = str(target or "").strip()
    if not raw:
        raise ValueError("target is required")
    canonical = urlnorm.canonical_url(raw)
    if canonical and domain_of(canonical):
        return domain_of(canonical), canonical
    return account_profiles.normalize_domain(raw), ""


def _profile_payload(profile: account_profiles.AccountProfile | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    payload = profile.as_dict()
    payload["usable_for_automation"] = account_profiles.is_usable_for_automation(profile)
    payload["status_reason"] = account_profiles.status_reason(profile)
    return payload


def _vault_payload(domain: str, account_id: str) -> dict[str, Any]:
    storage_state_path = account_vault.storage_state_path(domain, account_id=account_id)
    cookie_jar_path = account_vault.auto_cookies_path(domain, account_id=account_id)
    return {
        "enabled": account_vault.is_vault_enabled(),
        "storage_state_present": storage_state_path.exists(),
        "cookie_jar_present": cookie_jar_path.exists(),
        "storage_state_path": str(storage_state_path),
        "cookie_jar_path": str(cookie_jar_path),
    }


def _recommended_extract_mode(site_profile: dict[str, Any]) -> str:
    profile = site_profile.get("profile") if isinstance(site_profile, dict) else {}
    modes = profile.get("recommended_extract_modes") if isinstance(profile, dict) else []
    if isinstance(modes, list):
        for mode in ("bundle", "visible_blocks", "fit_markdown", "page"):
            if mode in modes:
                return mode
    return "bundle"


def readiness_payload(
    target: str,
    *,
    account_id: str = "",
    access_mode: str = "user_authorized",
) -> dict[str, Any]:
    domain, canonical_url = _domain_from_target(target)
    explicit_account = bool(str(account_id or "").strip())
    selected_account_id = account_profiles.resolve_account_id(domain, account_id)
    profiles = account_profiles.list_profiles(domain)
    selected_profile = account_profiles.get_profile(domain, selected_account_id)
    site_profile = site_rules.site_profile_payload(domain)
    vault = _vault_payload(domain, selected_account_id)

    site_rule = site_profile.get("profile") if site_profile.get("ok") else {}
    site_needs_account = (
        bool(site_rule.get("needs_account")) if isinstance(site_rule, dict) else False
    )
    human_assist_info = site_rule.get("human_assist") if isinstance(site_rule, dict) else {}
    human_expected = bool(human_assist_info) or site_needs_account
    usable_profile = account_profiles.is_usable_for_automation(selected_profile)
    status_reason = account_profiles.status_reason(selected_profile)

    issues: list[str] = []
    reasons: list[str] = []
    if not site_profile.get("ok"):
        reasons.append("no_site_profile")
    if site_needs_account:
        reasons.append("site_needs_account")
    if human_expected:
        reasons.append("human_assist_expected")
    if selected_profile is None:
        reasons.append("no_account_profile")
    elif not usable_profile:
        reasons.append(f"account_{status_reason}")
    if not vault["enabled"] and access_mode == "user_authorized":
        issues.append("TRAWLER_VAULT_KEY is not set; authorized browser state cannot be persisted")
    if (
        selected_profile is not None
        and selected_profile.status == "blocked"
        and not explicit_account
    ):
        issues.append("default account profile is blocked and will not be selected automatically")

    extract_mode = _recommended_extract_mode(site_profile)
    if (
        selected_profile is not None
        and selected_profile.status == "blocked"
        and not explicit_account
    ):
        tool = "mark_account_profile_or_register_account_profile"
        human_assist = "required"
    elif access_mode == "standard" and not human_expected:
        tool = "retrieve_page"
        human_assist = "off"
    elif usable_profile and vault["storage_state_present"] and not human_expected:
        tool = "retrieve_page"
        human_assist = "auto"
    else:
        tool = "open_browser_session"
        human_assist = "required" if (site_needs_account or not usable_profile) else "auto"

    next_call: dict[str, Any] = {
        "tool": tool,
        "url": canonical_url or "",
        "domain": domain,
    }
    if tool == "open_browser_session":
        next_call.update(
            {
                "account_id": selected_account_id,
                "access_mode": "user_authorized",
            }
        )
    elif tool == "retrieve_page":
        next_call.update(
            {
                "access_mode": access_mode,
                "account_id": selected_account_id if access_mode == "user_authorized" else "",
                "human_assist": human_assist,
                "extract_mode": "page",
            }
        )
    policy_decision = policy.decide(
        tool,
        target_url=canonical_url,
        domain=domain,
        access_mode=access_mode,
        uses_live_browser=tool == "open_browser_session"
        or (tool == "retrieve_page" and access_mode == "user_authorized"),
    )
    if not policy_decision.allowed:
        issues.append("policy_denied")

    return {
        "ok": True,
        "target": target,
        "canonical_url": canonical_url,
        "domain": domain,
        "access_mode": access_mode,
        "site_profile": site_profile,
        "accounts": {
            "count": len(profiles),
            "selected_account_id": selected_account_id,
            "selected_profile": _profile_payload(selected_profile),
            "items": [_profile_payload(profile) for profile in profiles],
        },
        "vault": vault,
        "recommendation": {
            "tool": tool,
            "human_assist": human_assist,
            "extract_mode": extract_mode,
            "reasons": reasons,
            "issues": issues,
            "next_call": next_call,
        },
        "policy_decision": policy_decision.as_dict(),
    }


def readiness_summary(payload: dict[str, Any]) -> str:
    recommendation = payload.get("recommendation", {})
    accounts = payload.get("accounts", {})
    vault = payload.get("vault", {})
    return (
        f"domain={payload.get('domain')} "
        f"tool={recommendation.get('tool')} "
        f"extract_mode={recommendation.get('extract_mode')} "
        f"account_id={accounts.get('selected_account_id')} "
        f"vault_state={'yes' if vault.get('storage_state_present') else 'no'}"
    )


def path_exists(path: str) -> bool:
    return Path(path).exists()
