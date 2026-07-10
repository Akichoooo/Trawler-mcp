"""集中配置 — 所有魔法数字在此, 环境变量覆盖。

模式照搬 fish 主项目: os.getenv("TRAWLER_X", default) + cast。
int 用 int(...), bool 用 .lower()=="true"。
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

# ── 路径 ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("TRAWLER_DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "trawler.db"
RAW_DIR = DATA_DIR / "raw"
VAULT_DIR = DATA_DIR / "account_vault"
ARTIFACT_DIR = Path(os.getenv("TRAWLER_ARTIFACT_DIR", str(DATA_DIR / "artifacts")))

# 启动即建目录 (和 fish 一致, import 副作用)
for _d in (DATA_DIR, RAW_DIR, VAULT_DIR, ARTIFACT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── 安全 ──────────────────────────────────────────────────────────
# SSRF 守卫: 是否放行内网/环回。默认拦截。
ALLOW_LOCAL: bool = _env_bool("TRAWLER_ALLOW_LOCAL", False)
# HITL 自动过码 (CapSolver API key)。空 = 不启用, 走人工。
CAPSOLVER_KEY: str = os.getenv("TRAWLER_CAPSOLVER_KEY", "")
# robots.txt 合规: 默认尊重 RFC 9309 (2026 已有 35% top10k 站加 AI-bot Disallow)。
# force_refresh=True 时跳过此检查 (用户明示要刷新)。
RESPECT_ROBOTS: bool = _env_bool("TRAWLER_RESPECT_ROBOTS", True)
ROBOTS_FAIL_CLOSED: bool = _env_bool("TRAWLER_ROBOTS_FAIL_CLOSED", True)
ROBOTS_MAX_BYTES: int = int(os.getenv("TRAWLER_ROBOTS_MAX_BYTES", str(512 * 1024)))
SSRF_DNS_TIMEOUT_FAIL_CLOSED: bool = _env_bool("TRAWLER_SSRF_DNS_TIMEOUT_FAIL_CLOSED", True)
SSRF_ALLOW_FAKE_IP_DNS: bool = _env_bool("TRAWLER_SSRF_ALLOW_FAKE_IP_DNS", False)
SSRF_FAKE_IP_CIDRS: str = os.getenv("TRAWLER_SSRF_FAKE_IP_CIDRS", "198.18.0.0/15")
BROWSER_ROUTE_DNS_CACHE_TTL: float = float(os.getenv("TRAWLER_BROWSER_ROUTE_DNS_CACHE_TTL", "15.0"))
ALLOW_LEGACY_PLAINTEXT_VAULT: bool = _env_bool("TRAWLER_ALLOW_LEGACY_PLAINTEXT_VAULT", False)
ALLOW_REMOTE_CDP: bool = _env_bool("TRAWLER_ALLOW_REMOTE_CDP", False)
ALLOW_UNGUARDED_BROWSER: bool = _env_bool("TRAWLER_ALLOW_UNGUARDED_BROWSER", False)
ENABLE_PII_MASKING: bool = _env_bool("TRAWLER_ENABLE_PII_MASKING", True)
ENABLE_WORD_FILTER: bool = _env_bool("TRAWLER_ENABLE_WORD_FILTER", True)

# Central MCP policy broker. "permissive" preserves local-dev behaviour while
# still reporting policy decisions. "strict" denies unknown/untargeted risky
# calls unless they pass the configured domain/tool restrictions.
POLICY_MODE: str = os.getenv("TRAWLER_POLICY_MODE", "permissive").strip().lower()
ALLOWED_DOMAINS: str = os.getenv("TRAWLER_ALLOWED_DOMAINS", "")
BLOCKED_DOMAINS: str = os.getenv("TRAWLER_BLOCKED_DOMAINS", "")
ENABLE_LIVE_BROWSER: bool = _env_bool("TRAWLER_ENABLE_LIVE_BROWSER", True)
ENABLE_CDP: bool = _env_bool("TRAWLER_ENABLE_CDP", True)
ENABLE_CRAWL_SITE: bool = _env_bool("TRAWLER_ENABLE_CRAWL_SITE", True)

# Debug artifacts are stored out-of-band so MCP results stay small.
# off: never capture; fail: capture failures; sample: failures + sampled successes.
# always: every eligible fetch.
DEBUG_ARTIFACTS: str = os.getenv("TRAWLER_DEBUG_ARTIFACTS", "fail").strip().lower()
ARTIFACT_HTML_MAX_BYTES: int = int(os.getenv("TRAWLER_ARTIFACT_HTML_MAX_BYTES", str(512 * 1024)))
ARTIFACT_SAMPLE_RATE: float = float(os.getenv("TRAWLER_ARTIFACT_SAMPLE_RATE", "0.05"))
ARTIFACT_RETENTION_DAYS: int = int(os.getenv("TRAWLER_ARTIFACT_RETENTION_DAYS", "14"))
ARTIFACT_MAX_BYTES: int = int(os.getenv("TRAWLER_ARTIFACT_MAX_BYTES", str(512 * 1024 * 1024)))
EXPOSE_ARTIFACT_BODIES: bool = _env_bool("TRAWLER_EXPOSE_ARTIFACT_BODIES", False)

# ── 代理 ──────────────────────────────────────────────────────────
HTTP_PROXY: str = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY: str = os.getenv("HTTPS_PROXY", "")
PROXY_POOL: str = os.getenv("TRAWLER_PROXY_POOL", "")

# ── 超时 (秒) ────────────────────────────────────────────────────
# crawl_url 墙钟上限 (低于 MCP client 默认 30s)
CRAWL_TIMEOUT: int = int(os.getenv("TRAWLER_CRAWL_TIMEOUT", "35"))
PATCHRIGHT_TIMEOUT: int = int(os.getenv("TRAWLER_PATCHRIGHT_TIMEOUT", "30"))
JINA_TIMEOUT: int = int(os.getenv("TRAWLER_JINA_TIMEOUT", "15"))
HTML2TEXT_TIMEOUT: int = int(os.getenv("TRAWLER_HTML2TEXT_TIMEOUT", "5"))
HITL_TIMEOUT: int = int(os.getenv("TRAWLER_HITL_TIMEOUT", "60"))

# ── 爬取规模 ──────────────────────────────────────────────────────
MAX_PAGES: int = int(os.getenv("TRAWLER_MAX_PAGES", "20"))
MAX_PAGES_HARD: int = int(os.getenv("TRAWLER_MAX_PAGES_HARD", "500"))
SEMAPHORE: int = int(os.getenv("TRAWLER_SEMAPHORE", "3"))
CRAWL_MAX_ERRORS: int = int(os.getenv("TRAWLER_CRAWL_MAX_ERRORS", "0"))
MAX_LINKS_PER_PAGE: int = int(os.getenv("TRAWLER_MAX_LINKS_PER_PAGE", "200"))
SITE_INDEX_MAX_BYTES: int = int(os.getenv("TRAWLER_SITE_INDEX_MAX_BYTES", str(1024 * 1024)))
SITE_INDEX_CHILD_SITEMAPS: int = int(os.getenv("TRAWLER_SITE_INDEX_CHILD_SITEMAPS", "10"))
FRONTIER_MAX_RETRIES: int = int(os.getenv("TRAWLER_FRONTIER_MAX_RETRIES", "2"))
FRONTIER_RETRY_BASE_SECONDS: float = float(os.getenv("TRAWLER_FRONTIER_RETRY_BASE_SECONDS", "2.0"))

# ── 资源回收与治理 ──────────────────────────────────────────────
PROFILE_TOPN: int = int(os.getenv("TRAWLER_PROFILE_TOPN", "50"))
HTML_TRUNCATE: int = int(os.getenv("TRAWLER_HTML_TRUNCATE", str(2 * 1024 * 1024)))
MAX_BROWSER_CONCURRENCY: int = int(os.getenv("TRAWLER_MAX_BROWSER_CONCURRENCY", "3"))
MEM_SAFETY_THRESHOLD_MB: int = int(os.getenv("TRAWLER_MEM_SAFETY_THRESHOLD_MB", "512"))

# ── 去重缓存 TTL (秒) ────────────────────────────────────────────
CACHE_TTL_NEWS: int = int(os.getenv("TRAWLER_CACHE_TTL_NEWS", "3600"))        # 1h
CACHE_TTL_WIKI: int = int(os.getenv("TRAWLER_CACHE_TTL_WIKI", "604800"))     # 7d
CACHE_TTL_DEFAULT: int = int(os.getenv("TRAWLER_CACHE_TTL_DEFAULT", "21600")) # 6h

# ── 手册置信度 ────────────────────────────────────────────────────
CONFIDENCE_MIN: float = float(os.getenv("TRAWLER_CONFIDENCE_MIN", "0.5"))
UNREACHABLE_TTL: int = int(os.getenv("TRAWLER_UNREACHABLE_TTL", "86400"))  # 1d

# ── 提取后正文字数阈值 (L3 双判据之一) ───────────────────────────
MIN_CONTENT_CHARS: int = int(os.getenv("TRAWLER_MIN_CONTENT_CHARS", "200"))

# ── 去重 id 长度 (sha1 前 N 位, 64-bit) ──────────────────────────
ID_LEN: int = int(os.getenv("TRAWLER_ID_LEN", "16"))

# ── 礼貌/等待间隔 (秒) ──────────────────────────────────────────
# SPA 渲染等待 (patchright goto 后)
SPA_RENDER_WAIT: float = float(os.getenv("TRAWLER_SPA_RENDER_WAIT", "1.5"))
# 同域请求最小间隔 (crawl_site 礼貌)
SAME_DOMAIN_INTERVAL: float = float(os.getenv("TRAWLER_SAME_DOMAIN_INTERVAL", "1.0"))
# wait_for_job 轮询间隔
JOB_POLL_INTERVAL: float = float(os.getenv("TRAWLER_JOB_POLL_INTERVAL", "2.0"))
# audit 最近错误返回条数
AUDIT_RECENT_LIMIT: int = int(os.getenv("TRAWLER_AUDIT_RECENT_LIMIT", "10"))
# wait_for_job 默认超时 (秒)
WAIT_FOR_JOB_TIMEOUT: int = int(os.getenv("TRAWLER_WAIT_FOR_JOB_TIMEOUT", "120"))
