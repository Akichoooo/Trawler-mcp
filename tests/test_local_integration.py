from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = ""
        status = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if self.path == "/robots.txt":
            headers["Content-Type"] = "text/plain; charset=utf-8"
            body = f"Sitemap: {self.server.base_url}/sitemap.xml\n"  # type: ignore[attr-defined]
        elif self.path == "/sitemap.xml":
            headers["Content-Type"] = "application/xml; charset=utf-8"
            body = (
                "<urlset>"
                f"<url><loc>{self.server.base_url}/docs/a?version=1</loc></url>"  # type: ignore[attr-defined]
                f"<url><loc>{self.server.base_url}/blog/b</loc></url>"  # type: ignore[attr-defined]
                "<url><loc>https://other.test/out</loc></url>"
                "</urlset>"
            )
        elif self.path.startswith("/docs/a"):
            body = (
                "<html><body><article><h1>Docs A</h1>"
                "<p>"
                + ("This is a stable local integration page. " * 20)
                + "</p><a href='/docs/b?x=1'>Docs B</a><a href='/blog/c'>Blog C</a>"
                "</article></body></html>"
            )
        elif self.path == "/redirect-localhost":
            status = 302
            headers["Location"] = self.server.base_url.replace("127.0.0.1", "localhost") + "/docs/a"  # type: ignore[attr-defined]
        else:
            status = 404
            body = "not found"

        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):  # noqa: A002
        return


@pytest.fixture
def local_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    host, port = server.server_address
    server.base_url = f"http://{host}:{port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.base_url  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_discover_site_index_against_local_http_site(local_site, monkeypatch):
    from trawler import config, site_index

    monkeypatch.setattr(config, "ALLOW_LOCAL", True)

    result = await site_index.discover_site_index(
        local_site + "/",
        max_urls=10,
        include_paths=["/docs/*"],
        ignore_query_parameters=True,
    )

    assert result["ok"] is True
    assert result["urls"] == [local_site + "/docs/a"]
    assert local_site + "/sitemap.xml" in result["sitemap_urls"]


@pytest.mark.asyncio
async def test_crawl_url_blocks_local_redirect_outside_allowed_domain(tmp_db, local_site, monkeypatch):
    import json

    from trawler import config
    from trawler import crawl_url as crawl_url_mod

    monkeypatch.setattr(config, "ALLOW_LOCAL", True)
    monkeypatch.setattr(config, "RESPECT_ROBOTS", False)

    result = await crawl_url_mod.crawl_url(
        local_site + "/redirect-localhost",
        allowed_domain="127.0.0.1",
        bypass_l3=True,
        cache_mode="write_only",
    )

    assert result.startswith("__TRAWLER_ERROR__:")
    payload = json.loads(result[len("__TRAWLER_ERROR__:"):])
    assert payload["errorType"] == "blocked-scope"
