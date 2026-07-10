"""Deterministic sitemap/feed discovery for crawl seeding."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx

from trawler import config, proxy_pool, ssrf, urlnorm
from trawler.crawl_policy import CrawlPolicy
from trawler.errors import format_error

_USER_AGENT = "TrawlerBot/0.1 (+https://modelcontextprotocol.io)"
_FEED_TYPES = {
    "application/rss+xml",
    "application/atom+xml",
    "application/feed+json",
    "text/xml",
    "application/xml",
}


def origin_for(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def parse_robots_sitemaps(
    text: str,
    base_url: str,
    *,
    same_domain_only: bool = True,
    include_subdomains: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    ignore_query_parameters: bool = False,
    max_urls: int = 20,
) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().lower() != "sitemap":
            continue
        normalized = _normalize_candidate(
            value.strip(),
            base_url,
            same_domain_only,
            include_subdomains=include_subdomains,
            include_paths=None,
            exclude_paths=None,
            ignore_query_parameters=ignore_query_parameters,
        )
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
        if len(urls) >= max_urls:
            break
    return urls


def parse_html_feed_links(
    html: str,
    base_url: str,
    *,
    same_domain_only: bool = True,
    include_subdomains: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    ignore_query_parameters: bool = False,
    max_urls: int = 20,
) -> list[str]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    soup = BeautifulSoup(html or "", "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all("link", href=True):
        rel = tag.get("rel") or []
        rel_values = [rel] if isinstance(rel, str) else [str(item) for item in rel]
        rel_set = {item.lower() for item in rel_values}
        type_value = str(tag.get("type") or "").lower()
        if "alternate" not in rel_set and type_value not in _FEED_TYPES:
            continue
        if type_value and type_value not in _FEED_TYPES:
            continue
        normalized = _normalize_candidate(
            str(tag.get("href") or ""),
            base_url,
            same_domain_only,
            include_subdomains=include_subdomains,
            include_paths=None,
            exclude_paths=None,
            ignore_query_parameters=ignore_query_parameters,
        )
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
        if len(urls) >= max_urls:
            break
    return urls


def parse_index_document(
    text: str,
    base_url: str,
    *,
    same_domain_only: bool = True,
    include_subdomains: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    ignore_query_parameters: bool = False,
    max_urls: int = 200,
) -> dict[str, Any]:
    """Parse sitemap, sitemap index, RSS, or Atom content."""
    try:
        root = ElementTree.fromstring(text.encode("utf-8"))
    except ElementTree.ParseError:
        return {"kind": "unknown", "urls": [], "sitemap_urls": [], "feed_urls": []}

    root_name = _local_name(root.tag)
    if root_name == "sitemapindex":
        return {
            "kind": "sitemapindex",
            "urls": [],
            "sitemap_urls": _xml_loc_values(
                root,
                base_url,
                same_domain_only=same_domain_only,
                include_subdomains=include_subdomains,
                include_paths=None,
                exclude_paths=None,
                ignore_query_parameters=ignore_query_parameters,
                max_urls=max_urls,
                parent_name="sitemap",
            ),
            "feed_urls": [],
        }
    if root_name == "urlset":
        return {
            "kind": "urlset",
            "urls": _xml_loc_values(
                root,
                base_url,
                same_domain_only=same_domain_only,
                include_subdomains=include_subdomains,
                include_paths=include_paths,
                exclude_paths=exclude_paths,
                ignore_query_parameters=ignore_query_parameters,
                max_urls=max_urls,
                parent_name="url",
            ),
            "sitemap_urls": [],
            "feed_urls": [],
        }
    if root_name == "rss":
        return {
            "kind": "rss",
            "urls": _rss_item_links(
                root,
                base_url,
                same_domain_only=same_domain_only,
                include_subdomains=include_subdomains,
                include_paths=include_paths,
                exclude_paths=exclude_paths,
                ignore_query_parameters=ignore_query_parameters,
                max_urls=max_urls,
            ),
            "sitemap_urls": [],
            "feed_urls": [],
        }
    if root_name == "feed":
        return {
            "kind": "atom",
            "urls": _atom_entry_links(
                root,
                base_url,
                same_domain_only=same_domain_only,
                include_subdomains=include_subdomains,
                include_paths=include_paths,
                exclude_paths=exclude_paths,
                ignore_query_parameters=ignore_query_parameters,
                max_urls=max_urls,
            ),
            "sitemap_urls": [],
            "feed_urls": [],
        }
    return {"kind": "unknown", "urls": [], "sitemap_urls": [], "feed_urls": []}


async def discover_site_index(
    start_url: str,
    *,
    max_urls: int = 200,
    same_domain_only: bool = True,
    include_subdomains: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    ignore_query_parameters: bool = False,
    use_proxy: bool = False,
    timeout: float = 10.0,
) -> dict[str, Any]:
    start_url = urlnorm.canonical_url(start_url)
    if not start_url:
        return {"ok": False, "error": format_error("invalid-url", "Invalid URL provided")}

    max_urls = max(1, min(int(max_urls), 1000))
    start_origin = origin_for(start_url)
    start_domain = urlnorm.domain_of(start_url)
    fetched: list[str] = []
    errors: list[dict[str, str]] = []
    sitemap_urls: list[str] = []
    feed_urls: list[str] = []
    urls: list[str] = []

    async with _client(use_proxy=use_proxy, domain=start_domain, timeout=timeout) as client:
        robots_url = f"{start_origin}/robots.txt"
        robots_text, _, robots_error = await _fetch_text(client, robots_url)
        if robots_error:
            errors.append({"url": robots_url, "error": robots_error})
            if _robots_error_fail_closed(robots_error):
                return {
                    "ok": False,
                    "error": format_error(
                        "blocked-robots",
                        f"robots.txt fetch failed for {start_domain}; fail-closed is enabled",
                    ),
                    "start_url": start_url,
                    "fetched": [],
                    "sitemap_urls": [],
                    "feed_urls": [],
                    "url_count": 0,
                    "sitemap_count": 0,
                    "feed_count": 0,
                    "urls": [],
                    "errors": errors,
                }
        else:
            fetched.append(robots_url)
            sitemap_urls.extend(
                parse_robots_sitemaps(
                    robots_text,
                    start_url,
                    same_domain_only=same_domain_only,
                    include_subdomains=include_subdomains,
                    include_paths=include_paths,
                    exclude_paths=exclude_paths,
                    ignore_query_parameters=ignore_query_parameters,
                    max_urls=20,
                )
            )

        home_text, final_url, home_error = await _fetch_text(client, start_url)
        if home_error:
            errors.append({"url": start_url, "error": home_error})
        else:
            fetched.append(final_url or start_url)
            feed_urls.extend(
                parse_html_feed_links(
                    home_text,
                    final_url or start_url,
                    same_domain_only=same_domain_only,
                    include_subdomains=include_subdomains,
                    include_paths=include_paths,
                    exclude_paths=exclude_paths,
                    ignore_query_parameters=ignore_query_parameters,
                    max_urls=20,
                )
            )

        default_sitemaps = [
            f"{start_origin}/sitemap.xml",
            f"{start_origin}/sitemap_index.xml",
        ]
        sitemap_queue = _dedupe([*sitemap_urls, *default_sitemaps])
        feed_queue = _dedupe(feed_urls)

        child_budget = int(getattr(config, "SITE_INDEX_CHILD_SITEMAPS", 10))
        sitemap_index = 0
        while sitemap_index < len(sitemap_queue) and sitemap_index < 20 + child_budget:
            if len(urls) >= max_urls:
                break
            sitemap_url = sitemap_queue[sitemap_index]
            sitemap_index += 1
            text, final_url, error = await _fetch_text(client, sitemap_url)
            if error:
                errors.append({"url": sitemap_url, "error": error})
                continue
            fetched.append(final_url or sitemap_url)
            parsed = parse_index_document(
                text,
                final_url or sitemap_url,
                same_domain_only=same_domain_only,
                include_subdomains=include_subdomains,
                include_paths=include_paths,
                exclude_paths=exclude_paths,
                ignore_query_parameters=ignore_query_parameters,
                max_urls=max_urls - len(urls),
            )
            urls.extend(parsed["urls"])
            sitemap_urls.extend(parsed["sitemap_urls"])
            if parsed["kind"] == "sitemapindex":
                for child in parsed["sitemap_urls"]:
                    if child_budget <= 0:
                        break
                    if child not in sitemap_queue:
                        sitemap_queue.append(child)
                        child_budget -= 1

        for feed_url in feed_queue[:20]:
            if len(urls) >= max_urls:
                break
            text, final_url, error = await _fetch_text(client, feed_url)
            if error:
                errors.append({"url": feed_url, "error": error})
                continue
            fetched.append(final_url or feed_url)
            parsed = parse_index_document(
                text,
                final_url or feed_url,
                same_domain_only=same_domain_only,
                include_subdomains=include_subdomains,
                include_paths=include_paths,
                exclude_paths=exclude_paths,
                ignore_query_parameters=ignore_query_parameters,
                max_urls=max_urls - len(urls),
            )
            urls.extend(parsed["urls"])

    urls = _dedupe(urls)[:max_urls]
    sitemap_urls = _dedupe(sitemap_urls)
    feed_urls = _dedupe(feed_urls)
    return {
        "ok": True,
        "start_url": start_url,
        "same_domain_only": same_domain_only,
        "fetched": _dedupe(fetched),
        "sitemap_urls": sitemap_urls,
        "feed_urls": feed_urls,
        "url_count": len(urls),
        "sitemap_count": len(sitemap_urls),
        "feed_count": len(feed_urls),
        "urls": urls,
        "errors": errors[:20],
    }


def _robots_error_fail_closed(error: str) -> bool:
    if not error or not getattr(config, "ROBOTS_FAIL_CLOSED", True):
        return False
    if error.startswith("http-"):
        try:
            status = int(error.removeprefix("http-"))
        except ValueError:
            return True
        return status == 429 or status >= 500
    return True


def _client(*, use_proxy: bool, domain: str, timeout: float) -> httpx.AsyncClient:
    proxy_url = proxy_pool.select_proxy(use_proxy, domain=domain)
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": False,
        "headers": {"User-Agent": _USER_AGENT},
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return httpx.AsyncClient(**kwargs)


async def _fetch_text(client: httpx.AsyncClient, url: str) -> tuple[str, str, str]:
    current_url = url
    for _ in range(10):
        blocked, _ = await ssrf.resolve_and_check_async(current_url)
        if blocked:
            return "", current_url, ssrf.block_reason(current_url)

        max_bytes = int(getattr(config, "SITE_INDEX_MAX_BYTES", 1024 * 1024))
        try:
            chunks: list[bytes] = []
            total = 0
            async with client.stream("GET", current_url) as response:
                final_url = str(response.url)
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location")
                    if not location:
                        return "", final_url, f"redirect-{response.status_code}-missing-location"
                    next_url = urljoin(final_url, location)
                    if await ssrf.is_blocked_async(next_url):
                        return "", next_url, ssrf.block_reason(next_url)
                    current_url = next_url
                    continue

                if final_url and await ssrf.is_blocked_async(final_url):
                    return "", final_url, ssrf.block_reason(final_url)
                if response.status_code >= 400:
                    return "", final_url, f"http-{response.status_code}"
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    remaining = max_bytes - total
                    if remaining <= 0:
                        break
                    chunks.append(chunk[:remaining])
                    total += len(chunk[:remaining])
            raw = b"".join(chunks)
            return raw.decode("utf-8", errors="replace"), final_url, ""
        except httpx.HTTPError as e:
            return "", current_url, type(e).__name__
    return "", current_url, "too-many-redirects"


def _xml_loc_values(
    root: ElementTree.Element,
    base_url: str,
    *,
    same_domain_only: bool,
    include_subdomains: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    ignore_query_parameters: bool = False,
    max_urls: int = 200,
    parent_name: str = "",
) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for parent in root.iter():
        if _local_name(parent.tag) != parent_name:
            continue
        for child in parent:
            if _local_name(child.tag) != "loc":
                continue
            normalized = _normalize_candidate(
                child.text or "",
                base_url,
                same_domain_only,
                include_subdomains=include_subdomains,
                include_paths=include_paths,
                exclude_paths=exclude_paths,
                ignore_query_parameters=ignore_query_parameters,
            )
            if normalized and normalized not in seen:
                seen.add(normalized)
                urls.append(normalized)
            if len(urls) >= max_urls:
                return urls
    return urls


def _rss_item_links(
    root: ElementTree.Element,
    base_url: str,
    *,
    same_domain_only: bool,
    include_subdomains: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    ignore_query_parameters: bool = False,
    max_urls: int = 200,
) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for item in root.iter():
        if _local_name(item.tag) != "item":
            continue
        for child in item:
            if _local_name(child.tag) != "link":
                continue
            normalized = _normalize_candidate(
                child.text or "",
                base_url,
                same_domain_only,
                include_subdomains=include_subdomains,
                include_paths=include_paths,
                exclude_paths=exclude_paths,
                ignore_query_parameters=ignore_query_parameters,
            )
            if normalized and normalized not in seen:
                seen.add(normalized)
                urls.append(normalized)
            if len(urls) >= max_urls:
                return urls
    return urls


def _atom_entry_links(
    root: ElementTree.Element,
    base_url: str,
    *,
    same_domain_only: bool,
    include_subdomains: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    ignore_query_parameters: bool = False,
    max_urls: int = 200,
) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for entry in root.iter():
        if _local_name(entry.tag) != "entry":
            continue
        for child in entry:
            if _local_name(child.tag) != "link":
                continue
            href = child.attrib.get("href") or child.text or ""
            normalized = _normalize_candidate(
                href,
                base_url,
                same_domain_only,
                include_subdomains=include_subdomains,
                include_paths=include_paths,
                exclude_paths=exclude_paths,
                ignore_query_parameters=ignore_query_parameters,
            )
            if normalized and normalized not in seen:
                seen.add(normalized)
                urls.append(normalized)
            if len(urls) >= max_urls:
                return urls
    return urls


def _normalize_candidate(
    raw_url: str,
    base_url: str,
    same_domain_only: bool,
    *,
    include_subdomains: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    ignore_query_parameters: bool = False,
) -> str:
    policy = CrawlPolicy.from_options(
        base_url,
        same_domain_only=same_domain_only,
        include_subdomains=include_subdomains,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        ignore_query_parameters=ignore_query_parameters,
    )
    return policy.normalize_page_url(raw_url, base_url=base_url)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
