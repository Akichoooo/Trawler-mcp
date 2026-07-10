import json
import os

import pytest


def test_artifact_save_list_read_and_truncate(tmp_path, monkeypatch):
    from trawler import artifacts, config

    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(config, "DEBUG_ARTIFACTS", "fail")
    monkeypatch.setattr(config, "ARTIFACT_HTML_MAX_BYTES", 10)

    artifact_id = artifacts.save_artifact(
        url="https://example.com/",
        reason="empty-content",
        success=False,
        final_url="https://example.com/final",
        http_status=403,
        gear_used="patchright_headless",
        session_id="session-1",
        html="<html>hello world</html>",
        console_messages=[{"type": "error", "text": "boom"}],
        request_failures=[{"url": "https://example.com/a.js", "failure": "blocked"}],
    )

    assert artifact_id
    listed = artifacts.list_artifacts()
    assert listed[0]["artifact_id"] == artifact_id
    assert listed[0]["html_truncated"] is True
    assert "page.html" in listed[0]["files"]

    metadata = json.loads(artifacts.read_artifact(artifact_id))
    assert metadata["reason"] == "empty-content"
    assert metadata["http_status"] == 403
    assert artifacts.read_artifact(artifact_id, "page.html")


def test_artifact_summary_omits_large_bodies(tmp_path, monkeypatch):
    from trawler import artifacts, config

    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(config, "DEBUG_ARTIFACTS", "always")

    artifact_id = artifacts.save_artifact(
        url="https://example.com/",
        reason="blocked",
        success=False,
        http_status=403,
        html="<html>secret body</html>",
        screenshot=b"png",
        console_messages=[{"type": "warning", "text": "console body"}],
        request_failures=[{"url": "https://example.com/app.js", "failure": "blocked"}],
        extra={"challenge": "captcha"},
    )

    summary = artifacts.artifact_summary(artifact_id)
    encoded = json.dumps(summary, ensure_ascii=False)

    assert summary["artifact_id"] == artifact_id
    assert summary["reason"] == "blocked"
    assert summary["http_status"] == 403
    assert summary["console_count"] == 1
    assert summary["request_failure_count"] == 1
    assert summary["extra_keys"] == ["challenge"]
    assert "page.html" in {item["name"] for item in summary["files"]}
    assert "screenshot.png" in {item["name"] for item in summary["files"]}
    assert "secret body" not in encoded
    assert "console body" not in encoded


def test_artifact_path_safety(tmp_path, monkeypatch):
    from trawler import artifacts, config

    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifacts")

    with pytest.raises(ValueError):
        artifacts.read_artifact("../secret")
    with pytest.raises(ValueError):
        artifacts.read_artifact("safe-id", "../metadata.json")
    with pytest.raises(ValueError):
        artifacts.read_artifact("safe-id", "screenshot.png")
    with pytest.raises(ValueError):
        artifacts.artifact_summary("../secret")


def _set_artifact_mtime(root, artifact_id, timestamp):
    artifact_dir = root / artifact_id
    for child in artifact_dir.rglob("*"):
        os.utime(child, (timestamp, timestamp))
    os.utime(artifact_dir, (timestamp, timestamp))


def test_cleanup_artifacts_dry_run_and_age(tmp_path, monkeypatch):
    from trawler import artifacts, config

    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(config, "DEBUG_ARTIFACTS", "always")

    old_id = artifacts.save_artifact(
        url="https://example.com/old",
        reason="old",
        html="<html>old</html>",
    )
    new_id = artifacts.save_artifact(
        url="https://example.com/new",
        reason="new",
        html="<html>new</html>",
    )
    now = 1_700_000_000
    _set_artifact_mtime(config.ARTIFACT_DIR, old_id, now - 10 * 86400)
    _set_artifact_mtime(config.ARTIFACT_DIR, new_id, now)
    monkeypatch.setattr(artifacts.time, "time", lambda: now)

    dry = artifacts.cleanup_artifacts(dry_run=True, max_age_days=7, max_total_bytes=-1)

    assert dry["candidate_count"] == 1
    assert dry["candidates"][0]["artifact_id"] == old_id
    assert (config.ARTIFACT_DIR / old_id).exists()

    done = artifacts.cleanup_artifacts(dry_run=False, max_age_days=7, max_total_bytes=-1)

    assert done["deleted_count"] == 1
    assert not (config.ARTIFACT_DIR / old_id).exists()
    assert (config.ARTIFACT_DIR / new_id).exists()


