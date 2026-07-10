"""Deterministic markdown quality/provenance signals."""

from __future__ import annotations

import re
from typing import Any


def markdown_quality(markdown: str) -> dict[str, Any]:
    text = markdown or ""
    lines = text.splitlines()
    return {
        "char_count": len(text),
        "line_count": len(lines),
        "heading_count": sum(1 for line in lines if re.match(r"^#{1,6}\s+\S", line)),
        "link_count_markdown": len(re.findall(r"\[[^\]]+\]\([^)]+\)", text)),
        "table_count": _table_count(lines),
        "code_block_count": _code_block_count(lines),
    }


def _table_count(lines: list[str]) -> int:
    count = 0
    for idx in range(len(lines) - 1):
        if "|" not in lines[idx]:
            continue
        separator = lines[idx + 1].strip()
        if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", separator):
            count += 1
    return count


def _code_block_count(lines: list[str]) -> int:
    count = 0
    in_block = False
    for line in lines:
        if line.strip().startswith("```"):
            if not in_block:
                count += 1
            in_block = not in_block
    return count
