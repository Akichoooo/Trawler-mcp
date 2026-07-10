import json

import pytest


@pytest.mark.asyncio
async def test_public_mcp_tool_names_and_policy_schema():
    from trawler import server

    tools = await server.mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}

    for name in {
        "crawl_site",
        "crawl_site_structured",
        "crawl_site_indexed_structured",
        "get_site_profile",
        "get_retrieval_readiness",
        "get_policy_decision",
        "map_site",
        "wait_for_job",
        "get_job_status",
        "get_job_status_structured",
        "cancel_job",
        "get_job_errors",
        "get_job_errors_structured",
        "get_job_results",
        "get_job_results_structured",
        "wait_for_job_structured",
        "get_artifact_summary",
        "get_artifact_summary_structured",
        "get_artifact_screenshot",
        "list_raw_structured",
        "get_raw_metadata_structured",
        "list_artifacts_structured",
        "discover_site_index_structured",
        "retrieve_page",
        "register_account_profile",
        "list_account_profiles",
        "mark_account_profile",
        "open_browser_session",
        "connect_browser_session",
        "list_browser_sessions",
        "run_browser_actions",
        "observe_browser_session",
        "start_element_picker",
        "start_region_picker",
        "extract_browser_session",
        "close_browser_session",
    }:
        assert name in by_name
        assert f"{name}_tool" not in by_name

    crawl_schema = by_name["crawl_site"].inputSchema
    properties = crawl_schema["properties"]
    for field in {
        "max_depth",
        "include_paths",
        "exclude_paths",
        "include_subdomains",
        "ignore_query_parameters",
    }:
        assert field in properties

    structured_schema = by_name["crawl_site_structured"].inputSchema
    for field in {
        "max_depth",
        "include_paths",
        "exclude_paths",
        "include_subdomains",
        "ignore_query_parameters",
    }:
        assert field in structured_schema["properties"]

    crawl_url_schema = by_name["crawl_url"].inputSchema
    assert "user_authorized_access" in crawl_url_schema["properties"]
    assert "account_id" in crawl_url_schema["properties"]
    assert "human_assist" in crawl_url_schema["properties"]
    assert "selector" in crawl_url_schema["properties"]
    crawl_url_structured_schema = by_name["crawl_url_structured"].inputSchema
    assert "user_authorized_access" in crawl_url_structured_schema["properties"]
    assert "account_id" in crawl_url_structured_schema["properties"]
    retrieve_page_schema = by_name["retrieve_page"].inputSchema
    for field in {"access_mode", "account_id", "human_assist", "extract_mode", "selector"}:
        assert field in retrieve_page_schema["properties"]
    register_account_schema = by_name["register_account_profile"].inputSchema
    for field in {"domain", "account_id", "label", "login_method", "notes", "make_default"}:
        assert field in register_account_schema["properties"]
    mark_account_schema = by_name["mark_account_profile"].inputSchema
    for field in {"domain", "account_id", "status", "notes", "expires_at", "risk_flags"}:
        assert field in mark_account_schema["properties"]
    assert "domain" in by_name["list_account_profiles"].inputSchema["properties"]
    readiness_schema = by_name["get_retrieval_readiness"].inputSchema
    for field in {"target", "account_id", "access_mode"}:
        assert field in readiness_schema["properties"]
    policy_schema = by_name["get_policy_decision"].inputSchema
    for field in {
        "tool",
        "target_url",
        "domain",
        "access_mode",
        "requested_pages",
        "uses_live_browser",
        "uses_cdp",
        "reads_artifact_body",
        "capture_artifact",
    }:
        assert field in policy_schema["properties"]
    open_browser_schema = by_name["open_browser_session"].inputSchema
    for field in {"url", "account_id", "access_mode", "wait_until"}:
        assert field in open_browser_schema["properties"]
    connect_browser_schema = by_name["connect_browser_session"].inputSchema
    for field in {"cdp_url", "url", "account_id", "access_mode", "wait_until"}:
        assert field in connect_browser_schema["properties"]
    run_actions_schema = by_name["run_browser_actions"].inputSchema
    for field in {"session_id", "actions", "wait_until", "timeout"}:
        assert field in run_actions_schema["properties"]
    observe_schema = by_name["observe_browser_session"].inputSchema
    for field in {"session_id", "selector", "max_elements", "include_accessibility"}:
        assert field in observe_schema["properties"]
    extract_browser_schema = by_name["extract_browser_session"].inputSchema
    for field in {
        "session_id",
        "extract_mode",
        "selector",
        "actions",
        "action_timeout",
        "wait_until",
        "max_markdown_chars",
        "close_after",
    }:
        assert field in extract_browser_schema["properties"]
    assert "session_id" in by_name["start_element_picker"].inputSchema["properties"]
    assert "session_id" in by_name["start_region_picker"].inputSchema["properties"]


@pytest.mark.asyncio
async def test_crawl_site_tool_forwards_policy(monkeypatch):
    from trawler import server

    captured = {}

    async def fake_crawl_site(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"job_id": "job-1", "max_pages": 3, "seed_count": 0}

    monkeypatch.setattr(server, "crawl_site", fake_crawl_site)

    result = await server.crawl_site_tool(
        "https://example.com/",
        max_pages=3,
        max_depth=2,
        include_paths=["/docs/*"],
        exclude_paths=["/docs/private/*"],
        include_subdomains=True,
        ignore_query_parameters=True,
    )

    assert result.startswith("__TRAWLER_OK__:")
    assert captured["args"] == ("https://example.com/",)
    assert captured["kwargs"]["max_depth"] == 2
    assert captured["kwargs"]["include_paths"] == ["/docs/*"]
    assert captured["kwargs"]["exclude_paths"] == ["/docs/private/*"]
    assert captured["kwargs"]["include_subdomains"] is True
    assert captured["kwargs"]["ignore_query_parameters"] is True


@pytest.mark.asyncio
async def test_retrieve_page_returns_screenshot_image(monkeypatch):
    from trawler import server
    from trawler.errors import format_ok
    from trawler.page_retrieval import PageRetrievalResult

    async def fake_retrieve_page(*args, **kwargs):
        return PageRetrievalResult(
            legacy_text=format_ok("# OK"),
            structured={"ok": True, "artifact_id": "art-1"},
            screenshot=b"\x89PNG\r\n\x1a\nfake",
        )

    monkeypatch.setattr(server, "_retrieve_page", fake_retrieve_page)

    result = await server.retrieve_page(
        "https://example.com/",
        extract_mode="screenshot",
    )

    assert result.structuredContent["ok"] is True
    assert result.content[0].text == format_ok("# OK")
    assert result.content[1].type == "image"
    assert result.content[1].mimeType == "image/png"


@pytest.mark.asyncio
async def test_extract_browser_session_returns_screenshot_image(monkeypatch):
    from trawler import server
    from trawler.errors import format_ok
    from trawler.live_browser import LiveBrowserExtraction

    async def fake_extract_browser_session(*args, **kwargs):
        return LiveBrowserExtraction(
            legacy_text=format_ok("screenshot"),
            structured={"ok": True, "session_id": "live-1"},
            screenshot=b"\x89PNG\r\n\x1a\nfake",
        )

    monkeypatch.setattr(server, "_extract_browser_session", fake_extract_browser_session)

    result = await server.extract_browser_session(
        "live-1",
        extract_mode="screenshot",
    )

    assert result.structuredContent["ok"] is True
    assert result.content[0].text == format_ok("screenshot")
    assert result.content[1].type == "image"
    assert result.content[1].mimeType == "image/png"