def test_cleanup_artifacts_max_size_deletes_oldest(tmp_path, monkeypatch):
    from trawler import artifacts, config

    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(config, "DEBUG_ARTIFACTS", "always")

    old_id = artifacts.save_artifact(
        url="https://example.com/old",
        reason="old",
        html="x" * 200,
    )
    new_id = artifacts.save_artifact(
        url="https://example.com/new",
        reason="new",
        html="y" * 200,
    )
    _set_artifact_mtime(config.ARTIFACT_DIR, old_id, 1_700_000_000)
    _set_artifact_mtime(config.ARTIFACT_DIR, new_id, 1_700_000_100)

    newest_size = sum(
        p.stat().st_size
        for p in (config.ARTIFACT_DIR / new_id).rglob("*")
        if p.is_file()
    )
    result = artifacts.cleanup_artifacts(
        dry_run=False,
        max_age_days=-1,
        max_total_bytes=newest_size,
    )

    assert result["deleted_count"] == 1
    assert result["candidates"][0]["artifact_id"] == old_id
    assert not (config.ARTIFACT_DIR / old_id).exists()
    assert (config.ARTIFACT_DIR / new_id).exists()


def test_cleanup_artifacts_skips_invalid_dirs(tmp_path, monkeypatch):
    from trawler import artifacts, config

    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifacts")
    config.ARTIFACT_DIR.mkdir(parents=True)
    invalid = config.ARTIFACT_DIR / "bad id"
    invalid.mkdir()
    (invalid / "metadata.json").write_text("{}", encoding="utf-8")

    result = artifacts.cleanup_artifacts(dry_run=False, max_age_days=0, max_total_bytes=0)

    assert result["skipped_count"] == 1
    assert invalid.exists()


def test_list_artifacts_skips_symlink_dirs(tmp_path, monkeypatch):
    from trawler import artifacts, config

    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifacts")
    config.ARTIFACT_DIR.mkdir(parents=True)
    outside = tmp_path / "outside-artifact"
    outside.mkdir()
    (outside / "metadata.json").write_text(
        json.dumps({"artifact_id": "safeid", "reason": "outside"}),
        encoding="utf-8",
    )
    try:
        os.symlink(outside, config.ARTIFACT_DIR / "safeid", target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"symlink unavailable: {e}")

    assert artifacts.list_artifacts() == []


@pytest.mark.asyncio
async def test_empty_content_error_includes_artifact(tmp_db, tmp_path, monkeypatch):
    from trawler import config
    from trawler import crawl_url as crawl_url_mod
    from trawler.parser import extract as parser_extract

    monkeypatch.setattr(config, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(config, "DEBUG_ARTIFACTS", "fail")
    monkeypatch.setattr(config, "RESPECT_ROBOTS", False)

    async def fake_resolve(*args, **kwargs):
        return False, "93.184.216.34"

    async def fake_ladder(*args, **kwargs):
        return (
            "<html><head><title>x</title></head><body></body></html>",
            200,
            "https://example.com/",
            "patchright_headless",
            "",
        )

    monkeypatch.setattr(crawl_url_mod.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(crawl_url_mod, "_fetch_ladder", fake_ladder)
    monkeypatch.setattr(parser_extract, "extract", lambda *args, **kwargs: parser_extract.PARSERS_FAILED)

    result = await crawl_url_mod.crawl_url("https://example.com/", force_refresh=True)

    assert result.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(result[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "empty-content"
    assert payload["artifact_id"]

    metadata = json.loads(
        (tmp_path / "artifacts" / payload["artifact_id"] / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["reason"] == "empty-content"
