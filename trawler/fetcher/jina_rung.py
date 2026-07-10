"""jina_rung — rung3, JS 兜底。

移植自 fish jina_reader, 增强:
- 仅当公网 + 无鉴权才走 (敏感页永不出公网)
- X-No-Cache: true, X-Return-Format: markdown
"""

from __future__ import annotations

import logging

import httpx

from trawler import config
from trawler.urlnorm import is_public_url

log = logging.getLogger("trawler.fetcher.jina")

JINA_HEADERS = {
    "X-No-Cache": "true",
    "X-Return-Format": "markdown",
}


async def fetch(
    url: str,
    *,
    needs_account: bool = False,
    use_proxy: bool = False,
    proxy_url: str = "",
) -> str:
    """Jina Reader 抓取。返回 markdown, 失败返回空串 (不抛)。

    前置: 公网 + 无鉴权。否则直接返回空 (敏感页不出公网)。
    """
    if needs_account:
        log.info("jina skipped: url needs account (sensitive, won't send to Jina cloud)")
        return ""
    if not is_public_url(url):
        log.info("jina skipped: url not public")
        return ""

    # proxy 接入
    client_kwargs: dict = {"timeout": config.JINA_TIMEOUT}
    if use_proxy:
        proxy = proxy_url or config.HTTPS_PROXY or config.HTTP_PROXY
        if proxy:
            client_kwargs["proxy"] = proxy

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(f"https://r.jina.ai/{url}", headers=JINA_HEADERS)
            resp.raise_for_status()
            return resp.text or ""
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (429, 503):
            retry_after = e.response.headers.get("Retry-After")
            from trawler.errors import RateLimitError, parse_retry_after
            delay = parse_retry_after(retry_after)
            raise RateLimitError(delay)
        log.warning("jina fetch failed for %s: %s", url, e)
        return ""
    except Exception as e:
        log.warning("jina fetch failed for %s: %s", url, e)
        return ""
