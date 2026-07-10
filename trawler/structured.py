"""Structured MCP result helpers built on Trawler's legacy string contract."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field

from trawler import urlnorm
from trawler.errors import ERROR_PREFIX, is_error, is_ok, unwrap_ok
from trawler.raw_store import read_metadata
from trawler.seen import url_id


class StructuredCrawlResult(BaseModel):
    ok: bool
    text: str = ""
    error: dict[str, Any] | None = None
    raw_id: str = ""
    url: str = ""
    canonical_url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifact_id: str = ""
    cache_mode: str = ""
    mode: str = ""
    links: list[Any] = Field(default_factory=list)
    link_count: int = 0


class StructuredMapResult(BaseModel):
    ok: bool
    text: str = ""
    error: dict[str, Any] | None = None
    url: str = ""
    canonical_url: str = ""
    links: list[Any] = Field(default_factory=list)
    link_count: int = 0


class StructuredCrawlJobResult(BaseModel):
    ok: bool
    text: str = ""
    error: dict[str, Any] | None = None
    job_id: str = ""
    status: str = ""
    max_pages: int = 0
    seed_count: int = 0
    discovered_url_count: int = 0
    sitemap_count: int = 0
    feed_count: int = 0
    policy: dict[str, Any] = Field(default_factory=dict)


class StructuredJobStatusResult(BaseModel):
    ok: bool
    text: str = ""
    error: dict[str, Any] | None = None
    job_id: str = ""
    status: str = ""
    completed: int = 0
    total: int = 0
    updated_at: str = ""
    frontier: dict[str, Any] = Field(default_factory=dict)


class StructuredJobItemsResult(BaseModel):
    ok: bool
    text: str = ""
    error: dict[str, Any] | None = None
    job_id: str = ""
    items: list[Any] = Field(default_factory=list)
    next_cursor: int | None = None
    count: int = 0


class StructuredItemsResult(BaseModel):
    ok: bool
    text: str = ""
    error: dict[str, Any] | None = None
    items: list[Any] = Field(default_factory=list)
    count: int = 0


class StructuredRawMetadataResult(BaseModel):
    ok: bool
    text: str = ""
    error: dict[str, Any] | None = None
    raw_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class StructuredArtifactSummaryResult(BaseModel):
    ok: bool
    text: str = ""
    error: dict[str, Any] | None = None
    artifact_id: str = ""
    summary: dict[str, Any] = Field(default_factory=dict)


class StructuredSiteIndexResult(BaseModel):
    ok: bool
    text: str = ""
    error: dict[str, Any] | None = None
    start_url: str = ""
    urls: list[str] = Field(default_factory=list)
    sitemap_urls: list[str] = Field(default_factory=list)
    feed_urls: list[str] = Field(default_factory=list)
    url_count: int = 0
    sitemap_count: int = 0
    feed_count: int = 0
    errors: list[Any] = Field(default_factory=list)


def call_tool_result(legacy_text: str, structured: BaseModel) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=legacy_text)],
        structuredContent=structured.model_dump(mode="json"),
        isError=False,
    )


def crawl_result_to_call_result(
    *,
    input_url: str,
    result: str,
    cache_mode: str,
    force_refresh: bool,
    mode: str,
) -> CallToolResult:
    return call_tool_result(
        result,
        crawl_result_payload(
            input_url=input_url,
            result=result,
            cache_mode=cache_mode,
            force_refresh=force_refresh,
            mode=mode,
        ),
    )


def map_result_to_call_result(
    *,
    start_url: str,
    legacy_text: str,
    result: dict[str, Any],
) -> CallToolResult:
    return call_tool_result(
        legacy_text,
        map_result_payload(start_url=start_url, legacy_text=legacy_text, result=result),
    )


def crawl_result_payload(
    *,
    input_url: str,
    result: str,
    cache_mode: str,
    force_refresh: bool,
    mode: str,
) -> StructuredCrawlResult:
    canonical_url = urlnorm.canonical_url(input_url) or ""
    effective_cache_mode = _effective_cache_mode(cache_mode, force_refresh)

    if is_ok(result):
        raw_id, metadata = _metadata_for_url(canonical_url)
        links, link_count = _links_from_metadata(metadata)
        artifact_id = metadata.get("artifact_id")
        return StructuredCrawlResult(
            ok=True,
            text=unwrap_ok(result),
            raw_id=raw_id,
            url=str(metadata.get("url") or canonical_url or input_url),
            canonical_url=canonical_url,
            metadata=metadata,
            artifact_id=artifact_id if isinstance(artifact_id, str) else "",
            cache_mode=effective_cache_mode,
            mode=mode,
            links=links,
            link_count=link_count,
        )

    error = _error_payload(result)
    raw_id, metadata = _metadata_for_url(canonical_url, prefer_blocked=True)
    artifact_id = error.get("artifact_id")
    return StructuredCrawlResult(
        ok=False,
        text="",
        error=error,
        raw_id=raw_id,
        url=canonical_url or input_url,
        canonical_url=canonical_url,
        metadata=metadata,
        artifact_id=artifact_id if isinstance(artifact_id, str) else "",
        cache_mode=effective_cache_mode,
        mode=mode,
    )


def map_result_payload(
    *,
    start_url: str,
    legacy_text: str,
    result: dict[str, Any],
) -> StructuredMapResult:
    canonical_url = urlnorm.canonical_url(start_url) or ""
    if result.get("ok"):
        links = result.get("links")
        links = links if isinstance(links, list) else []
        raw_count = result.get("link_count")
        return StructuredMapResult(
            ok=True,
            text=unwrap_ok(legacy_text) if is_ok(legacy_text) else legacy_text,
            url=str(result.get("url") or canonical_url or start_url),
            canonical_url=canonical_url,
            links=links,
            link_count=raw_count if isinstance(raw_count, int) else len(links),
        )

    error_text = str(result.get("error") or legacy_text)
    return StructuredMapResult(
        ok=False,
        text="",
        error=_error_payload(error_text),
        url=canonical_url or start_url,
        canonical_url=canonical_url,
        links=[],
        link_count=0,
    )


def error_payload(result: str) -> dict[str, Any]:
    return _error_payload(result)


def _error_payload(result: str) -> dict[str, Any]:
    if is_error(result):
        try:
            parsed = json.loads(result[len(ERROR_PREFIX):])
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            return parsed
    return {
        "errorType": "internal-error",
        "message": result,
        "retryable": False,
        "suggestedAction": "abort",
    }


def _effective_cache_mode(cache_mode: str, force_refresh: bool) -> str:
    if force_refresh and cache_mode == "enabled":
        return "write_only"
    return cache_mode


def _metadata_for_url(
    canonical_url: str,
    *,
    prefer_blocked: bool = False,
) -> tuple[str, dict[str, Any]]:
    if not canonical_url:
        return "", {}
    raw_id = url_id(canonical_url)
    try:
        metadata = read_metadata(raw_id, prefer_blocked=prefer_blocked)
    except Exception:
        metadata = {}
    return raw_id, metadata


def _links_from_metadata(metadata: dict[str, Any]) -> tuple[list[Any], int]:
    raw_links = metadata.get("links")
    links = raw_links if isinstance(raw_links, list) else []
    raw_count = metadata.get("link_count")
    link_count = raw_count if isinstance(raw_count, int) else len(links)
    return links, link_count
