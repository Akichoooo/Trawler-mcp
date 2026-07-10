"""challenge_detect — 移植自 fish，2026 增强。

is_challenge_page: 检测是否被反爬挑战页挡住 (Cloudflare 5s / CAPTCHA / Akamai / DataDome)。
喂给 detect 的 L1 层。

2026 增强维度:
- Akamai _abck cookie 失效标记 (~-1~ / ~0~ 开头表示 sensor_data 未通过)
- DataDome dd-key / datadome cookie 签名
- Turnstile 嵌入式挑战 (cf-turnstile)
- 状态码集合扩展: 401/425/503 (2026 厂商常用)
"""
from __future__ import annotations

# 强签名: 命中即判定为挑战页
_STRONG_SIGS = (
    "cf-browser-verification",
    "cf_chl_opt",
    "cf-mitigated",
    "challenge-platform",
    "Just a moment",        # Cloudflare 5s 页标题
    "Checking your browser",# 常见挑战页文案
    "Attention Required! | Cloudflare",
    "ddos protection by",   # 通用 DDoS 防护
    "cdn-cgi/challenge",
    "PxDLPixel",            # PerimeterX
    "_pxCaptcha",
    "cf-turnstile",          # Cloudflare Turnstile 嵌入式
    "datadome",             # DataDome 签名
    "geo.captcha-delivery", # DataDome CDN 路径
    "kasada",               # Kasada (WASM 挑战, 2026 新增)
    "ips.js",               # Imperva/Incapsula
    "incap_ses",            # Incapsula session cookie
)

# 弱签名: 需要组合命中或特殊上下文才判定
_WEAK_SIGS = (
    "recaptcha",
    "g-recaptcha",
    "hcaptcha",
    "arkoselabs",
    "funcaptcha",
)

# SPA 动态加载失败占位文案 (非反爬, 是运行时 API 拉取失败)。
# Webflow/Vue/React 等 SPA 框架渲染了外壳, 但动态区从后端 API 拉数据失败时显示的占位。
# 这类页面字数可能过 L3 阈值 (框架+导航+页脚), 但正文是脏的加载失败提示。
#
# 分强弱两档防误伤:
# - 强词: 明确的加载失败指令式文案, 单独命中即可判
# - 弱词: 可能在正文中出现的短语, 需配合强词或 SPA 框架特征才判
_SPA_LOAD_FAILURE_STRONG = (
    "data loading failed",
    "please check your network",
    "no content available",
    "加载失败",
    "请检查网络",
    "暂无内容",
    "重新加载",
    "loading error",
)
_SPA_LOAD_FAILURE_WEAK = (
    "check back later",
    "something went wrong",
    "failed to load",
)
# SPA 框架特征 (弱词命中时要求同时出现, 才判加载失败, 防正文误伤)
_SPA_FRAMEWORK_MARKERS = (
    "data-wf-page",      # Webflow
    "data-wf-site",
    "data-reactroot",    # React
    "__nuxt",            # Nuxt
    "__next",            # Next.js
    "data-v-",           # Vue
    "w-dyn-item",        # Webflow 动态列表
)


# 通用拦截状态码 / 文案
# 2026 扩展: 401 (Akamai BMP 鉴权挑战), 425 Too Early (TLS 1.3 反爬), 503 (挑战页常用)
_BLOCK_STATUSES = {401, 403, 425, 429, 503}
_BLOCK_TEXTS = (
    "access denied",
    "forbidden",
    "rate limit",
    "too many requests",
    "unauthorized",
    "captcha",
    "are you a robot",
    "verify you are human",
    "access to this page has been denied",
    "px-captcha",               # PerimeterX
    "datadome",                 # DataDome
    "robot check",              # Amazon
    "bot detection",
    "security check",
)


