"""detect — 3 层主动检测 + 短路决策。

拿到 fetcher 响应立刻判, 不等超时:
  L1 厂商签名: Cloudflare / Akamai / Datadome → 短路跳 Jina
  L2 通用拦截: 403/429/"Just a moment"/空body → 降级
  L3 结构完整性: 空正文 → wait_for_selector 再判 + 字数双判据 → 降级
  bypass_l3=true 时跳过 L3
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from trawler import config
from trawler.fetcher import challenge_detect


class Verdict(StrEnum):
    OK = "ok"                          # 正常, 进 Parser
    BLOCKED_CLOUDFLARE = "blocked_cloudflare"   # L1 命中, 短路跳 Jina
    BLOCKED_LOGIN = "blocked_login"             # 302->login, 短路跳 HITL
    BLOCKED_GENERIC = "blocked_generic"        # L2 通用拦截, 降级
    SPA_LOAD_INCOMPLETE = "spa_load_incomplete"  # SPA 动态加载失败, 降级
    EMPTY = "empty"                             # L3 空正文, 降级


@dataclass
class DetectionResult:
    verdict: Verdict
    reason: str = ""

    @property
    def is_ok(self) -> bool:
        return self.verdict == Verdict.OK

    @property
    def should_shortcircuit(self) -> str | None:
        """返回应短路跳到的 rung, 或 None (走默认降级)。"""
        if self.verdict == Verdict.BLOCKED_CLOUDFLARE:
            return "jina"
        if self.verdict == Verdict.BLOCKED_LOGIN:
            return "hitl"
        return None


def detect(
    html: str,
    http_status: int = 200,
    *,
    bypass_l3: bool = False,
    final_url: str = "",
) -> DetectionResult:
    """3 层主动检测。返回决策。
    
    L1 (厂商签名/登录流): 遇到 Cloudflare 等强盾则短路跳 jina; 遇到 302 登录墙短路跳 hitl。
    L2 (通用拦截): 遇到 403/429 或明显的阻断文本/空响应, 则宣告本档失败, 继续下一档降级。
    L3 (结构完整性): 基于字数与 DOM 特征判断是否有实质内容, 若空则降级 (可 bypass_l3)。
    """
    # L1: 先判登录跳转 (302→login) → 短路跳 HITL
    if _is_login_redirect(html, final_url):
        return DetectionResult(Verdict.BLOCKED_LOGIN, "302→login detected")

    # L1: 厂商签名 (Cloudflare/Px/Datadome) → 短路跳 Jina
    if challenge_detect.is_challenge_page(html, http_status):
        return DetectionResult(Verdict.BLOCKED_CLOUDFLARE, "challenge page signature")

    # L2: 通用拦截 → 降级
    if challenge_detect.is_blocked_status(http_status):
        return DetectionResult(Verdict.BLOCKED_GENERIC, f"HTTP {http_status}")
    if not html or not html.strip():
        return DetectionResult(Verdict.BLOCKED_GENERIC, "empty body")
    if challenge_detect.has_block_text(html):
        return DetectionResult(Verdict.BLOCKED_GENERIC, "block text detected")

    # L2.5: SPA 动态加载失败 (框架在但动态区 API 拉取失败, 如 "Data loading failed")
    # 放在 L3 之前: 加载失败是比"空正文"更具体的诊断。与 L3 同级, bypass_l3 可跳过。
    if not bypass_l3:
        spa_fail = challenge_detect.has_spa_load_failure(html)
        if spa_fail:
            return DetectionResult(
                Verdict.SPA_LOAD_INCOMPLETE, f"SPA load failure: '{spa_fail}'"
            )

    # L3: 结构完整性 (除非 bypass)
    if not bypass_l3:
        if _is_empty_content(html):
            return DetectionResult(Verdict.EMPTY, "no extractable content (SPA or blocked)")

    return DetectionResult(Verdict.OK)


def _is_login_redirect(html: str, final_url: str) -> bool:
    """检测是否跳到了登录页 (302→login)。保守判定。"""
    from urllib.parse import urlparse
    
    final = (final_url or "").lower()
    path = urlparse(final).path or "/"
    
    login_paths = {"/login", "/signin", "/auth", "/account/login", "/sessions"}
    if any(path == p or path.startswith(f"{p}/") or path.startswith(f"{p}?") for p in login_paths):
        return True
        
    # HTML 里有登录表单特征 (避免由于改密码页误判, 这里需非常谨慎, 此前已在 verify_session_valid 中解耦)
    html_lower = html.lower()[:5000] if html else ""
    if 'name="password"' in html_lower or 'type="password"' in html_lower:
        if "login" in html_lower or "sign in" in html_lower:
            return True
    return False


def _is_empty_content(html: str) -> bool:
    """L3 双判据: DOM 选择器 + 提取后字数。

    SPA 初始 HTML 可能只有 <div id=root>, 字数极低 → 判空。
    注意: 真正判空最终靠 parser 提取后的字数, 这里是预判 (fetcher 阶段)。
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        # 找有意义的正文标签
        for tag in soup.find_all(["p", "article", "h1", "h2", "h3", "li", "td", "div"]):
            text = tag.get_text(strip=True)
            if len(text) >= config.MIN_CONTENT_CHARS:
                return False  # 找到一段够长的正文 → 非空
        # 全部标签文本总和也很少
        all_text = soup.get_text(strip=True)
        return len(all_text) < config.MIN_CONTENT_CHARS
    except Exception:
        # bs4 不可用 → 退回纯文本长度判
        import re
        text = re.sub(r"<[^>]+>", "", html)
        text = re.sub(r"\s+", " ", text).strip()
        return len(text) < config.MIN_CONTENT_CHARS


def verify_session_valid(html: str, final_url: str) -> bool:
    """校验已登录的 storage_state 是否有效。
    
    检测是否被踢回登录页, 或页面包含鉴权失效特征。返回 True 表示仍然有效。
    为避免误判设置页的修改密码框，这里使用严格匹配：只看 URL 和明确的 session 提示文案。
    """
    final = (final_url or "").lower()
    from urllib.parse import urlparse
    try:
        path = urlparse(final).path.rstrip("/")
    except Exception:
        path = ""
    login_paths = ("/login", "/signin", "/auth", "/account/login", "/sessions")
    if any(path == p or path.startswith(p + "/") for p in login_paths):
        return False
    
    html_lower = html.lower()[:5000] if html else ""
    expired_texts = ["session expired", "please log in", "please login", "unauthorized", "sign in to continue"]
    if any(t in html_lower for t in expired_texts):
        return False
        
    return True
