"""extract — 从 HTML 提取干净 markdown。

移植自 fish clean_html_to_markdown, 增强:
- 前置 2MB 截断防 OOM
- 第三档 html2text 兜底 (fish 原本只有两档)
- 全失败返回特殊标记, 供 crawl_url 组装 __TRAWLER_ERROR__

提取链 (每拿到 HTML 都走, 与 fetcher 正交):
  trafilatura 2.x → extruct (JSON-LD/Microdata 结构化数据)
  → readability-lxml → markdownify → html2text

extruct 是 2026 新增档: 电商/新闻 schema.org 结构化数据质量优于启发式提取。
"""

from __future__ import annotations

import logging
import re

from trawler import config

log = logging.getLogger("trawler.parser")

# 提取全失败的特殊标记 (crawl_url 检测它来组装 __TRAWLER_ERROR__)
PARSERS_FAILED = "__PARSERS_EXTRACTED_NO_TEXT__"


def _truncate(html: str) -> str:
    """2MB 硬截断, 防 trafilatura 正则在十几 MB base64 上 OOM。"""
    if len(html) > config.HTML_TRUNCATE:
        log.warning("HTML truncated %d → %d bytes", len(html), config.HTML_TRUNCATE)
        return html[: config.HTML_TRUNCATE]
    return html


def _strip_boilerplate(html: str) -> str:
    """bs4 剪 nav/footer/aside/script/style, 减小 DOM 并应用前置安全净化。"""
    try:
        from bs4 import BeautifulSoup
        from trawler.parser import safety
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["nav", "footer", "aside", "script", "style", "noscript"]):
            tag.decompose()
        # 前置安全过滤：移除 CSS 隐藏、零字体大小等隐藏注入元素
        safety.sanitize_html_soup(soup)
        return str(soup)
    except ImportError:
        return html
    except Exception as e:
        log.warning("bs4 boilerplate strip failed: %s", e)
        return html


def extract(html: str, url: str = "") -> str:
    """主入口。返回干净且经过安全净化（防提示词注入）的 markdown，或 PARSERS_FAILED 标记。"""
    from trawler.parser import safety

    raw_md = _extract_raw(html, url)
    if raw_md == PARSERS_FAILED:
        return PARSERS_FAILED

    # 应用安全脱敏与反提示词注入层
    return safety.sanitize_markdown(raw_md)


def _extract_raw(html: str, url: str = "") -> str:
    """原提取主逻辑。返回干净 markdown, 或 PARSERS_FAILED 标记。"""
    if not html or not html.strip():
        return PARSERS_FAILED

    from trawler.parser import content_adapter

    adapted = content_adapter.adapt_text_response(html, url)
    if adapted:
        return adapted

    html = _truncate(html)
    structure_hints = _structure_hints(html)
    html = _strip_boilerplate(html)

    # ① trafilatura (主)
    md = _try_trafilatura(html, url)
    trafilatura_md = md
    if md and not _misses_markdown_structure(md, structure_hints):
        return _normalize_markdown(md)

    # ② extruct: 提 schema.org JSON-LD 结构化数据 (电商/新闻质量提升)
    md = _try_extruct(html, url)
    if md:
        return _normalize_markdown(md)

    # ③ readability + markdownify (兜底)
    md = _try_readability(html, url)
    readability_md = md
    if md and not _misses_markdown_structure(md, structure_hints):
        return _normalize_markdown(md)

    # ④ html2text (最后兜底, 质量最低但绝不挂)
    md = _try_html2text(html)
    if md:
        return _normalize_markdown(md)

    if readability_md:
        return _normalize_markdown(readability_md)
    if trafilatura_md:
        return _normalize_markdown(trafilatura_md)

    return PARSERS_FAILED


def _normalize_markdown(markdown: str) -> str:
    return str(markdown or "").replace("\\_", "_").strip()


def _structure_hints(html: str) -> dict[str, bool]:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        content_root = soup.find(["main", "article"]) or soup.body or soup
        return {
            "heading": bool(content_root.find(re.compile(r"^h[1-6]$"))),
            "table": bool(content_root.find("table")),
            "code": bool(content_root.find(["pre", "code"])),
            "link": bool(content_root.find("a", href=True)),
        }
    except Exception:
        return {"heading": False, "table": False, "code": False, "link": False}


