"""CSS selector based deterministic HTML cropping."""

from __future__ import annotations

from typing import Any


def apply_selectors(html: str, selectors: list[str]) -> tuple[str, dict[str, Any]]:
    """Return HTML composed from selector matches, or the original HTML on miss."""
    normalized = _normalize_selectors(selectors)
    report: dict[str, Any] = {
        "selectors": normalized,
        "selector_used": "",
        "selector_match_count": 0,
        "selector_errors": [],
    }
    if not html or not normalized:
        return html, report

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        report["selector_errors"] = ["beautifulsoup4 unavailable"]
        return html, report

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        report["selector_errors"] = [str(e)]
        return html, report

    selected: list[str] = []
    used: list[str] = []
    errors: list[str] = []
    for selector in normalized:
        try:
            matches = soup.select(selector)
        except Exception as e:
            errors.append(f"{selector}: {type(e).__name__}")
            continue
        if not matches:
            continue
        used.append(selector)
        selected.extend(str(match) for match in matches)

    report["selector_errors"] = errors
    if not selected:
        return html, report

    report["selector_used"] = ", ".join(used)
    report["selector_match_count"] = len(selected)
    return "<html><body>\n" + "\n".join(selected) + "\n</body></html>", report


def _normalize_selectors(selectors: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        if not isinstance(selector, str):
            continue
        selector = selector.strip()
        if not selector or len(selector) > 500 or selector in seen:
            continue
        seen.add(selector)
        result.append(selector)
        if len(result) >= 20:
            break
    return result
