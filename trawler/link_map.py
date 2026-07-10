"""DOM link discovery for crawl frontiers.

This module keeps frontier discovery separate from markdown extraction. The
reader sees cleaned markdown; the crawler schedules from raw DOM links.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

from trawler import urlnorm


def extract_links(
    html: str,
    base_url: str,
    *,
    same_domain_only: bool = False,
    start_domain: str = "",
    max_links: int = 500,
) -> list[dict[str, object]]:
    """Extract normalized HTTP(S) links from raw HTML."""
    if not html or not base_url:
        return []

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    base_domain = start_domain or urlnorm.domain_of(base_url)
    seen: set[str] = set()
    links: list[dict[str, object]] = []

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("a", href=True):
        href = str(tag.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue

        absolute = urljoin(base_url, href)
        try:
            parts = urlsplit(absolute)
        except ValueError:
            continue
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            continue

        normalized = urlnorm.canonical_url(absolute)
        if not normalized or normalized in seen:
            continue

        domain = urlnorm.domain_of(normalized)
        same_domain = domain == base_domain
        if same_domain_only and not same_domain:
            continue

        text = tag.get_text(" ", strip=True)
        title = str(tag.get("title") or "").strip()
        rel = tag.get("rel") or []
        if isinstance(rel, str):
            rel_values = [rel]
        else:
            rel_values = [str(item) for item in rel]

        seen.add(normalized)
        links.append(
            {
                "url": normalized,
                "text": text[:200],
                "title": title[:200],
                "rel": rel_values,
                "same_domain": same_domain,
            }
        )
        if len(links) >= max_links:
            break

    return links
