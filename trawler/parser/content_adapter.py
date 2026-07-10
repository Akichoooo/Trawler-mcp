"""Deterministic adapters for non-HTML text responses."""

from __future__ import annotations

import json
from typing import Any

from trawler import config
from trawler.site_index import parse_index_document


def adapt_text_response(text: str, url: str = "") -> str:
    source = (text or "").strip()
    if not source:
        return ""

    json_md = _adapt_json(source)
    if json_md:
        return json_md

    feed_md = _adapt_feed(source, url)
    if feed_md:
        return feed_md

    if _looks_like_plain_text(source):
        return _truncate_text(source)

    return ""


def _adapt_json(source: str) -> str:
    if not source.startswith(("{", "[")):
        return ""
    try:
        parsed = json.loads(source)
    except json.JSONDecodeError:
        return ""
    pretty = json.dumps(_bounded_json(parsed), ensure_ascii=False, indent=2)
    return "# JSON response\n\n```json\n" + _truncate_text(pretty) + "\n```"


def _adapt_feed(source: str, url: str) -> str:
    if not source.startswith("<"):
        return ""
    parsed = parse_index_document(source, url or "https://example.invalid/")
    if parsed["kind"] not in {"rss", "atom"} or not parsed["urls"]:
        return ""
    lines = [f"# {parsed['kind'].upper()} feed", ""]
    lines.extend(f"- {item}" for item in parsed["urls"])
    return "\n".join(lines)


def _looks_like_plain_text(source: str) -> bool:
    if source.startswith("<"):
        return False
    tagish = source.count("<") + source.count(">")
    return tagish <= max(2, len(source) // 500)


def _truncate_text(text: str) -> str:
    limit = min(int(getattr(config, "HTML_TRUNCATE", 2 * 1024 * 1024)), 100_000)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[truncated]"


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return "..."
    if isinstance(value, dict):
        items = list(value.items())[:200]
        bounded = {str(key): _bounded_json(item, depth=depth + 1) for key, item in items}
        if len(value) > len(items):
            bounded["..."] = f"{len(value) - len(items)} more keys"
        return bounded
    if isinstance(value, list):
        items = value[:200]
        bounded = [_bounded_json(item, depth=depth + 1) for item in items]
        if len(value) > len(items):
            bounded.append(f"... {len(value) - len(items)} more items")
        return bounded
    return value
