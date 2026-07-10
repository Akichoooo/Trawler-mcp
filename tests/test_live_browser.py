import pytest


class FakeLocator:
    def __init__(self, page=None, selector=""):
        self.page = page
        self.selector = selector

    async def inner_text(self, timeout=5000):
        return "Visible body text"

    async def screenshot(self, timeout=10000):
        return b"\x89PNG\r\n\x1a\nelement"

    async def click(self, timeout=30000):
        if self.selector == ".boom":
            raise RuntimeError("click failed")
        self.page.calls.append(("click", self.selector))

    async def fill(self, text, timeout=30000):
        self.page.calls.append(("fill", self.selector, text))

    async def type(self, text, timeout=30000):
        self.page.calls.append(("type", self.selector, text))

    async def press(self, key, timeout=30000):
        self.page.calls.append(("press", self.selector, key))

    async def wait_for(self, timeout=30000):
        self.page.calls.append(("locator_wait_for", self.selector))

    async def scroll_into_view_if_needed(self, timeout=30000):
        self.page.calls.append(("scroll_into_view", self.selector))

    async def check(self, timeout=30000):
        self.page.calls.append(("check", self.selector))

    async def uncheck(self, timeout=30000):
        self.page.calls.append(("uncheck", self.selector))

    async def select_option(self, value, timeout=30000):
        self.page.calls.append(("select_option", self.selector, value))


class FakeCdpSession:
    def __init__(self, context):
        self.context = context

    async def send(self, method, params=None):
        self.context.cdp_calls.append((method, params or {}))
        return {
            "nodes": [
                {"role": {"value": "RootWebArea"}, "name": {"value": "Example"}},
                {"role": {"value": "heading"}, "name": {"value": "Hello"}},
                {"role": {"value": "button"}, "name": {"value": "Submit"}},
                {"role": {"value": "textbox"}, "value": {"value": "SECRET_PASSWORD"}},
            ]
        }


class FakePage:
    def __init__(self):
        self.url = "https://example.com/current"
        self.calls = []

    async def content(self):
        return """
        <html><body>
          <main>
            <div class="target"><h1>Hello</h1><p>Selected body text.</p></div>
            <a href="/docs">Docs link</a>
            <div class="other">Ignore me</div>
          </main>
        </body></html>
        """

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def title(self):
        return "Example title"

    async def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        self.calls.append(("goto", url, wait_until, timeout))
        self.url = url

    async def wait_for_selector(self, selector, timeout=30000):
        self.calls.append(("wait_for_selector", selector))

    async def evaluate(self, script, *args):
        if "__TRAWLER_PICKED_ELEMENT" in script and "|| null" in script:
            return {
                "selector": ".target",
                "outerHTML": '<div class="target">Picked</div>',
                "text": "Picked",
                "rect": {"x": 10, "y": 20, "width": 100, "height": 50},
            }
        if "__TRAWLER_PICKED_REGION" in script and "|| null" in script:
            return {
                "rect": {"x": 10, "y": 20, "width": 100, "height": 50},
                "text": "Region text",
            }
        if "__TRAWLER_PICKED_ELEMENT" in script or "__TRAWLER_PICKED_REGION" in script:
            return {"ok": True}
        if args:
            if isinstance(args[0], dict):
                if "maxElements" in args[0]:
                    return {
                        "selector": args[0]["selector"],
                        "url": self.url,
                        "title": "Example title",
                        "element_count": 3,
                        "truncated": False,
                        "elements": [
                            {
                                "index": 0,
                                "selector": "button.login",
                                "tag": "button",
                                "role": "button",
                                "name": "Log in",
                                "text": "Log in",
                                "disabled": False,
                                "action_hints": ["click"],
                                "rect": {"x": 1, "y": 2, "width": 60, "height": 24},
                            },
                            {
                                "index": 1,
                                "selector": "input[name=password]",
                                "tag": "input",
                                "role": "textbox",
                                "name": "Password",
                                "text": "",
                                "disabled": False,
                                "input_type": "password",
                                "action_hints": ["fill", "press"],
                                "rect": {"x": 1, "y": 40, "width": 160, "height": 24},
                            },
                            {
                                "index": 2,
                                "selector": "a.docs",
                                "tag": "a",
                                "role": "link",
                                "name": "Docs",
                                "text": "Docs",
                                "href": "https://example.com/docs",
                                "disabled": False,
                                "action_hints": ["click"],
                                "rect": {"x": 1, "y": 80, "width": 80, "height": 20},
                            },
                        ],
                    }
                if "maxBlocks" in args[0]:
                    return {
                        "selector": args[0]["selector"],
                        "blockCount": 2,
                        "truncated": False,
                        "blocks": [
                            {
                                "tag": "div",
                                "selector": ".card:nth-of-type(1)",
                                "text": "Card one rendered text",
                                "rect": {"x": 10, "y": 20, "width": 120, "height": 80},
                            },
                            {
                                "tag": "div",
                                "selector": ".card:nth-of-type(2)",
                                "text": "Card two rendered text",
                                "rect": {"x": 20, "y": 120, "width": 120, "height": 80},
                            },
                        ],
                    }
                if "x" in args[0] and "y" in args[0]:
                    self.calls.append(("scroll", args[0]["x"], args[0]["y"]))
                    return None
                return {
                    "selector": args[0]["selector"],
                    "nodeCount": 2,
                    "tree": {"tag": "body", "children": []},
                }
            return {
                "selector": args[0],
                "outerHTML": '<div class="target">Hello</div>',
                "styles": {"display": "block", "color": "rgb(0, 0, 0)"},
                "children": [],
            }
        return "Visible body text"

    async def screenshot(self, full_page=True, timeout=10000, clip=None):
        return b"\x89PNG\r\n\x1a\nfake"


