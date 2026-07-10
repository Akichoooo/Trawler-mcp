"""User-facing page retrieval orchestration.

This module gives MCP callers one deep interface for a single-page retrieval
task. It translates product intent (standard fetch, authorized browser access,
human assist, selector extraction, screenshots) into the lower-level crawl_url
pipeline while preserving the existing raw/artifact provenance contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trawler.artifacts import read_artifact_screenshot
from trawler.crawl_url import crawl_url
from trawler.errors import format_error
from trawler.structured import crawl_result_payload

ACCESS_MODES = {"standard", "user_authorized"}
HUMAN_ASSIST_MODES = {"auto", "required", "off"}
EXTRACT_MODES = {"page", "selector", "visible_text", "screenshot"}


@dataclass
class PageRetrievalResult:
    legacy_text: str
    structured: dict[str, Any]
    screenshot: bytes | None = None
    screenshot_error: str = ""
    warnings: list[str] = field(default_factory=list)


def _invalid_mode(message: str, *, url: str) -> PageRetrievalResult:
    legacy_text = format_error("invalid-mode", message)
    payload = crawl_result_payload(
        input_url=url,
        result=legacy_text,
        cache_mode="disabled",
        force_refresh=False,
        mode="full",
    ).model_dump(mode="json")
    payload.update({"access_mode": "", "human_assist": "", "extract_mode": ""})
    return PageRetrievalResult(legacy_text=legacy_text, structured=payload)


def effective_cache_mode(
    *,
    access_mode: str,
    extract_mode: str,
    cache_mode: str,
) -> str:
    """Avoid stale cache reads for modes that promise a fresh browser visit."""
    if cache_mode == "enabled" and access_mode == "user_authorized":
        return "write_only"
    if cache_mode in {"enabled", "read_only"} and extract_mode == "screenshot":
        return "write_only"
    return cache_mode


async def retrieve_page(
    url: str,
    *,
    access_mode: str = "standard",
    account_id: str = "",
    human_assist: str = "auto",
    extract_mode: str = "page",
    selector: str = "",
    use_proxy: bool = False,
    cache_mode: str = "enabled",
    timeout: int = 120,
) -> PageRetrievalResult:
    if access_mode not in ACCESS_MODES:
        return _invalid_mode(f"Unsupported access_mode: {access_mode}", url=url)
    if human_assist not in HUMAN_ASSIST_MODES:
        return _invalid_mode(f"Unsupported human_assist: {human_assist}", url=url)
    if extract_mode not in EXTRACT_MODES:
        return _invalid_mode(f"Unsupported extract_mode: {extract_mode}", url=url)
    if extract_mode == "selector" and not selector.strip():
        return _invalid_mode("extract_mode='selector' requires selector", url=url)

    effective_cache = effective_cache_mode(
        access_mode=access_mode,
        extract_mode=extract_mode,
        cache_mode=cache_mode,
    )
    capture_artifact = extract_mode == "screenshot"
    result = await crawl_url(
        url,
        use_proxy=use_proxy,
        cache_mode=effective_cache,
        user_authorized_access=access_mode == "user_authorized",
        account_id=account_id,
        human_assist=human_assist,
        selector=selector if extract_mode == "selector" else "",
        capture_artifact=capture_artifact,
        timeout=timeout,
    )
    payload = crawl_result_payload(
        input_url=url,
        result=result,
        cache_mode=effective_cache,
        force_refresh=False,
        mode="full",
    ).model_dump(mode="json")
    payload.update(
        {
            "access_mode": access_mode,
            "account_id": account_id,
            "human_assist": human_assist,
            "extract_mode": extract_mode,
            "selector": selector if extract_mode == "selector" else "",
        }
    )

    warnings: list[str] = []
    screenshot: bytes | None = None
    screenshot_error = ""
    artifact_id = str(payload.get("artifact_id") or "")
    if capture_artifact:
        if artifact_id:
            try:
                screenshot = read_artifact_screenshot(artifact_id)
            except Exception as e:
                screenshot_error = f"screenshot unavailable for artifact {artifact_id}: {e}"
        else:
            screenshot_error = "screenshot unavailable: no artifact_id was produced"
        if screenshot_error:
            warnings.append(screenshot_error)
    if warnings:
        payload["warnings"] = warnings
    if screenshot_error:
        payload["screenshot_error"] = screenshot_error

    return PageRetrievalResult(
        legacy_text=result,
        structured=payload,
        screenshot=screenshot,
        screenshot_error=screenshot_error,
        warnings=warnings,
    )
