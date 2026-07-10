"""Central policy decisions for MCP-exposed retrieval tools.

This module is intentionally small at the interface. Callers describe the
intended tool call; the policy broker returns one decision payload that can be
used by MCP tools, readiness reports, and future gateway/sandbox adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any

from trawler import config, urlnorm
from trawler.urlnorm import domain_of

LOW_RISK_TOOLS = {
    "get_site_profile",
    "get_retrieval_readiness",
    "get_policy_decision",
    "list_account_profiles",
    "list_browser_sessions",
    "list_raw",
    "list_artifacts",
    "get_engine_status",
}
MEDIUM_RISK_TOOLS = {
    "retrieve_page",
    "crawl_url",
    "crawl_url_structured",
    "map_site",
    "map_site_structured",
    "discover_site_index",
    "discover_site_index_structured",
    "get_job_status",
    "get_job_status_structured",
    "get_job_errors",
    "get_job_errors_structured",
    "get_job_results",
    "get_job_results_structured",
    "wait_for_job",
    "wait_for_job_structured",
}
HIGH_RISK_TOOLS = {
    "crawl_site",
    "crawl_site_structured",
    "crawl_site_indexed",
    "crawl_site_indexed_structured",
    "get_raw",
    "get_raw_metadata",
    "get_raw_metadata_structured",
    "get_artifact_summary",
    "get_artifact_summary_structured",
    "get_artifact_screenshot",
}
CRITICAL_RISK_TOOLS = {
    "open_browser_session",
    "connect_browser_session",
    "run_browser_actions",
    "start_element_picker",
    "start_region_picker",
    "observe_browser_session",
    "extract_browser_session",
    "close_browser_session",
    "get_artifact",
    "cleanup_artifacts",
}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    tool: str
    risk: str
    mode: str
    reasons: list[str] = field(default_factory=list)
    restrictions: dict[str, Any] = field(default_factory=dict)
    approval_required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "tool": self.tool,
            "risk": self.risk,
            "mode": self.mode,
            "approval_required": self.approval_required,
            "reasons": list(self.reasons),
            "restrictions": dict(self.restrictions),
        }


def _csv_values(raw: str) -> tuple[str, ...]:
    values: list[str] = []
    for chunk in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        item = chunk.strip().lower()
        if item:
            values.append(item)
    return tuple(values)


def _domain_matches(domain: str, pattern: str) -> bool:
    domain = domain.lower().strip(".")
    pattern = pattern.lower().strip(".")
    if not domain or not pattern:
        return False
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return domain == suffix or domain.endswith(f".{suffix}")
    if "*" in pattern:
        return fnmatch(domain, pattern)
    return domain == pattern or domain.endswith(f".{pattern}")


def _domain_from_target(target_url: str = "", domain: str = "") -> str:
    if domain:
        return str(domain).lower().strip()
    canonical = urlnorm.canonical_url(target_url)
    return domain_of(canonical) if canonical else ""


def tool_risk(tool: str) -> str:
    if tool in CRITICAL_RISK_TOOLS:
        return "critical"
    if tool in HIGH_RISK_TOOLS:
        return "high"
    if tool in MEDIUM_RISK_TOOLS:
        return "medium"
    if tool in LOW_RISK_TOOLS:
        return "low"
    return "unknown"


def decide(
    tool: str,
    *,
    target_url: str = "",
    domain: str = "",
    access_mode: str = "",
    requested_pages: int | None = None,
    uses_live_browser: bool = False,
    uses_cdp: bool = False,
    reads_artifact_body: bool = False,
    capture_artifact: bool = False,
    dry_run: bool = True,
) -> PolicyDecision:
    risk = tool_risk(tool)
    mode = str(getattr(config, "POLICY_MODE", "permissive") or "permissive").lower()
    target_domain = _domain_from_target(target_url=target_url, domain=domain)
    reasons: list[str] = []
    restrictions: dict[str, Any] = {
        "policy_mode": mode,
        "risk": risk,
        "target_domain": target_domain,
        "live_browser_enabled": bool(getattr(config, "ENABLE_LIVE_BROWSER", True)),
        "cdp_enabled": bool(getattr(config, "ENABLE_CDP", True)),
        "crawl_site_enabled": bool(getattr(config, "ENABLE_CRAWL_SITE", True)),
        "artifact_bodies_enabled": bool(getattr(config, "EXPOSE_ARTIFACT_BODIES", False)),
        "max_pages_hard": int(getattr(config, "MAX_PAGES_HARD", 500)),
    }

    allowed = True
    approval_required = risk == "critical"

    allowed_domains = _csv_values(getattr(config, "ALLOWED_DOMAINS", ""))
    blocked_domains = _csv_values(getattr(config, "BLOCKED_DOMAINS", ""))
    if target_domain:
        if any(_domain_matches(target_domain, pattern) for pattern in blocked_domains):
            allowed = False
            reasons.append("blocked_domain")
        if allowed_domains and not any(
            _domain_matches(target_domain, pattern) for pattern in allowed_domains
        ):
            allowed = False
            reasons.append("domain_not_allowed")
    elif mode == "strict" and risk in {"medium", "high", "critical"}:
        allowed = False
        reasons.append("target_domain_required")

    if risk == "unknown" and mode == "strict":
        allowed = False
        reasons.append("unknown_tool")

    if tool in {
        "crawl_site",
        "crawl_site_structured",
        "crawl_site_indexed",
        "crawl_site_indexed_structured",
    } and not getattr(config, "ENABLE_CRAWL_SITE", True):
        allowed = False
        reasons.append("crawl_site_disabled")

    if uses_live_browser and not getattr(config, "ENABLE_LIVE_BROWSER", True):
        allowed = False
        reasons.append("live_browser_disabled")

    if uses_cdp and not getattr(config, "ENABLE_CDP", True):
        allowed = False
        reasons.append("cdp_disabled")

    if reads_artifact_body and not getattr(config, "EXPOSE_ARTIFACT_BODIES", False):
        allowed = False
        reasons.append("artifact_body_disabled")

    if requested_pages is not None:
        max_pages_hard = int(getattr(config, "MAX_PAGES_HARD", 500))
        if int(requested_pages) > max_pages_hard:
            allowed = False
            reasons.append("max_pages_exceeds_hard_limit")
        restrictions["requested_pages"] = int(requested_pages)

    if capture_artifact:
        restrictions["artifact_capture"] = True
        if risk in {"high", "critical"}:
            approval_required = True

    if access_mode == "user_authorized":
        restrictions["single_page_user_authorized"] = True

    if allowed and not reasons:
        reasons.append("allowed_by_policy")

    return PolicyDecision(
        allowed=allowed,
        tool=tool,
        risk=risk,
        mode=mode,
        reasons=reasons,
        restrictions=restrictions,
        approval_required=approval_required,
    )


def readiness_decision(
    target: str,
    *,
    access_mode: str = "user_authorized",
    tool: str = "retrieve_page",
) -> PolicyDecision:
    canonical = urlnorm.canonical_url(target) or ""
    target_domain = domain_of(canonical) if canonical else str(target or "")
    return decide(
        tool,
        target_url=canonical,
        domain="" if canonical else target_domain,
        access_mode=access_mode,
        uses_live_browser=tool in CRITICAL_RISK_TOOLS,
    )
