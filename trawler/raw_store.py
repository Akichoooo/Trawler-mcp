"""raw_store — raw/<id>.md 原子写 + frontmatter + .BLOCKED.md。

raw 是交接边界 (爬虫写 / 图书馆读)。所有写都原子 (.tmp → os.replace)。
get_raw 路径白名单防穿越 (pathlib.resolve + parents 检查, 防 Windows 大小写/UNC)。
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml

from trawler import config
from trawler.atomic import atomic_write

log = logging.getLogger("trawler.raw")
_SAFE_RAW_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _safe_raw_id(raw_id: str) -> str:
    value = str(raw_id or "")
    if not _SAFE_RAW_ID.fullmatch(value):
        raise ValueError("invalid raw_id")
    return value


def raw_path(raw_id: str) -> Path:
    """raw/<raw_id>.md 完整路径。"""
    return config.RAW_DIR / f"{_safe_raw_id(raw_id)}.md"


def blocked_path(raw_id: str) -> Path:
    """raw/<raw_id>.BLOCKED.md (失败诊断)。"""
    return config.RAW_DIR / f"{_safe_raw_id(raw_id)}.BLOCKED.md"


def save_raw(
    raw_id: str,
    *,
    url: str,
    final_url: str,
    title: str,
    markdown: str,
    gear_used: str,
    status: str = "ok",
    blocked_reason: str = "",
    metadata: dict | None = None,
) -> Path:
    """存 raw/<id>.md (带 frontmatter)。原子写。返回路径。"""
    fm = {
        "url": url,
        "final_url": final_url,
        "title": title,
        "crawled_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": status,
        "gear_used": gear_used,
    }
    if blocked_reason:
        fm["blocked_reason"] = blocked_reason
    if metadata:
        fm.update(metadata)

    frontmatter = yaml.dump(fm, allow_unicode=True, sort_keys=False)
    # 正文直接是 md (不在文件里重复 title, 避免和 md 自身标题冲突)
    content = f"---\n{frontmatter}---\n\n{markdown}\n"
    path = raw_path(raw_id)
    atomic_write(path, content)
    return path


def save_blocked(
    raw_id: str,
    *,
    url: str,
    reason: str,
    html_excerpt: str = "",
    gear_used: str = "",
    metadata: dict | None = None,
) -> Path:
    """失败也存: raw/<id>.BLOCKED.md (诊断用, 留给 librarian)。原子写。"""
    fm = {
        "url": url,
        "crawled_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "blocked",
        "reason": reason,
        "gear_used": gear_used,
    }
    if metadata:
        fm.update(metadata)
    excerpt = html_excerpt[:5000] if html_excerpt else ""  # 截 5KB 诊断
    frontmatter = yaml.dump(fm, allow_unicode=True, sort_keys=False)
    content = f"---\n{frontmatter}---\n\n# BLOCKED: {reason}\n\n## HTML excerpt\n\n```\n{excerpt}\n```\n"
    path = blocked_path(raw_id)
    atomic_write(path, content)
    return path


def get_raw(path: str) -> str:
    """读 raw 文件。路径白名单防穿越。

    用 pathlib.resolve() 解析 (防 Windows 大小写/UNC/.. 绕过)。
    只允许读 RAW_DIR 下的文件。
    """
    target = Path(path).resolve()
    raw_root = config.RAW_DIR.resolve()
    # 检查 target 是否在 RAW_DIR 内 (是 raw_root 本身或其子孙)
    try:
        target.relative_to(raw_root)
    except ValueError:
        raise PermissionError(f"path outside RAW_DIR: {path}")
    if not target.exists():
        raise FileNotFoundError(f"raw file not found: {path}")
    return target.read_text(encoding="utf-8")


def strip_frontmatter(raw_content: str) -> str:
    """Return markdown body without YAML frontmatter."""
    if not raw_content.startswith("---"):
        return raw_content
    end = raw_content.find("\n---", 4)
    if end == -1:
        return raw_content
    return raw_content[end + 4:].lstrip("\n")


def read_metadata(raw_id: str, *, prefer_blocked: bool = False) -> dict:
    """Read raw frontmatter metadata by raw_id."""
    blocked = blocked_path(raw_id)
    path = blocked if prefer_blocked and blocked.exists() else raw_path(raw_id)
    if not path.exists():
        path = blocked
    if not path.exists():
        return {}
    try:
        content = get_raw(str(path))
    except (OSError, PermissionError, ValueError):
        return {}
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 4)
    if end == -1:
        return {}
    try:
        data = yaml.safe_load(content[4:end]) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def list_raw() -> list[dict]:
    """列出 raw 目录所有 .md (含 .BLOCKED.md)。返回 [{id, path, blocked, size}]。"""
    items = []
    for p in sorted(config.RAW_DIR.glob("*.md")):
        name = p.stem  # 去掉 .md (BLOCKED 的会是 xxx.BLOCKED)
        blocked = name.endswith(".BLOCKED")
        raw_id = name[:-len(".BLOCKED")] if blocked else name
        items.append({
            "raw_id": raw_id,
            "path": str(p),
            "blocked": blocked,
            "size": p.stat().st_size,
        })
    return items