@pytest.mark.asyncio
async def test_run_browser_actions_tool_forwards(monkeypatch):
    from trawler import server
    from trawler.errors import format_ok

    captured = {}

    async def fake_actions(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return format_ok('{"ok": true}')

    monkeypatch.setattr(server, "_perform_browser_actions", fake_actions)

    result = await server.run_browser_actions(
        "live-1",
        [{"type": "click", "selector": "button"}],
        wait_until="load",
        timeout=12,
    )

    assert result.startswith("__TRAWLER_OK__:")
    assert captured["args"] == ("live-1", [{"type": "click", "selector": "button"}])
    assert captured["kwargs"] == {"wait_until": "load", "timeout": 12}


@pytest.mark.asyncio
async def test_get_policy_decision_tool_reports_denial(monkeypatch):
    from trawler import config, server

    monkeypatch.setattr(config, "BLOCKED_DOMAINS", "example.com")

    result = await server.get_policy_decision(
        "retrieve_page",
        target_url="https://example.com/private",
    )

    assert result.startswith("__TRAWLER_OK__:")
    payload = json.loads(result[len("__TRAWLER_OK__:\n\n"):])
    assert payload["allowed"] is False
    assert "blocked_domain" in payload["reasons"]


@pytest.mark.asyncio
async def test_open_browser_session_denied_when_live_browser_disabled(monkeypatch):
    from trawler import config, server

    async def should_not_open(*args, **kwargs):
        raise AssertionError("policy should deny before opening browser")

    monkeypatch.setattr(config, "ENABLE_LIVE_BROWSER", False)
    monkeypatch.setattr(server, "_open_browser_session", should_not_open)

    result = await server.open_browser_session("https://example.com/")

    assert result.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(result[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "permission-denied"
    assert "live_browser_disabled" in payload["policy_decision"]["reasons"]


@pytest.mark.asyncio
async def test_crawl_site_denied_when_disabled(monkeypatch):
    from trawler import config, server

    async def should_not_crawl(*args, **kwargs):
        raise AssertionError("policy should deny before crawl_site")

    monkeypatch.setattr(config, "ENABLE_CRAWL_SITE", False)
    monkeypatch.setattr(server, "crawl_site", should_not_crawl)

    result = await server.crawl_site_tool("https://example.com/", max_pages=2)

    assert result.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(result[len("__TRAWLER_ERROR__:"):])
    assert "crawl_site_disabled" in payload["policy_decision"]["reasons"]


@pytest.mark.asyncio
async def test_crawl_site_structured_returns_job_payload(monkeypatch):
    from trawler import server

    async def fake_crawl_site(*args, **kwargs):
        return {"job_id": "job-structured", "status": "crawling", "max_pages": 2, "seed_count": 0}

    monkeypatch.setattr(server, "crawl_site", fake_crawl_site)

    result = await server.crawl_site_structured(
        "https://example.com/",
        max_pages=2,
        include_paths=["/docs/*"],
    )

    assert result.content[0].text.startswith("__TRAWLER_OK__:")
    assert result.structuredContent is not None
    assert result.structuredContent["ok"] is True
    assert result.structuredContent["job_id"] == "job-structured"
    assert result.structuredContent["policy"]["include_paths"] == ["/docs/*"]


@pytest.mark.asyncio
async def test_crawl_site_indexed_forwards_policy_to_discovery_and_crawl(monkeypatch):
    from trawler import server

    discovered_kwargs = {}
    crawl_kwargs = {}

    async def fake_discover(*args, **kwargs):
        discovered_kwargs.update(kwargs)
        return {
            "ok": True,
            "urls": ["https://docs.example.com/docs/a?version=1"],
            "url_count": 1,
            "sitemap_count": 1,
            "feed_count": 0,
        }

    async def fake_crawl_site(*args, **kwargs):
        crawl_kwargs.update(kwargs)
        return {"job_id": "job-idx", "max_pages": 5, "seed_count": 1}

    monkeypatch.setattr(server, "_discover_site_index", fake_discover)
    monkeypatch.setattr(server, "crawl_site", fake_crawl_site)

    result = await server.crawl_site_indexed(
        "https://example.com/",
        max_pages=5,
        include_paths=["/docs/*"],
        exclude_paths=["/docs/private/*"],
        include_subdomains=True,
        ignore_query_parameters=True,
    )

    payload = json.loads(result[len("__TRAWLER_OK__:\n\n"):])
    assert payload["job_id"] == "job-idx"
    for target in (discovered_kwargs, crawl_kwargs):
        assert target["include_paths"] == ["/docs/*"]
        assert target["exclude_paths"] == ["/docs/private/*"]
        assert target["include_subdomains"] is True
        assert target["ignore_query_parameters"] is True


@pytest.mark.asyncio
async def test_job_result_tools_return_job_not_found_for_unknown_job(tmp_db):
    from trawler import server

    errors = await server.get_job_errors_tool("missing-job")
    results = await server.get_job_results_tool("missing-job")

    assert errors.startswith("__TRAWLER_ERROR__:")
    assert '"errorType": "job-not-found"' in errors
    assert results.startswith("__TRAWLER_ERROR__:")
    assert '"errorType": "job-not-found"' in results


@pytest.mark.asyncio
async def test_structured_job_inspection_tools_return_payloads(tmp_db):
    from trawler import db, frontier, jobs, server

    conn = db.connect()
    try:
        job_id = jobs.create_job(conn, "https://example.com/", total=2)
        frontier.enqueue(conn, job_id, "https://example.com/ok")
        frontier.mark_error(conn, job_id, "https://example.com/ok", "__TRAWLER_ERROR__:boom")
        jobs.update_progress(conn, job_id, visited=["https://example.com/ok"], queue=[], completed=1)
    finally:
        conn.close()

    status = await server.get_job_status_structured(job_id)
    errors = await server.get_job_errors_structured(job_id)
    results = await server.get_job_results_structured(job_id)

    assert status.structuredContent["ok"] is True
    assert status.structuredContent["job_id"] == job_id
    assert errors.structuredContent["count"] == 1
    assert errors.structuredContent["items"][0]["url"] == "https://example.com/ok"
    assert results.structuredContent["count"] == 1
    assert results.structuredContent["items"][0]["status"] == "error"


@pytest.mark.asyncio
async def test_mcp_call_tool_get_artifact_summary(tmp_path, monkeypatch):
    from trawler import artifacts, config, server

    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(config, "DEBUG_ARTIFACTS", "always")
    artifact_id = artifacts.save_artifact(
        url="https://example.com/",
        reason="mcp-smoke",
        html="<html>large body</html>",
    )

    content, metadata = await server.mcp.call_tool(
        "get_artifact_summary",
        {"artifact_id": artifact_id},
    )

    assert metadata["result"].startswith("__TRAWLER_OK__:")
    text = content[0].text
    payload = json.loads(text[len("__TRAWLER_OK__:\n\n"):])
    assert payload["artifact_id"] == artifact_id
    assert payload["reason"] == "mcp-smoke"
    assert "large body" not in text


@pytest.mark.asyncio
async def test_get_raw_returns_error_for_invalid_raw_id():
    from trawler import server

    result = await server.get_raw("bad:id")

    assert result.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(result[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "invalid-url"


@pytest.mark.asyncio
async def test_get_artifact_page_body_requires_explicit_opt_in(tmp_path, monkeypatch):
    from trawler import artifacts, config, server

    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(config, "DEBUG_ARTIFACTS", "always")
    monkeypatch.setattr(config, "EXPOSE_ARTIFACT_BODIES", False)
    artifact_id = artifacts.save_artifact(
        url="https://example.com/",
        reason="body-gate",
        html="<html>SECRET_TOKEN</html>",
    )

    result = await server.get_artifact(artifact_id, "page.html")

    assert result.startswith("__TRAWLER_ERROR__:")
    assert "SECRET_TOKEN" not in result
    payload = json.loads(result[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "permission-denied"


@pytest.mark.asyncio
async def test_get_artifact_screenshot_returns_image_content(tmp_path, monkeypatch):
    from trawler import artifacts, config, server

    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(config, "DEBUG_ARTIFACTS", "always")
    artifact_id = artifacts.save_artifact(
        url="https://example.com/",
        reason="screenshot",
        screenshot=b"\x89PNG\r\n\x1a\nfake",
    )

    result = await server.get_artifact_screenshot(artifact_id)

    assert result.structuredContent["ok"] is True
    assert result.structuredContent["artifact_id"] == artifact_id
    assert result.content[0].text.startswith("__TRAWLER_OK__:")
    assert result.content[1].type == "image"
    assert result.content[1].mimeType == "image/png"


@pytest.mark.asyncio
async def test_structured_raw_and_artifact_helpers(tmp_path, tmp_db, monkeypatch):
    from trawler import artifacts, config, server
    from trawler.raw_store import save_raw

    save_raw(
        "raw-1",
        url="https://example.com/",
        final_url="https://example.com/",
        title="Example",
        markdown="# Example",
        gear_used="curl_cffi",
    )
    raw_meta = await server.get_raw_metadata_structured("raw-1")
    assert raw_meta.structuredContent["ok"] is True
    assert raw_meta.structuredContent["raw_id"] == "raw-1"

    list_raw = await server.list_raw_structured()
    assert list_raw.structuredContent["count"] >= 1

    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(config, "DEBUG_ARTIFACTS", "always")
    artifact_id = artifacts.save_artifact(url="https://example.com/", reason="structured")

    artifacts_list = await server.list_artifacts_structured()
    artifact_summary = await server.get_artifact_summary_structured(artifact_id)

    assert artifacts_list.structuredContent["ok"] is True
    assert artifacts_list.structuredContent["count"] == 1
    assert artifact_summary.structuredContent["ok"] is True
    assert artifact_summary.structuredContent["artifact_id"] == artifact_id
