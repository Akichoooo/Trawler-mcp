"""
safety — 爬虫数据安全过滤与反提示词注入层。

包含：
1. 阻断/转义 LLM 聊天控制 Token（如 <|im_start|>, [INST] ）。
2. 打散/破坏潜在的全局指令覆盖文本（如 "Ignore all previous instructions" ）（插入零宽空格 \u200b）。
3. 前置移除隐藏的恶意注入 HTML 标签（如 display:none, font-size:0 ）。
4. PII（个人敏感信息）自动脱敏掩码（手机号、身份证、邮箱、银行卡）。
5. 热加载敏感词库（从 data/sensitive_words.txt 自动加载并脱敏过滤）。
"""

from __future__ import annotations

import logging
import os
import re
import threading

log = logging.getLogger("trawler.parser.safety")

# ----------------- 1. 控制 Token 与系统标签正则 -----------------
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

FAKE_SYSTEM_TAGS_RE = re.compile(
    r"</?(system|instruction|instruction_override|ai_rules|sys|assistant|user|developer)>",
    re.IGNORECASE
)

INJECTION_KEYWORDS = [
    "ignore", "instructions", "override", "system prompt",
    "forget", "prior", "previous", "developer mode",
    "jailbreak", "do not tell", "print instead"
]

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

# ----------------- 2. PII 敏感信息脱敏正则 -----------------
# 手机号 (匹配 11 位大陆手机号)
PHONE_RE = re.compile(r"\b((?:\+?86)?)(1[3-9]\d{9})\b")
# 身份证号 (18位大陆身份证)
ID_CARD_RE = re.compile(r"\b([1-9]\d{5})(\d{10})(\d[0-9Xx])\b")
# 邮箱
EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Z|a-z]{2,})\b")
# 信用卡号 (16-19位)
CREDIT_CARD_RE = re.compile(r"\b(\d{4})[ -]?(\d{4,11})[ -]?(\d{4})\b")


# ----------------- 3. 热加载敏感词库 -----------------
_WORDS_CACHE: list[tuple[str, str]] = []
_WORDS_REGEX: re.Pattern | None = None
_LAST_MTIME = 0.0
_words_lock = threading.Lock()


def _get_words_file_path() -> str:
    # 查找敏感词库文件路径
    base_dir = os.path.abspath(".")
    words_file = os.path.join(base_dir, "data", "sensitive_words.txt")
    if not os.path.exists(words_file):
        # 兼容测试/打包子目录路径
        module_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        words_file = os.path.join(module_dir, "data", "sensitive_words.txt")
    return words_file


