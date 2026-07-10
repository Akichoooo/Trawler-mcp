"""Compact markdown and extract lightweight citations for agent context."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from trawler import urlnorm

LINK_RE = re.compile(r"(?<!!)\[([^\]\n]{1,240})\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
BLANK_RE = re.compile(r"\n{3,}")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass
class FitMarkdownResult:
    markdown: str
    citations: list[dict[str, str]]
    original_chars: int
    output_chars: int
    truncated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "markdown": self.markdown,
            "citations": self.citations,
            "original_chars": self.original_chars,
            "output_chars": self.output_chars,
            "truncated": self.truncated,
        }


def clean_markdown(markdown: str) -> str:
    text = CONTROL_RE.sub("", str(markdown or ""))
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return BLANK_RE.sub("\n\n", "\n".join(lines)).strip()


def extract_citations(
    markdown: str,
    *,
    base_url: str = "",
    max_citations: int = 100,
) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in LINK_RE.finditer(markdown or ""):
        label = re.sub(r"\s+", " ", match.group(1)).strip()
        raw_url = match.group(2).strip()
        if raw_url.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        absolute = urljoin(base_url, raw_url) if base_url else raw_url
        try:
            parts = urlsplit(absolute)
        except ValueError:
            continue
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            continue
        normalized = urlnorm.canonical_url(absolute) or absolute
        if normalized in seen:
            continue
        seen.add(normalized)
        citations.append({"label": label[:200], "url": normalized})
        if len(citations) >= max_citations:
            break
    return citations


def _truncate_at_boundary(markdown: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(markdown) <= max_chars:
        return markdown, False
    marker = "\n\n[...truncated...]"
    limit = max(0, max_chars - len(marker))
    candidate = markdown[:limit]
    boundary = max(candidate.rfind("\n\n"), candidate.rfind("\n#"), candidate.rfind("\n- "))
    if boundary > max_chars * 0.55:
        candidate = candidate[:boundary]
    return candidate.rstrip() + marker, True


def fit_markdown(
    markdown: str,
    *,
    max_chars: int = 20000,
    base_url: str = "",
    max_citations: int = 100,
) -> FitMarkdownResult:
    original = str(markdown or "")
    cleaned = clean_markdown(original)
    fitted, truncated = _truncate_at_boundary(cleaned, max_chars)
    citations = extract_citations(fitted, base_url=base_url, max_citations=max_citations)
    return FitMarkdownResult(
        markdown=fitted,
        citations=citations,
        original_chars=len(original),
        output_chars=len(fitted),
        truncated=truncated,
    )
