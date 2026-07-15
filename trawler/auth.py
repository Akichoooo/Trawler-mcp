"""auth — OIDC Bearer token / mTLS 认证层。

仅 streamable-http / SSE transport 启用; stdio 跳过 (本地进程, 无网络暴露)。
支持两种认证模式:
  1. OIDC JWT: TRAWLER_OIDC_ISSUER + TRAWLER_OIDC_JWKS_URL 校验 Bearer token
  2. mTLS: 依赖 ASGI 服务器 (uvicorn) 的 ssl_cert_reqs 配置, 本模块只做 presence 检查

无配置时: 非 stdio transport 启动即 warn (不阻断, 兼容开发环境)。
"""

from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("trawler.auth")

# JWKS 缓存 (模块级, 防 per-request 拉取)
_jwks_cache: dict[str, dict] = {}
_jwks_fetched_at: float = 0.0
_JWKS_TTL = 3600.0  # 1h


def is_auth_enabled() -> bool:
    """是否启用认证 (仅非 stdio transport)。"""
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        return False
    return os.getenv("TRAWLER_AUTH_ENABLED", "").lower() in ("1", "true", "yes", "on")


def is_mtls_enabled() -> bool:
    """是否启用 mTLS (检查配置)。"""
    return os.getenv("TRAWLER_MTLS_ENABLED", "").lower() in ("1", "true", "yes", "on")


def _get_oidc_config() -> tuple[str, str]:
    """获取 OIDC issuer + JWKS URL。"""
    issuer = os.getenv("TRAWLER_OIDC_ISSUER", "")
    jwks_url = os.getenv("TRAWLER_OIDC_JWKS_URL", "")
    return issuer, jwks_url


async def _fetch_jwks(jwks_url: str) -> dict:
    """拉取 JWKS (带 TTL 缓存)。"""
    global _jwks_cache, _jwks_fetched_at
    now = time.monotonic()
    if _jwks_cache and now - _jwks_fetched_at < _JWKS_TTL:
        return _jwks_cache
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(jwks_url)
            resp.raise_for_status()
            _jwks_cache = resp.json()
            _jwks_fetched_at = now
            return _jwks_cache
    except Exception as e:
        log.warning("Failed to fetch JWKS from %s: %s", jwks_url, e)
        return {}


async def verify_token(token: str) -> tuple[bool, str]:
    """校验 Bearer token (OIDC JWT)。

    返回 (valid, reason)。
    无 OIDC 配置时: 放行 (降级, 兼容开发环境) + warn。
    """
    issuer, jwks_url = _get_oidc_config()
    if not issuer and not jwks_url:
        log.warning("Auth enabled but no OIDC issuer/JWKS configured — token passthrough (development mode)")
        return True, "no-oidc-configured"

    try:
        import jwt as pyjwt  # PyJWT
    except ImportError:
        log.error("No JWT library installed (pip install trawler-mcp[auth]) — token verification skipped")
        return False, "jwt-library-missing"

    try:
        # 先解码 header 拿 kid
        unverified_header = pyjwt.get_unverified_header(token)
        kid = unverified_header.get("kid", "")
        if not jwks_url:
            jwks_url = f"{issuer}/.well-known/jwks.json"
        jwks = await _fetch_jwks(jwks_url)
        signing_key = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                signing_key = pyjwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
                break
        if not signing_key:
            return False, f"key-not-found kid={kid}"

        payload = pyjwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=issuer or None,
            options={"verify_aud": False},
        )
        return True, f"sub={payload.get('sub', '?')}"
    except Exception as e:
        return False, f"decode-failed: {type(e).__name__}: {e}"


class AuthMiddleware:
    """ASGI middleware: 校验 Bearer token (OIDC JWT) 或 mTLS 证书。

    用法:
        app = AuthMiddleware(app)  # 包裹 ASGI app

    stdio transport 不经过 ASGI, 天然跳过。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # mTLS: 检查客户端证书 (由 uvicorn --ssl-cert-reqs 注入)
        if is_mtls_enabled():
            ssl_info = scope.get("extensions", {}).get("tls", {})
            client_cert = ssl_info.get("client_cert_subject")
            if client_cert:
                await self.app(scope, receive, send)
                return
            # mTLS 启用但无证书 → 拒绝
            await self._send_error(send, 403, "mTLS client certificate required")
            return

        # OIDC Bearer token
        if is_auth_enabled():
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode("latin-1")
            if not auth_header.startswith("Bearer "):
                await self._send_error(send, 401, "Missing or invalid Authorization header")
                return
            token = auth_header[7:]
            valid, reason = await verify_token(token)
            if not valid:
                log.warning("Auth rejected: %s", reason)
                await self._send_error(send, 401, f"Invalid token: {reason}")
                return

        await self.app(scope, receive, send)

    async def _send_error(self, send, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })
