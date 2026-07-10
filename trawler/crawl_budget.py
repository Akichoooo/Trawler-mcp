"""Crawl budget controls for frontier workers."""

from __future__ import annotations

from dataclasses import dataclass

from trawler import config


@dataclass(frozen=True)
class CrawlBudget:
    max_pages: int
    max_errors: int = 0
    max_links_per_page: int = 200

    @classmethod
    def from_config(cls, max_pages: int) -> CrawlBudget:
        return cls(
            max_pages=max_pages,
            max_errors=max(0, int(getattr(config, "CRAWL_MAX_ERRORS", 0))),
            max_links_per_page=max(1, int(getattr(config, "MAX_LINKS_PER_PAGE", 200))),
        )

    def stop_reason(self, counts: dict[str, int]) -> str:
        if counts.get("terminal", 0) >= self.max_pages:
            return "page-limit"
        if self.max_errors and counts.get("error", 0) >= self.max_errors:
            return "error-limit"
        return ""

    def status_for_stop_reason(self, reason: str) -> str:
        if reason == "error-limit":
            return "failed"
        return "completed"
