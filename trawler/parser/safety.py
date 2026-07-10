"""
safety — 爬虫数据安全过滤与反提示词注入层。

防范间接提示词注入（Indirect Prompt Injection）：
1. 阻断/转义 LLM 聊天控制 Token（如 <|im_start|>, [INST] ）。
2. 打散/破坏潜在的全局指令覆盖文本（如 "Ignore all previous instructions" ），通过插入零宽空格（\u200b）破坏 Tokenizer 语义。
3. 转义可能被用来伪造系统指令的 XML/HTML 标签（如 <system> ）。
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("trawler.parser.safety")

# 1. 聊天控制 Token 列表 (各种常见开源/闭源模型)
CONTROL_TOKENS_RE = re.compile(
    r"("
    r"<\|im_start\|>|<\|im_end\|>|"
    r"<\|begin_of_text\|>|<\|end_of_text\|>|"
    r"<\|start_header_id\|>|<\|end_header_id\|>|"
    r"\[INST\]|\[/INST\]|"
    r"<s>|</s>|"
    r"__PROMPT_INJECTION__|__TRAWLER_ERROR__|__TRAWLER_OK__"
    r")",
    re.IGNORECASE
)

# 2. 常见伪造系统角色标签
FAKE_SYSTEM_TAGS_RE = re.compile(
    r"</?(system|instruction|instruction_override|ai_rules|sys|assistant|user|developer)>",
    re.IGNORECASE
)

# 3. 常见指令覆盖触发词 (注入词)
INJECTION_KEYWORDS = [
    "ignore", "instructions", "override", "system prompt",
    "forget", "prior", "previous", "developer mode",
    "jailbreak", "do not tell", "print instead"
]

# 组装正则：匹配这些敏感词及其近义词组合
INJECTION_PHRASES_RE = re.compile(
    r"("
    r"(ignore|forget|override|bypass)\s+(all\s+)?(previous|prior|original)?\s*(instructions|prompts|rules|system)|"
    r"you\s+must\s+now\s+act\s+as|"
    r"new\s+instruction|"
    r"system\s+message\s*:|"
    r"assistant\s+override|"
    r"stop\s+(writing|outputting|answering)|"
    r"instead\s+of\s+following"
    r")",
    re.IGNORECASE
)


def _neutralize_text(text: str) -> str:
    """通过插入零宽空格 (ZWS, \u200b) 破坏文本对 Tokenizer 的语义连贯性，

    同时保留人类可读性。
    例如: "ignore instructions" -> "i\u200bgnore in\u200bstructions"
    """
    def inject_zws(match: re.Match) -> str:
        word = match.group(0)
        if len(word) > 2:
            # 在中间插入零宽空格
            return f"{word[:2]}\u200b{word[2:]}"
        return word

    # 对匹配到的注入短语词汇进行微观分割
    for kw in INJECTION_KEYWORDS:
        text = re.sub(rf"({kw})", inject_zws, text, flags=re.IGNORECASE)
    return text


def sanitize_markdown(md: str) -> str:
    """清理与过滤 Markdown，拦截潜在的提示词注入和标记欺骗。"""
    if not md:
        return md

    # ① 拦截/转义 LLM 控制 Token
    def replace_control(match: re.Match) -> str:
        token = match.group(0)
        # 用零宽空格打碎 control token
        log.warning("Detected potential LLM control token injection: %s", token)
        return f"[SECURE_MUTED: {token[0]}\u200b{token[1:]}]"

    md = CONTROL_TOKENS_RE.sub(replace_control, md)

    # ② 拦截/转义系统标签
    def replace_fake_tag(match: re.Match) -> str:
        tag = match.group(0)
        log.warning("Detected potential fake system tag: %s", tag)
        return f"[SECURE_TAG_MUTED: {tag.replace('<', '&lt;').replace('>', '&gt;')}]"

    md = FAKE_SYSTEM_TAGS_RE.sub(replace_fake_tag, md)

    # ③ 处理间接注入短语
    def replace_injection_phrase(match: re.Match) -> str:
        phrase = match.group(0)
        log.info("Neutralizing potential prompt injection phrase: %s", phrase)
        # 破坏短语的连贯性
        return _neutralize_text(phrase)

    md = INJECTION_PHRASES_RE.sub(replace_injection_phrase, md)

    return md


def sanitize_html_soup(soup) -> None:
    """针对 BeautifulSoup DOM 树的前置安全加固。

    移除 CSS 隐藏、零字体大小等可能欺骗视觉/大模型的隐藏注入元素。
    """
    try:
        # 1. 查找带有隐藏样式的标签
        hidden_tags = soup.find_all(lambda tag: tag.has_attr('style'))
        for tag in hidden_tags:
            style = tag['style'].lower().replace(" ", "")
            # display:none, visibility:hidden
            if "display:none" in style or "visibility:hidden" in style:
                log.info("Removed hidden HTML element: <%s style='%s'>", tag.name, tag['style'])
                tag.decompose()
                continue
            
            # font-size: 0px / 0pt / 0%
            font_size_match = re.search(r"font-size:(0(px|em|pt|%)?|0)", style)
            if font_size_match:
                log.info("Removed zero-font HTML element: <%s style='%s'>", tag.name, tag['style'])
                tag.decompose()
                continue

            # color matches background color (invisible text injection)
            # e.g., color: white; background: white;
            # 这种攻击在真实注入中常用于欺骗 LLM 提取不可见指令。
            # 目前只做最简单的 decompose。
    except Exception as e:
        log.warning("HTML safety soup sanitization failed: %s", e)
