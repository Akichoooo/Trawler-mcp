"""test_detect.py - 测试 detect.py 的 3 层主动检测短路逻辑。"""

from trawler.fetcher.detect import Verdict, detect, verify_session_valid


def test_detect_l1_cloudflare():
    # 包含 Cloudflare 强特征的 HTML
    html = '<html><head><title>Just a moment...</title></head><body>cf-browser-verification</body></html>'
    res = detect(html, 403)
    assert res.verdict == Verdict.BLOCKED_CLOUDFLARE
    assert res.should_shortcircuit == "jina"

    # 测试弱特征且不满足累计条件不会误杀
    html_tech_article = '<html><body>In this article we discuss how recaptcha works.</body></html>'
    res2 = detect(html_tech_article, 200)
    assert res2.verdict != Verdict.BLOCKED_CLOUDFLARE


def test_detect_l1_login():
    # 登录墙跳转
    html = '<html><body>Redirecting to <a href="/login?next=/">login</a></body></html>'
    res = detect(html, 200, final_url="https://example.com/login")
    assert res.verdict == Verdict.BLOCKED_LOGIN
    assert res.should_shortcircuit == "hitl"

    # 测试路径子串误判 (如 /author/)
    html_author = '<html><body>About the author</body></html>'
    res2 = detect(html_author, 200, final_url="https://example.com/author/john")
    assert res2.verdict != Verdict.BLOCKED_LOGIN


def test_detect_l2_generic_block():
    # 429 Too Many Requests
    html = '<html><body>Too Many Requests</body></html>'
    res = detect(html, 429)
    assert res.verdict == Verdict.BLOCKED_GENERIC
    assert res.should_shortcircuit is None

    # 空 body
    res2 = detect("   ", 200)
    assert res2.verdict == Verdict.BLOCKED_GENERIC
    assert res2.should_shortcircuit is None


def test_detect_l3_empty_content():
    # 拿到完整的 HTML，但是没有任何实质内容 (例如只有外壳没有内容的 SPA)
    html = '<html><body><div id="root"></div><script src="app.js"></script></body></html>'
    res = detect(html, 200)
    assert res.verdict == Verdict.EMPTY
    assert res.should_shortcircuit is None

    # bypass_l3 能绕过检查
    res_bypassed = detect(html, 200, bypass_l3=True)
    assert res_bypassed.verdict == Verdict.OK


def test_detect_ok():
    # 正常的文章页面
    html = '''
    <html>
      <head><title>Test Article</title></head>
      <body>
        <h1>This is a valid article</h1>
        <article>
          <p>This paragraph contains enough text to satisfy the L3 MIN_CONTENT_CHARS requirement. 
          It has a lot of words so the parser knows it is not an empty page or a blocked page.</p>
        </article>
      </body>
    </html>
    '''
    res = detect(html, 200)
    assert res.verdict == Verdict.OK
    assert res.should_shortcircuit is None
    assert res.is_ok is True


def test_detect_spa_load_incomplete():
    # SPA 框架渲染了, 字数过 L3 阈值, 但动态区加载失败 (4seas.xyz 实测场景)
    html = '''<html><head><title>4Seas</title></head><body>
    <nav>Home About Events Contact</nav>
    <main><h1>Events</h1><p>Data loading failed. Please check your network and click retry.</p>
    <p>No content available. Please check back later.</p></main>
    <footer>4Seas Nimman Chiang Mai</footer></body></html>'''
    res = detect(html, 200)
    assert res.verdict == Verdict.SPA_LOAD_INCOMPLETE
    assert res.should_shortcircuit is None
    assert "SPA load failure" in res.reason


def test_detect_spa_load_bypass_l3():
    # bypass_l3=True 时跳过 SPA 加载失败检测 (与 L3 同级)
    html = '<html><body><main><p>Data loading failed. Please check your network.</p></main></body></html>'
    res = detect(html, 200, bypass_l3=True)
    assert res.verdict == Verdict.OK


def test_detect_spa_no_false_positive():
    # 正常文章里提到 "check back later" 不应误判为加载失败
    # (要求同时有明确的加载失败占位特征, 而非正文语境提及)
    html = '''<html><body><article>
    <h1>How to handle API errors</h1>
    <p>When a service is down, users should check back later. This is a normal article
    discussing error handling patterns. It has plenty of substantive content about
    retry strategies, circuit breakers, and graceful degradation techniques used in
    production systems.</p></article></body></html>'''
    res = detect(html, 200)
    # "check back later" 在正文语境, 但没有其他加载失败占位特征 -> OK
    assert res.verdict == Verdict.OK


def test_verify_session_valid():
    # 测试有效情况 (普通页面)
    html_ok = '<html><body><h1>Dashboard</h1><p>Welcome back</p></body></html>'
    assert verify_session_valid(html_ok, "https://example.com/dashboard") is True

    # 测试修改密码页不误判
    html_settings = '<html><body><form><input type="password" name="password"/><button>Change</button></form></body></html>'
    assert verify_session_valid(html_settings, "https://example.com/settings/security") is True

    # 测试 302 跳转到 login
    html_redirect = '<html><body>Redirecting...</body></html>'
    assert verify_session_valid(html_redirect, "https://example.com/auth/login?next=/") is False

    # 测试明显包含 session expired 文案
    html_expired = '<html><body><div class="alert">Session expired. Please log in again.</div></body></html>'
    assert verify_session_valid(html_expired, "https://example.com/dashboard") is False