def is_challenge_page(html: str, http_status: int = 200) -> bool:
    """检测是否厂商挑战页 (Cloudflare/Px/Datadome/Akamai 签名)。True = 被厂商挡。

    注意: 仅看厂商签名, 不看状态码 (状态码归 L2 通用拦截)。
    Akamai _abck 失效标记检测在 is_akamai_abck_invalid。
    """
    if not html:
        return False
    html_lower = html.lower()[:20000]  # 只看前 20KB 加速

    # 强签名直接判
    if any(sig.lower() in html_lower for sig in _STRONG_SIGS):
        return True

    # 弱签名需累计命中, 且要求更严苛的 HTML 上下文 (如明确的脚本/标签特征)
    # 此处简化为要求命中 2 个以上的不同弱签名, 防止单文中提到导致误伤
    weak_hits = sum(1 for sig in _WEAK_SIGS if sig.lower() in html_lower)
    if weak_hits >= 2:
        # 为了进一步防误判，只有在包含确切的 DOM 结构词时才认
        dom_sigs = ["<iframe", "<script", "g-recaptcha", "cf-turnstile"]
        if any(d in html_lower for d in dom_sigs):
            return True

    return False


def is_akamai_abck_invalid(html: str, cookie_header: str = "") -> bool:
    """检测 Akamai _abck cookie 是否失效 (sensor_data 未通过验证)。

    Akamai BMP 用 _abck cookie 跟踪 sensor_data 验证状态:
    - 格式 `...~-1~...` 或 `...~0~...` 开头段 → 失效, 需重新跑 sensor
    - 格式 `...~0~...` 中段含 `~-1~` → 待验证
    - 格式 `...~1~...` 或无 ~-1~ → 已通过 (不阻断)

    2026 关键: Akamai 不返回传统挑战页, 而是静默标记 cookie 后 403,
    必须看 cookie 而非 HTML 才能识别。

    cookie_header: Set-Cookie 或 Cookie header 全文 (可选, 拿不到传空)。
    """
    # 从 cookie header 提 _abck 值
    abck_value = ""
    if cookie_header:
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.lower().startswith("_abck="):
                abck_value = part.split("=", 1)[1]
                break
    if abck_value:
        # 失效标记: cookie 值含 ~-1~ 或 ~0~- (待验证且未通过)
        # 已通过: ~0~...~1~ 或不含 ~-1~
        if "~-1~" in abck_value:
            return True
    # 退化: HTML 含 Akamai 鉴权失败特征
    if html:
        html_lower = html.lower()[:5000]
        if "access denied" in html_lower and "akamai" in html_lower:
            return True
        # Akamai BMP 静默挑战: body 极短 + 引用 akamaized.net
        if len(html) < 500 and "akamaized.net" in html_lower:
            return True
    return False


def is_blocked_status(http_status: int) -> bool:
    """通用拦截状态码。"""
    return http_status in _BLOCK_STATUSES


def has_block_text(html: str) -> bool:
    """通用拦截文案。"""
    if not html:
        return False
    html_lower = html.lower()[:20000]
    return any(t in html_lower for t in _BLOCK_TEXTS)


def has_spa_load_failure(html: str) -> str | None:
    """检测 SPA 动态加载失败占位。返回命中的文案片段, 或 None。

    与 has_block_text 的区别: 这不是反爬拦截, 而是页面框架渲染了但动态区
    (从后端 API 拉取的内容) 加载失败。这类页面字数可能过 L3 阈值, 但正文
    混着 "Data loading failed" 等脏占位。4seas.xyz (Webflow SPA) 实测命中。

    分强弱两档防误伤:
    - 强词 (data loading failed / 加载失败 等): 单独命中即判
    - 弱词 (check back later / something went wrong): 需配合 SPA 框架特征才判,
      避免正文中提到这些短语的文章被误杀
    """
    if not html:
        return None
    html_lower = html.lower()[:20000]
    # 强词: 单独命中即判
    for t in _SPA_LOAD_FAILURE_STRONG:
        if t in html_lower:
            return t
    # 弱词: 需配合 SPA 框架特征
    has_framework = any(m in html_lower for m in _SPA_FRAMEWORK_MARKERS)
    if has_framework:
        for t in _SPA_LOAD_FAILURE_WEAK:
            if t in html_lower:
                return t
    return None
