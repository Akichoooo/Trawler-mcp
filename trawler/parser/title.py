"""extract_title — 移植自 fish okf.extract_title。

三级 fallback: <title> 标签 (去站点后缀) → URL 末段 → fallback/URL。
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
# 常见站点后缀分隔符, 取第一个命中的切
_SITE_SUFFIX_SEPS = [" - ", " | ", " _ ", "—", "·"]


def extract_title(html: str, url: str, fallback_title: str = "") -> str:
    """提取标题。三级 fallback。"""
    # ① <title> 标签
    if html:
        m = _TITLE_TAG_RE.search(html)
        if m:
            title = m.group(1).strip()
            # 去站点后缀: "标题 - 站名" → "标题"
            for sep in _SITE_SUFFIX_SEPS:
                if sep in title:
                    title = title.split(sep, 1)[0].strip()
                    break
            if title and len(title) > 1:
                return title

    # ② URL 末段
    if url:
        path = urlparse(url).path.rstrip("/")
        if path:
            last = unquote(path.rsplit("/", 1)[-1])
            last = last.rsplit(".", 1)[0]  # 去扩展名
            last = last.replace("-", " ").replace("_", " ").strip()
            if last:
                return last

    # ③ fallback (没传则用 URL 本身)
    return fallback_title or url
