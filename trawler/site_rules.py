"""site_rules — 特定站点爬虫策略。

从 data/site_rules/*.yaml 加载种子规则。
优先级高于 DB 手册规则，可覆盖 gear_hint / wait_strategy / needs_proxy。
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("trawler.site_rules")

_RULES: dict[str, "SiteRule"] = {}
_LAST_MTIME: float = 0.0


@dataclass
class SiteRule:
    domain: str
    gear_hint: str | None = None
    wait_strategy: str = "domcontentloaded"
    wait_for_selector: str = ""  # fetcher 阶段等此选择器出现 (SPA 动态内容, 如 .w-dyn-item)
    selectors: list[str] = field(default_factory=list)
    needs_account: bool = False
    needs_proxy: bool = False
    notes: str = ""
    profile_name: str = "Site Intelligence Profile"
    profile_version: int = 1
    observed_at: str = ""
    review_after: str = ""
    page_traits: list[str] = field(default_factory=list)
    recommended_extract_modes: list[str] = field(default_factory=list)
    extraction_strategy: list[str] = field(default_factory=list)
    human_assist: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    known_limits: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SiteRule":
        return cls(
            domain=data.get("domain", ""),
            gear_hint=data.get("gear_hint"),
            wait_strategy=data.get("wait_strategy", "domcontentloaded"),
            wait_for_selector=data.get("wait_for_selector", ""),
            selectors=data.get("selectors", []),
            needs_account=data.get("needs_account", False),
            needs_proxy=data.get("needs_proxy", False),
            notes=data.get("notes", ""),
            profile_name=data.get("profile_name", "Site Intelligence Profile"),
            profile_version=int(data.get("profile_version", 1) or 1),
            observed_at=data.get("observed_at", ""),
            review_after=data.get("review_after", ""),
            page_traits=data.get("page_traits", []),
            recommended_extract_modes=data.get("recommended_extract_modes", []),
            extraction_strategy=data.get("extraction_strategy", []),
            human_assist=data.get("human_assist", {}),
            validation=data.get("validation", {}),
            known_limits=data.get("known_limits", []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "gear_hint": self.gear_hint,
            "wait_strategy": self.wait_strategy,
            "wait_for_selector": self.wait_for_selector,
            "selectors": self.selectors,
            "needs_account": self.needs_account,
            "needs_proxy": self.needs_proxy,
            "notes": self.notes,
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
            "observed_at": self.observed_at,
            "review_after": self.review_after,
            "page_traits": self.page_traits,
            "recommended_extract_modes": self.recommended_extract_modes,
            "extraction_strategy": self.extraction_strategy,
            "human_assist": self.human_assist,
            "validation": self.validation,
            "known_limits": self.known_limits,
        }


import threading  # noqa: E402

_load_lock = threading.Lock()

def _load_all() -> None:
    global _LAST_MTIME
    with _load_lock:
        rules_dir = Path("data/site_rules")
        if not rules_dir.exists() or not rules_dir.is_dir():
            base_dir = Path(__file__).parent.parent
            rules_dir = base_dir / "data" / "site_rules"

        if not rules_dir.exists() or not rules_dir.is_dir():
            return

        current_mtime = os.stat(rules_dir).st_mtime
        for f in rules_dir.glob("*.yaml"):
            try:
                mtime = os.stat(f).st_mtime
                if mtime > current_mtime:
                    current_mtime = mtime
            except OSError:
                pass

        if current_mtime <= _LAST_MTIME:
            return

        new_rules = {}
        for f in rules_dir.glob("*.yaml"):
            try:
                with open(f, encoding="utf-8") as file:
                    data = yaml.safe_load(file)
                    if data and "domain" in data:
                        rule = SiteRule.from_dict(data)
                        new_rules[rule.domain] = rule
                        log.debug("Loaded site rule for %s", rule.domain)
            except Exception as e:
                log.warning("Failed to load site rule %s: %s", f.name, e)
        
        global _RULES
        _RULES = new_rules
        _LAST_MTIME = current_mtime


import time  # noqa: E402

_AUTO_PROMOTED_RULES: dict[str, tuple[float, SiteRule]] = {}
_PROMOTION_LOCK = threading.Lock()


def promote_domain(domain: str, gear_hint: str = "patchright", ttl: float = 86400.0) -> None:
    """动态打标：当某域名在轻量 Rung 频繁被封且在重型 Rung 突破成功时，提升其策略。"""
    with _PROMOTION_LOCK:
        rule = SiteRule(domain=domain, gear_hint=gear_hint, notes="Auto-promoted rule")
        _AUTO_PROMOTED_RULES[domain] = (time.time() + ttl, rule)
        log.info(
            "Auto-promoted site rule for %s to gear_hint=%s (TTL %ds)",
            domain,
            gear_hint,
            int(ttl),
        )


def load(domain: str) -> SiteRule | None:
    """加载特定域名的规则 (支持子域匹配与自适应打标规则)。"""
    now = time.time()
    with _PROMOTION_LOCK:
        if domain in _AUTO_PROMOTED_RULES:
            exp_time, rule = _AUTO_PROMOTED_RULES[domain]
            if now < exp_time:
                return rule
            else:
                _AUTO_PROMOTED_RULES.pop(domain, None)

    _load_all()
    
    parts = domain.split(".")
    for i in range(len(parts)):
        sub = ".".join(parts[i:])
        if sub in _RULES:
            return _RULES[sub]
    return None


def site_profile_payload(domain: str) -> dict[str, Any]:
    rule = load(domain)
    if rule is None:
        return {
            "ok": False,
            "domain": domain,
            "profile_name": "Site Intelligence Profile",
            "message": "No site intelligence profile is available for this domain.",
        }
    return {
        "ok": True,
        "domain": domain,
        "matched_domain": rule.domain,
        "profile_name": rule.profile_name,
        "profile": rule.to_dict(),
    }