def load_sensitive_words() -> tuple[list[tuple[str, str]], re.Pattern | None]:
    """热加载敏感词，返回 (词组列表, 编译正则)。不重启服务。"""
    global _WORDS_CACHE, _WORDS_REGEX, _LAST_MTIME
    words_file = _get_words_file_path()
    if not os.path.exists(words_file):
        return [], None

    try:
        mtime = os.path.getmtime(words_file)
        if mtime <= _LAST_MTIME and _WORDS_REGEX is not None:
            return _WORDS_CACHE, _WORDS_REGEX

        with _words_lock:
            # 双重检查
            mtime = os.path.getmtime(words_file)
            if mtime <= _LAST_MTIME and _WORDS_REGEX is not None:
                return _WORDS_CACHE, _WORDS_REGEX

            new_words = []
            with open(words_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if ":" in line:
                        cat, val = line.split(":", 1)
                        new_words.append((cat.strip().upper(), val.strip()))
                    else:
                        new_words.append(("CUSTOM", line))

            # 编译为单一正则表达式，匹配速度极快
            if new_words:
                # 按长度降序排列，防止短词拦截长词匹配
                sorted_words = sorted(new_words, key=lambda x: len(x[1]), reverse=True)
                patterns = []
                for cat, val in sorted_words:
                    # 转义特殊正则符号，构建匹配组
                    safe_val = re.escape(val)
                    patterns.append(f"(?P<{cat}_{hash(val) & 0xffffffff}>{safe_val})")
                
                regex_str = "|".join(patterns)
                _WORDS_REGEX = re.compile(regex_str, re.IGNORECASE)
            else:
                _WORDS_REGEX = None

            _WORDS_CACHE = new_words
            _LAST_MTIME = mtime
            log.info("Successfully hot-loaded %d sensitive words from %s", len(_WORDS_CACHE), words_file)
    except Exception as e:
        log.warning("Failed to hot-load sensitive words: %s", e)

    return _WORDS_CACHE, _WORDS_REGEX


# ----------------- 4. 脱敏与净化核心逻辑 -----------------

def _neutralize_text(text: str) -> str:
    """插入零宽空格破开注入特征"""
    def inject_zws(match: re.Match) -> str:
        word = match.group(0)
        if len(word) > 2:
            return f"{word[:2]}\u200b{word[2:]}"
        return word

    for kw in INJECTION_KEYWORDS:
        text = re.sub(rf"({kw})", inject_zws, text, flags=re.IGNORECASE)
    return text


def mask_pii(text: str) -> str:
    """对 PII（手机号、身份证、邮箱、银行卡）进行掩码脱敏"""
    if not text:
        return text

    # ① 脱敏手机号 (如 13812345678 -> 138****5678)
    def replace_phone(match: re.Match) -> str:
        prefix, num = match.group(1), match.group(2)
        masked = f"{num[:3]}****{num[7:]}"
        return f"{prefix}{masked}"
    text = PHONE_RE.sub(replace_phone, text)

    # ② 脱敏身份证号 (如 110101199003072345 -> 110101**********45)
    def replace_id_card(match: re.Match) -> str:
        prefix, birth_and_tail, last = match.group(1), match.group(2), match.group(3)
        return f"{prefix}**********{last}"
    text = ID_CARD_RE.sub(replace_id_card, text)

    # ③ 脱敏信用卡/银行卡 (如 6222021000123456789 -> 6222**********6789)
    def replace_credit_card(match: re.Match) -> str:
        first, middle, last = match.group(1), match.group(2), match.group(3)
        return f"{first}{'*' * len(middle)}{last}"
    text = CREDIT_CARD_RE.sub(replace_credit_card, text)

    # ④ 脱敏邮箱 (如 abcdefg@example.com -> a****g@example.com)
    def replace_email(match: re.Match) -> str:
        name, domain = match.group(1), match.group(2)
        if len(name) > 2:
            masked_name = f"{name[0]}{'*' * (len(name) - 2)}{name[-1]}"
        else:
            masked_name = f"{name[0]}*"
        return f"{masked_name}@{domain}"
    text = EMAIL_RE.sub(replace_email, text)

    return text


def mask_sensitive_words(text: str) -> str:
    """利用热加载词库，对抓取内容中的涉敏、违法、越狱命令词汇进行掩码屏蔽"""
    _, regex = load_sensitive_words()
    if not regex or not text:
        return text

    def replace_word(match: re.Match) -> str:
        # 获取匹配的组名
        group_name = match.lastgroup
        if not group_name:
            return "[MASKED_SENSITIVE]"
        
        # 解析类别 (类别名_hash)
        category = group_name.rsplit("_", 1)[0]
        word = match.group(0)
        log.warning("Masked sensitive word: %s (Category: %s)", word, category)
        return f"[MASKED_{category}]"

    return regex.sub(replace_word, text)


def sanitize_markdown(md: str) -> str:
    """全面过滤管道"""
    if not md:
        return md

    # ① 转义 LLM 控制 Token
    def replace_control(match: re.Match) -> str:
        token = match.group(0)
        log.warning("Detected potential LLM control token injection: %s", token)
        return f"[SECURE_MUTED: {token[0]}\u200b{token[1:]}]"
    md = CONTROL_TOKENS_RE.sub(replace_control, md)

    # ② 转义仿冒系统标签
    def replace_fake_tag(match: re.Match) -> str:
        tag = match.group(0)
        log.warning("Detected potential fake system tag: %s", tag)
        return f"[SECURE_TAG_MUTED: {tag.replace('<', '&lt;').replace('>', '&gt;')}]"
    md = FAKE_SYSTEM_TAGS_RE.sub(replace_fake_tag, md)

    # ③ 中和间接指令注入短语
    def replace_injection_phrase(match: re.Match) -> str:
        phrase = match.group(0)
        log.info("Neutralizing potential prompt injection phrase: %s", phrase)
        return _neutralize_text(phrase)
    md = INJECTION_PHRASES_RE.sub(replace_injection_phrase, md)

    # ④ PII 脱敏掩码
    md = mask_pii(md)

    # ⑤ 词库敏感词掩码脱敏
    md = mask_sensitive_words(md)

    return md


def sanitize_html_soup(soup) -> None:
    """移除前置隐藏样式和恶意网页 CSS 注入"""
    try:
        hidden_tags = soup.find_all(lambda tag: tag.has_attr('style'))
        for tag in hidden_tags:
            style = tag['style'].lower().replace(" ", "")
            if "display:none" in style or "visibility:hidden" in style:
                log.info("Removed hidden HTML element: <%s style='%s'>", tag.name, tag['style'])
                tag.decompose()
                continue
            
            font_size_match = re.search(r"font-size:(0(px|em|pt|%)?|0)", style)
            if font_size_match:
                log.info("Removed zero-font HTML element: <%s style='%s'>", tag.name, tag['style'])
                tag.decompose()
                continue
    except Exception as e:
        log.warning("HTML safety soup sanitization failed: %s", e)
