"""Crawl scope policy shared by frontier, site index, and page fetches."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from trawler import urlnorm


def _tuple_or_empty(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    return tuple(str(item) for item in (values or ()) if str(item))


@dataclass(frozen=True)
class CrawlPolicy:
    """Small interface for all crawl scope decisions.

    The policy has two URL classes:
    - page URLs: final crawl targets, subject to domain/path/query policy.
    - index URLs: sitemap/feed documents, subject to domain/query policy but
      not page path filters, because /sitemap.xml often sits outside /docs/*.
    """

    start_url: str
    same_domain_only: bool = True
    max_depth: int = -1
    include_paths: tuple[str, ...] = field(default_factory=tuple)
    exclude_paths: tuple[str, ...] = field(default_factory=tuple)
    include_subdomains: bool = False
    ignore_query_parameters: bool = False

    def __post_init__(self) -> None:
        canonical_start = urlnorm.canonical_url(self.start_url)
        if canonical_start and self.ignore_query_parameters:
            canonical_start = drop_query(canonical_start)
        object.__setattr__(self, "start_url", canonical_start)
        object.__setattr__(self, "include_paths", _tuple_or_empty(self.include_paths))
        object.__setattr__(self, "exclude_paths", _tuple_or_empty(self.exclude_paths))

    @classmethod
    def from_options(
        cls,
        start_url: str,
        *,
        same_domain_only: bool = True,
        max_depth: int = -1,
        include_paths: list[str] | tuple[str, ...] | None = None,
        exclude_paths: list[str] | tuple[str, ...] | None = None,
        include_subdomains: bool = False,
        ignore_query_parameters: bool = False,
    ) -> CrawlPolicy:
        return cls(
            start_url=start_url,
            same_domain_only=same_domain_only,
            max_depth=max_depth,
            include_paths=_tuple_or_empty(include_paths),
            exclude_paths=_tuple_or_empty(exclude_paths),
            include_subdomains=include_subdomains,
            ignore_query_parameters=ignore_query_parameters,
        )

    @property
    def start_domain(self) -> str:
        return urlnorm.domain_of(self.start_url)

    @property
    def allowed_domain(self) -> str:
        return self.start_domain if self.same_domain_only else ""

    def payload(self) -> dict:
        return {
            "same_domain_only": self.same_domain_only,
            "max_depth": self.max_depth,
            "include_paths": list(self.include_paths),
            "exclude_paths": list(self.exclude_paths),
            "include_subdomains": self.include_subdomains,
            "ignore_query_parameters": self.ignore_query_parameters,
        }

    def normalize_page_url(self, raw_url: str, *, base_url: str = "") -> str:
        return self._normalize(raw_url, base_url=base_url, apply_path_policy=True)

    def normalize_index_url(self, raw_url: str, *, base_url: str = "") -> str:
        return self._normalize(raw_url, base_url=base_url, apply_path_policy=False)

    def _normalize(self, raw_url: str, *, base_url: str, apply_path_policy: bool) -> str:
        if not raw_url:
            return ""
        candidate = urljoin(base_url, raw_url.strip()) if base_url else raw_url.strip()
        normalized = urlnorm.canonical_url(candidate)
        if normalized and self.ignore_query_parameters:
            normalized = drop_query(normalized)
        if not normalized:
            return ""
        if not self.domain_allowed(normalized):
            return ""
        if apply_path_policy and not self.path_allowed(normalized):
            return ""
        return normalized

    def domain_allowed(self, url: str) -> bool:
        if not self.same_domain_only:
            return True
        domain = urlnorm.domain_of(url)
        start_domain = self.start_domain
        if domain == start_domain:
            return True
        return self.include_subdomains and domain.endswith(f".{start_domain}")

    def path_allowed(self, url: str) -> bool:
        path = urlsplit(url).path or "/"
        if self.include_paths and not any(
            fnmatch.fnmatch(path, pattern) for pattern in self.include_paths
        ):
            return False
        if self.exclude_paths and any(
            fnmatch.fnmatch(path, pattern) for pattern in self.exclude_paths
        ):
            return False
        return True

    def final_url_allowed(self, final_url: str) -> bool:
        if not final_url or not self.allowed_domain:
            return True
        return self.domain_allowed(final_url) and self.path_allowed(final_url)

    def should_expand_depth(self, depth: int) -> bool:
        return self.max_depth < 0 or depth < self.max_depth

    def normalize_seed_urls(self, seed_urls: list[str], *, limit: int) -> list[str]:
        result: list[str] = []
        seen: set[str] = {self.start_url}
        for seed_url in seed_urls:
            normalized = self.normalize_page_url(seed_url)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
            if len(result) >= limit:
                break
        return result


def drop_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
