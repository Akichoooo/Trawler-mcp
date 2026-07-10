"""chunker.py — Agent 友好型 Markdown 目录索引与切片抽取。

提供:
- generate_toc: 生成全局标题目录树 (TOC)
- slice_by_section: 按 Section ID 或标题提取特定章节
- slice_by_tokens: 按 Token/字数预估分页切片
"""

from __future__ import annotations

import re


def generate_toc(markdown_text: str) -> str:
    """提取 Markdown 中的 H1-H4 标题，生成带有 Section ID 的目录视图。"""
    lines = markdown_text.splitlines()
    headings = []
    
    # 匹配 # H1, ## H2, ### H3, #### H4
    heading_pattern = re.compile(r"^(#{1,4})\s+(.+)$")
    
    for idx, line in enumerate(lines, start=1):
        match = heading_pattern.match(line.strip())
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            headings.append((idx, level, title))
            
    if not headings:
        char_count = len(markdown_text)
        return (
            f"# 📍 页面无结构化标题 (总字数: {char_count} 字符)\n\n"
            "建议使用 `mode='full'` 阅读全文，或 `mode='chunk', chunk_index=1` 分页阅读。\n"
        )
        
    toc_lines = [
        "# 📍 页面目录索引 "
        f"(Total Headings: {len(headings)}, Total Length: {len(markdown_text)} chars)\n",
        "使用 `mode='section', section_id='Section N'` 调取对应章节内容。\n",
    ]
    
    for sec_num, (line_no, level, title) in enumerate(headings, start=1):
        indent = "  " * (level - 1)
        sec_id = f"Section {sec_num}"
        toc_lines.append(f"{indent}- **[{sec_id}]** Line {line_no}: {title}")
        
    return "\n".join(toc_lines)


def slice_by_section(markdown_text: str, section_id: str) -> str:
    """按 section_id (如 "Section 2" 或 "2") 或标题关键字切片提取。"""
    lines = markdown_text.splitlines()
    heading_pattern = re.compile(r"^(#{1,4})\s+(.+)$")
    
    sections = []
    for idx, line in enumerate(lines, start=0):
        match = heading_pattern.match(line.strip())
        if match:
            sections.append((idx, match.group(2).strip()))
            
    if not sections:
        return markdown_text
        
    target_idx = -1
    sec_num_match = re.search(r"\d+", section_id)
    if sec_num_match:
        num = int(sec_num_match.group()) - 1
        if 0 <= num < len(sections):
            target_idx = num
            
    if target_idx == -1:
        for i, (_, title) in enumerate(sections):
            if section_id.lower() in title.lower():
                target_idx = i
                break
                
    if target_idx == -1:
        from trawler.errors import format_error
        return format_error(
            "section-not-found",
            f"Section '{section_id}' not found in document. Available count: {len(sections)}",
        )
        
    start_line = sections[target_idx][0]
    end_line = len(lines) if target_idx == len(sections) - 1 else sections[target_idx + 1][0]
    
    section_content = "\n".join(lines[start_line:end_line])
    
    header = f"--- [Section {target_idx + 1} / {len(sections)}: {sections[target_idx][1]}] ---\n\n"
    footer = f"\n\n--- [End of Section {target_idx + 1}] ---"
    
    return header + section_content + footer


def slice_by_tokens(markdown_text: str, chunk_index: int = 1, chunk_size: int = 4000) -> str:
    """按字数/Token 分页切片。"""
    total_len = len(markdown_text)
    if total_len <= chunk_size:
        return markdown_text
        
    total_chunks = (total_len + chunk_size - 1) // chunk_size
    if chunk_index < 1 or chunk_index > total_chunks:
        from trawler.errors import format_error

        return format_error(
            "chunk-not-found",
            f"Chunk {chunk_index} not found. Available chunks: 1-{total_chunks}",
        )
    
    start = (chunk_index - 1) * chunk_size
    end = min(start + chunk_size, total_len)
    
    header = (
        f"--- [Chunk {chunk_index}/{total_chunks} "
        f"(Chars {start}-{end} of {total_len})] ---\n\n"
    )
    footer = (
        f"\n\n--- [End of Chunk {chunk_index}/{total_chunks}. "
        f"has_next={'true' if chunk_index < total_chunks else 'false'}"
    )
    if chunk_index < total_chunks:
        footer += f", next_chunk_index={chunk_index + 1}"
    footer += "] ---"
    
    return header + markdown_text[start:end] + footer
