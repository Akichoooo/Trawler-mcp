import json

OK_PREFIX = "__TRAWLER_OK__:\n\n"
ERROR_PREFIX = "__TRAWLER_ERROR__:"

_RETRYABLE_TYPES = frozenset({
    "timeout",
    "rate-limit",
    "session-expired",
    "domain-unreachable",
    "job-timeout",
})

ERROR_GUIDANCE = {
    "timeout": "retry with a higher timeout or a narrower request",
    "rate-limit": "wait before retrying and reduce concurrency for this domain",
    "session-expired": "refresh the account login or storage state, then retry",
    "domain-unreachable": "retry after the unreachable TTL or inspect the domain rule",
    "job-timeout": "check job status/results, then wait again if it is still running",
    "blocked-bot": "inspect artifact_id if present, then try proxy, account state, or HITL",
    "blocked-robots": (
        "respect robots.txt; for single-page user-authorized browser retrieval, "
        "use retrieve_page(access_mode='user_authorized')"
    ),
    "empty-content": (
        "inspect artifact_id if present; retry with bypass_l3 only for false positives"
    ),
    "blocked-ssrf": "abort unless local/internal crawling is explicitly enabled",
    "blocked-ssrf-redirect": "abort; the redirect target is blocked by SSRF protection",
    "blocked-scope": "narrow the crawl policy or start from a URL inside the allowed scope",
    "cache-miss": "fetch with cache_mode='enabled' or 'write_only' to populate cache",
    "chunk-not-found": "request a chunk_index within the available range",
    "all-fetchers-failed": "inspect artifact_id if present, then try proxy, account state, or HITL",
    "artifact-not-found": "call list_artifacts and use a current artifact_id",
    "dns-error": "check the domain or retry later if DNS is transient",
    "human-window-unavailable": (
        "run with a visible browser session and TRAWLER_VAULT_KEY, "
        "or preconfigure account state"
    ),
    "internal-error": "check get_engine_status and server logs",
    "invalid-artifact": "use an artifact_id from list_artifacts",
    "invalid-mode": "use one of the documented modes",
    "invalid-url": "provide a valid http or https URL",
    "job-not-found": "check the job_id from crawl_site or get_job_results",
    "map-failed": "retry map_site or crawl_url the start URL to inspect the failure",
    "raw-not-found": "call list_raw and use an existing raw_id",
    "permission-denied": "use a raw_id or artifact_id inside Trawler storage",
    "section-not-found": "call mode='toc' first and use an existing section id",
}


def format_ok(content: str) -> str:
    return f"{OK_PREFIX}{content}"


def is_ok(value: str) -> bool:
    return value.startswith(OK_PREFIX)


def is_error(value: str) -> bool:
    return value.startswith(ERROR_PREFIX)


def unwrap_ok(value: str) -> str:
    return value[len(OK_PREFIX):]


def format_error(error_type: str, message: str, **details) -> str:
    retryable = error_type in _RETRYABLE_TYPES
    payload = {
        "errorType": error_type,
        "message": message,
        "retryable": retryable,
        "suggestedAction": ERROR_GUIDANCE.get(
            error_type,
            "retry" if retryable else "abort",
        ),
    }
    payload.update({k: v for k, v in details.items() if v not in (None, "")})
    err_json = json.dumps(payload, ensure_ascii=False)
    return f"{ERROR_PREFIX}{err_json}"


class RateLimitError(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")

def parse_retry_after(
    retry_after: str | None,
    default: float = 2.0,
    *,
    max_backoff: float = 300.0,
) -> float:
    if not retry_after:
        return default
    try:
        val = float(retry_after)
        if val != val or val == float('inf'):  # NaN or inf
            return default
        return max(0.0, min(val, max_backoff))
    except ValueError:
        import time
        from email.utils import parsedate_to_datetime
        try:
            dt = parsedate_to_datetime(retry_after)
            delay = dt.timestamp() - time.time()
            return max(0.0, min(delay, max_backoff))
        except Exception:
            return default

VALID_ERROR_TYPES = [
    "timeout",
    "rate-limit",
    "session-expired",
    "domain-unreachable",
    "blocked-bot",
    "blocked-robots",
    "empty-content",
    "blocked-ssrf",
    "blocked-ssrf-redirect",
    "blocked-scope",
    "cache-miss",
    "chunk-not-found",
    "all-fetchers-failed",
    "artifact-not-found",
    "dns-error",
    "human-window-unavailable",
    "internal-error",
    "invalid-artifact",
    "invalid-mode",
    "invalid-url",
    "job-not-found",
    "job-timeout",
    "map-failed",
    "raw-not-found",
    "permission-denied",
    "section-not-found",
]