class FakeContext:
    def __init__(self):
        self.cdp_calls = []

    async def new_cdp_session(self, page):
        return FakeCdpSession(self)

    async def storage_state(self, indexed_db=True):
        return {"cookies": [], "origins": []}

    async def cookies(self, urls):
        return []

    async def close(self):
        return None


class FakePlaywright:
    async def stop(self):
        return None


@pytest.fixture(autouse=True)
def clear_live_sessions():
    from trawler import live_browser

    live_browser._LIVE_SESSIONS.clear()
    yield
    live_browser._LIVE_SESSIONS.clear()


@pytest.mark.asyncio
async def test_open_browser_session_requires_display(monkeypatch):
    from trawler import live_browser

    monkeypatch.setattr(live_browser, "PATCHRIGHT_AVAILABLE", True)
    monkeypatch.setattr(live_browser.hitl_rung, "has_display", lambda: False)

    result = await live_browser.open_browser_session("https://example.com/")

    assert result.startswith("__TRAWLER_ERROR__:")
    assert "human-window-unavailable" in result


@pytest.mark.asyncio
async def test_connect_browser_session_uses_adapter_without_vault(monkeypatch):
    from trawler import browser_adapter, live_browser

    async def fake_resolve(url):
        return False, "93.184.216.34"

    async def fake_connect(options):
        page = FakePage()
        page.url = options.url or "https://example.com/from-cdp"
        context = FakeContext()
        return browser_adapter.BrowserHandle(
            adapter_name="cdp",
            context=context,
            page=page,
            playwright=FakePlaywright(),
            route_guarded=True,
            owns_context=False,
            owns_page=False,
        )

    monkeypatch.setattr(live_browser, "PATCHRIGHT_AVAILABLE", True)
    monkeypatch.setattr(live_browser.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(live_browser.browser_adapter, "connect_cdp_browser", fake_connect)

    result = await live_browser.connect_browser_session(
        "http://127.0.0.1:9222",
        url="https://example.com/private",
    )

    assert result.startswith("__TRAWLER_OK__:")
    assert '"adapter_name": "cdp"' in result
    assert len(live_browser._LIVE_SESSIONS) == 1


@pytest.mark.asyncio
async def test_connect_browser_session_refuses_unguarded_adapter(monkeypatch):
    from trawler import browser_adapter, config, live_browser

    async def fake_resolve(url):
        return False, "93.184.216.34"

    async def fake_connect(options):
        page = FakePage()
        page.url = options.url or "https://example.com/from-cdp"
        return browser_adapter.BrowserHandle(
            adapter_name="cdp",
            context=FakeContext(),
            page=page,
            playwright=FakePlaywright(),
            route_guarded=False,
            owns_context=False,
            owns_page=False,
        )

    monkeypatch.setattr(config, "ALLOW_UNGUARDED_BROWSER", False)
    monkeypatch.setattr(live_browser, "PATCHRIGHT_AVAILABLE", True)
    monkeypatch.setattr(live_browser.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(live_browser.browser_adapter, "connect_cdp_browser", fake_connect)

    result = await live_browser.connect_browser_session(
        "http://127.0.0.1:9222",
        url="https://example.com/private",
    )

    assert result.startswith("__TRAWLER_ERROR__:")
    assert "unguarded session" in result


@pytest.mark.asyncio
async def test_open_browser_session_binds_and_reuses_account_profile(tmp_db, monkeypatch):
    from cryptography.fernet import Fernet

    from trawler import account_profiles, browser_adapter, live_browser

    async def fake_resolve(url):
        return False, "93.184.216.34"

    opened = []

    async def fake_open(options):
        opened.append(options)
        page = FakePage()
        page.url = options.url
        return browser_adapter.BrowserHandle(
            adapter_name="local_persistent",
            context=FakeContext(),
            page=page,
            playwright=FakePlaywright(),
            route_guarded=True,
            owns_context=True,
            owns_page=True,
        )

    monkeypatch.setenv("TRAWLER_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(live_browser.account_vault, "_fernet", None)
    monkeypatch.setattr(live_browser, "PATCHRIGHT_AVAILABLE", True)
    monkeypatch.setattr(live_browser.hitl_rung, "has_display", lambda: True)
    monkeypatch.setattr(live_browser.ssrf, "resolve_and_check_async", fake_resolve)
    monkeypatch.setattr(live_browser.browser_adapter, "open_local_persistent_browser", fake_open)

    first = await live_browser.open_browser_session(
        "https://example.com/private",
        account_id="work",
    )
    second = await live_browser.open_browser_session(
        "https://example.com/next",
        account_id="work",
    )
    third = await live_browser.open_browser_session(
        "https://example.com/private",
        account_id="default",
    )

    assert first.startswith("__TRAWLER_OK__:")
    assert second.startswith("__TRAWLER_OK__:")
    assert third.startswith("__TRAWLER_OK__:")
    assert '"account_id": "work"' in first
    assert '"reused": true' in second
    assert len(opened) == 2
    assert "accounts" in opened[0].profile_dir
    assert "work" in opened[0].profile_dir
    assert opened[1].profile_dir.endswith("example.com\\profile") or opened[
        1
    ].profile_dir.endswith("example.com/profile")
    assert account_profiles.get_profile("example.com", "work").account_id == "work"


@pytest.mark.asyncio
async def test_persist_account_state_updates_account_profile(tmp_db, monkeypatch):
    from cryptography.fernet import Fernet

    from trawler import account_profiles, account_vault, live_browser

    class CookieContext(FakeContext):
        async def storage_state(self, indexed_db=True):
            return {"cookies": [{"name": "sid", "value": "secret"}], "origins": []}

        async def cookies(self, urls):
            return [
                {
                    "name": "cf_clearance",
                    "value": "clearance",
                    "domain": "example.com",
                    "path": "/",
                }
            ]

    monkeypatch.setenv("TRAWLER_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(account_vault, "_fernet", None)
    session = live_browser.LiveBrowserSession(
        session_id="live-work",
        domain="example.com",
        account_id="work",
        start_url="https://example.com/",
        current_url="https://example.com/current",
        context=CookieContext(),
        page=FakePage(),
        playwright=FakePlaywright(),
        profile_dir="profile",
    )

    await live_browser._persist_account_state(session)

    profile = account_profiles.get_profile("example.com", "work")
    assert profile.status == "active"
    assert profile.last_verified_at
    state_path = account_vault.storage_state_path("example.com", account_id="work")
    assert state_path.exists()
    assert "secret" not in state_path.read_text(encoding="utf-8")
    assert account_vault.get_auto_cookies(
        "example.com",
        session_id="live-work",
        account_id="work",
    )["cf_clearance"] == "clearance"


@pytest.mark.asyncio
async def test_extract_browser_session_selector_and_screenshot(monkeypatch):
    from trawler import live_browser

    async def fake_resolve(url):
        return False, "93.184.216.34"

    page = FakePage()
    context = FakeContext()
    session = live_browser.LiveBrowserSession(
        session_id="live-test",
        domain="example.com",
        start_url="https://example.com/",
        current_url="https://example.com/current",
        context=context,
        page=page,
        playwright=FakePlaywright(),
        profile_dir="profile",
    )
    live_browser._LIVE_SESSIONS[session.session_id] = session
    monkeypatch.setattr(live_browser.account_vault, "is_vault_enabled", lambda: False)
    monkeypatch.setattr(live_browser.ssrf, "resolve_and_check_async", fake_resolve)

    selected = await live_browser.extract_browser_session(
        "live-test",
        extract_mode="selector",
        selector=".target",
    )
    screenshot = await live_browser.extract_browser_session(
        "live-test",
        extract_mode="screenshot",
    )
    element = await live_browser.extract_browser_session(
        "live-test",
        extract_mode="element_snapshot",
        selector=".target",
    )
    picked_element = await live_browser.extract_browser_session(
        "live-test",
        extract_mode="picked_element",
    )
    picked_region = await live_browser.extract_browser_session(
        "live-test",
        extract_mode="picked_region",
    )
    page_clone = await live_browser.extract_browser_session(
        "live-test",
        extract_mode="page_clone",
        selector="body",
    )
    accessibility = await live_browser.extract_browser_session(
        "live-test",
        extract_mode="accessibility_snapshot",
    )
    fitted = await live_browser.extract_browser_session(
        "live-test",
        extract_mode="fit_markdown",
        max_markdown_chars=1000,
    )
    visible_blocks = await live_browser.extract_browser_session(
        "live-test",
        extract_mode="visible_blocks",
    )
    bundle = await live_browser.extract_browser_session(
        "live-test",
        extract_mode="bundle",
        max_markdown_chars=200,
    )

    assert selected.structured["ok"] is True
    assert selected.structured["metadata"]["selector_report"]["selector_match_count"] == 1
    assert "Hello" in selected.legacy_text
    assert screenshot.structured["ok"] is True
    assert screenshot.screenshot == b"\x89PNG\r\n\x1a\nfake"
    assert element.structured["ok"] is True
    assert "outerHTML" in element.legacy_text
    assert element.screenshot == b"\x89PNG\r\n\x1a\nelement"
    assert picked_element.structured["ok"] is True
    assert "Picked" in picked_element.legacy_text
    assert picked_region.structured["ok"] is True
    assert "Region text" in picked_region.legacy_text
    assert picked_region.screenshot == b"\x89PNG\r\n\x1a\nfake"
    assert page_clone.structured["ok"] is True
    assert "nodeCount" in page_clone.legacy_text
    assert accessibility.structured["metadata"]["accessibility_snapshot"]["source"] == "cdp"
    assert ("Accessibility.getFullAXTree", {}) in context.cdp_calls
    assert "SECRET_PASSWORD" not in accessibility.legacy_text
    assert fitted.structured["metadata"]["fit_markdown"]["output_chars"] <= 1000
    assert "citations" in fitted.structured
    assert visible_blocks.structured["metadata"]["visible_blocks"]["blockCount"] == 2
    assert "Card one rendered text" in visible_blocks.legacy_text
    assert bundle.structured["bundle"]["link_count"] == 1
    assert bundle.structured["bundle"]["visible_block_count"] == 2
    assert "Card two rendered text" in bundle.legacy_text
    assert bundle.screenshot == b"\x89PNG\r\n\x1a\nfake"


@pytest.mark.asyncio
async def test_browser_actions_run_in_order_and_update_session(tmp_db, monkeypatch):
    from trawler import db, live_browser

    async def fake_resolve(url):
        return False, "93.184.216.34"

    page = FakePage()
    session = live_browser.LiveBrowserSession(
        session_id="live-actions",
        domain="example.com",
        start_url="https://example.com/",
        current_url="https://example.com/current",
        context=FakeContext(),
        page=page,
        playwright=FakePlaywright(),
        profile_dir="profile",
    )
    live_browser._LIVE_SESSIONS[session.session_id] = session
    monkeypatch.setattr(live_browser.account_vault, "is_vault_enabled", lambda: False)
    monkeypatch.setattr(live_browser.ssrf, "resolve_and_check_async", fake_resolve)

    result = await live_browser.perform_browser_actions(
        "live-actions",
        [
            {"type": "click", "selector": "button.login"},
            {"type": "fill", "selector": "input[name=email]", "text": "me@example.com"},
            {"type": "press", "selector": "input[name=email]", "key": "Enter"},
            {"type": "wait_for_selector", "selector": ".ready"},
            {"type": "scroll", "y": 400},
            {"type": "goto", "url": "https://example.com/dashboard"},
        ],
    )

    assert result.startswith("__TRAWLER_OK__:")
    assert page.calls == [
        ("click", "button.login"),
        ("fill", "input[name=email]", "me@example.com"),
        ("press", "input[name=email]", "Enter"),
        ("wait_for_selector", ".ready"),
        ("scroll", 0, 400),
        ("goto", "https://example.com/dashboard", "domcontentloaded", 30000),
    ]
    assert session.current_url == "https://example.com/dashboard"
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT status, rung_used FROM audit_log WHERE tool = 'browser_action' "
            "ORDER BY rowid ASC"
        ).fetchall()
    finally:
        conn.close()
    assert [row["status"] for row in rows] == ["ok"] * 6
    assert [row["rung_used"] for row in rows] == [
        "0:click",
        "1:fill",
        "2:press",
        "3:wait_for_selector",
        "4:scroll",
        "5:goto",
    ]
    assert "me@example.com" not in str([dict(row) for row in rows])


@pytest.mark.asyncio
async def test_observe_browser_session_returns_actionable_map(monkeypatch):
    import json

    from trawler import live_browser

    async def fake_resolve(url):
        return False, "93.184.216.34"

    context = FakeContext()
    session = live_browser.LiveBrowserSession(
        session_id="live-observe",
        domain="example.com",
        start_url="https://example.com/",
        current_url="https://example.com/current",
        context=context,
        page=FakePage(),
        playwright=FakePlaywright(),
        profile_dir="profile",
    )
    live_browser._LIVE_SESSIONS[session.session_id] = session
    monkeypatch.setattr(live_browser.ssrf, "resolve_and_check_async", fake_resolve)

    result = await live_browser.observe_browser_session(
        "live-observe",
        selector="body",
        max_elements=20,
    )

    assert result.startswith("__TRAWLER_OK__:")
    payload = json.loads(result[len("__TRAWLER_OK__:\n\n"):])
    observation = payload["observation"]
    assert observation["element_count"] == 3
    assert observation["elements"][0]["selector"] == "button.login"
    assert observation["elements"][1]["input_type"] == "password"
    assert "SECRET_PASSWORD" not in result
    assert "accessibility_snapshot" in observation
    assert ("Accessibility.getFullAXTree", {}) in context.cdp_calls


@pytest.mark.asyncio
async def test_browser_action_failure_reports_index(tmp_db, monkeypatch):
    from trawler import db, live_browser

    async def fake_resolve(url):
        return False, "93.184.216.34"

    session = live_browser.LiveBrowserSession(
        session_id="live-actions-fail",
        domain="example.com",
        start_url="https://example.com/",
        current_url="https://example.com/current",
        context=FakeContext(),
        page=FakePage(),
        playwright=FakePlaywright(),
        profile_dir="profile",
    )
    live_browser._LIVE_SESSIONS[session.session_id] = session
    monkeypatch.setattr(live_browser.account_vault, "is_vault_enabled", lambda: False)
    monkeypatch.setattr(live_browser.ssrf, "resolve_and_check_async", fake_resolve)

    result = await live_browser.perform_browser_actions(
        "live-actions-fail",
        [
            {"type": "click", "selector": ".ok"},
            {"type": "click", "selector": ".boom"},
        ],
    )

    assert result.startswith("__TRAWLER_ERROR__:")
    assert '"action_index": 1' in result
    assert '"selector": ".boom"' in result
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT status, rung_used FROM audit_log WHERE tool = 'browser_action' "
            "ORDER BY rowid ASC"
        ).fetchall()
    finally:
        conn.close()
    assert [row["status"] for row in rows] == ["ok", "failed"]
    assert [row["rung_used"] for row in rows] == ["0:click", "1:click"]


@pytest.mark.asyncio
async def test_picker_tools_inject_overlay(monkeypatch):
    from trawler import live_browser

    session = live_browser.LiveBrowserSession(
        session_id="live-picker",
        domain="example.com",
        start_url="https://example.com/",
        current_url="https://example.com/current",
        context=FakeContext(),
        page=FakePage(),
        playwright=FakePlaywright(),
        profile_dir="profile",
    )
    live_browser._LIVE_SESSIONS[session.session_id] = session

    element = await live_browser.start_element_picker("live-picker")
    region = await live_browser.start_region_picker("live-picker")

    assert element.startswith("__TRAWLER_OK__:")
    assert region.startswith("__TRAWLER_OK__:")


@pytest.mark.asyncio
async def test_close_browser_session_removes_session(monkeypatch):
    from trawler import live_browser

    session = live_browser.LiveBrowserSession(
        session_id="live-close",
        domain="example.com",
        start_url="https://example.com/",
        current_url="https://example.com/current",
        context=FakeContext(),
        page=FakePage(),
        playwright=FakePlaywright(),
        profile_dir="profile",
    )
    live_browser._LIVE_SESSIONS[session.session_id] = session
    monkeypatch.setattr(live_browser.account_vault, "is_vault_enabled", lambda: False)

    result = await live_browser.close_browser_session("live-close")

    assert result.startswith("__TRAWLER_OK__:")
    assert "live-close" not in live_browser._LIVE_SESSIONS
