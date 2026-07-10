import json

from trawler.crawl_url import _format_error
from trawler.errors import VALID_ERROR_TYPES
from trawler.ssrf import block_reason


def test_trawler_entrypoint_importable():
    from trawler.__main__ import main

    assert callable(main)

def test_format_error_contract():
    # 验证常规错误
    result = _format_error("rate-limit", "Too many requests")
    assert result.startswith("__TRAWLER_ERROR__:")
    err_json = json.loads(result[len("__TRAWLER_ERROR__:") :])
    assert err_json["errorType"] == "rate-limit"
    assert err_json["retryable"] is True
    assert "wait before retrying" in err_json["suggestedAction"]
    
    # 验证必定失败的错误
    result = _format_error("empty-content", "No content")
    err_json = json.loads(result[len("__TRAWLER_ERROR__:") :])
    assert err_json["errorType"] == "empty-content"
    assert err_json["retryable"] is False
    assert "inspect artifact_id" in err_json["suggestedAction"]

    # 验证特殊状态
    result = _format_error("all-fetchers-failed", "All failed")
    err_json = json.loads(result[len("__TRAWLER_ERROR__:") :])
    assert err_json["errorType"] == "all-fetchers-failed"
    assert err_json["retryable"] is False

    result = _format_error("blocked-ssrf-redirect", "SSRF")
    err_json = json.loads(result[len("__TRAWLER_ERROR__:") :])
    assert err_json["errorType"] == "blocked-ssrf-redirect"
    assert err_json["retryable"] is False

    result = _format_error("empty-content", "No content", artifact_id="art-123")
    err_json = json.loads(result[len("__TRAWLER_ERROR__:") :])
    assert err_json["artifact_id"] == "art-123"

def test_ssrf_block_reason_contract():
    result = block_reason("http://127.0.0.1")
    assert result.startswith("__TRAWLER_ERROR__:")
    err_json = json.loads(result[len("__TRAWLER_ERROR__:") :])
    assert err_json["errorType"] == "blocked-ssrf"
    assert err_json["retryable"] is False
    assert "local/internal crawling" in err_json["suggestedAction"]

def test_all_error_types_valid():
    # 提取代码中现有的 error_type 并验证其是否在集合内
    # 这只是一个保险单测，如果有新的需要手动加进来
    for t in VALID_ERROR_TYPES:
        assert isinstance(t, str) and "-" in t or t == "timeout", f"Invalid format for errorType: {t}"


def test_error_types_have_specific_guidance():
    from trawler.errors import ERROR_GUIDANCE

    for error_type in VALID_ERROR_TYPES:
        assert error_type in ERROR_GUIDANCE
        assert ERROR_GUIDANCE[error_type] not in {"retry", "abort"}