def _misses_markdown_structure(markdown: str, hints: dict[str, bool]) -> bool:
    if not markdown:
        return True
    if hints.get("heading") and not re.search(r"^#{1,6}\s+\S", markdown, re.MULTILINE):
        return True
    if hints.get("table") and "|" not in markdown:
        return True
    if hints.get("link") and not re.search(r"\[[^\]]+\]\([^)]+\)", markdown):
        return True
    if hints.get("code") and "```" not in markdown and not re.search(r"(^|\n)    \S", markdown):
        return True
    return False


def _try_extruct(html: str, url: str) -> str:
    """extruct 提取 schema.org JSON-LD / Microdata 结构化数据。

    电商 (Product)、新闻 (NewsArticle)、文章 (Article) 的 JSON-LD
    通常含 title/description/articleBody 字段, 质量优于启发式提取。
    失败返回空串 (不影响后续档)。
    """
    try:
        import extruct
        # 提 JSON-LD + Microdata (RDFa 对爬虫场景价值低, 跳过省时)
        data = extruct.extract(html, base_url=url, syntaxes=["json-ld", "microdata"])
    except ImportError:
        return ""
    except Exception as e:
        log.debug("extruct failed: %s", e)
        return ""

    parts: list[str] = []
    for syntax, items in data.items():
        if not items:
            continue
        for item in items:
            # 常见 schema.org 类型优先字段
            if not isinstance(item, dict):
                continue
            # 优先 articleBody (完整正文) — 类型安全: schema.org 允许 list/dict, 仅取 str
            body = item.get("articleBody") or item.get("text") or item.get("description")
            title = item.get("headline") or item.get("name") or item.get("title")
            if title and isinstance(title, str):
                parts.append(f"# {title}")
            if body:
                if isinstance(body, str):
                    parts.append(body)
                elif isinstance(body, list):
                    parts.extend(str(x) for x in body if isinstance(x, str))
                elif isinstance(body, dict):
                    for k in ("text", "value", "description"):
                        if k in body and isinstance(body[k], str):
                            parts.append(body[k])
                            break
            # Product 提取 (电商)
            elif item.get("@type") and "product" in str(item.get("@type", "")).lower():
                name = item.get("name")
                desc = item.get("description")
                # schema.org allows property values to be list/dict — coerce to str
                if isinstance(name, list):
                    name = next((x for x in name if isinstance(x, str)), "")
                elif not isinstance(name, str):
                    name = str(name) if name else ""
                if isinstance(desc, list):
                    desc = next((x for x in desc if isinstance(x, str)), "")
                elif not isinstance(desc, str):
                    desc = str(desc) if desc else ""
                if name:
                    parts.append(f"# {name}")
                if desc:
                    parts.append(desc)
    if parts:
        return "\n\n".join(parts).strip()
    return ""


def _try_trafilatura(html: str, url: str) -> str:
    try:
        import trafilatura
        result = trafilatura.extract(
            html, url=url, include_links=True, include_formatting=True
        )
        if result and result.strip():
            return result.strip()
    except Exception as e:
        log.debug("trafilatura failed: %s", e)
    return ""


def _try_readability(html: str, url: str) -> str:
    try:
        from markdownify import markdownify
        from readability import Document
        doc = Document(html)
        summary = doc.summary()
        if summary and len(summary) > 50:
            md = markdownify(summary, heading_style="ATX")
            if md and md.strip():
                return md.strip()
    except Exception as e:
        log.debug("readability failed: %s", e)
    return ""


def _try_html2text(html: str) -> str:
    try:
        import html2text
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0
        md = h.handle(html)
        if md and md.strip():
            return md.strip()
    except Exception as e:
        log.debug("html2text failed: %s", e)
    return ""


def is_extracted(md: str) -> bool:
    """crawl_url 用: 提取是否成功 (非空且非失败标记)。"""
    return bool(md) and md != PARSERS_FAILED
