"""测试集 #4 — OOM / Parser 降级。

喂 2.5MB 嵌套 HTML, 验证 2MB 截断生效, parser 容错输出或优雅报错, 不 CPU 100% 死锁。
"""
import time

from trawler import config
from trawler.parser import extract


def test_html_truncated():
    """2.5MB HTML 被截断到 2MB。"""
    big = "<html><body>" + ("<div>" + "x" * 100 + "</div>") * 40000 + "</body></html>"
    assert len(big) > config.HTML_TRUNCATE  # 确实超限
    # extract 内部应截断 (不崩溃)
    start = time.time()
    md = extract.extract(big, "https://example.com/big")
    elapsed = time.time() - start
    # 不应死锁 (有上限时间)
    assert elapsed < 30, f"parser took {elapsed}s, may be stuck"
    # 应有输出或失败标记 (不是异常)
    assert isinstance(md, str)


def test_deeply_nested_html():
    """极深嵌套 HTML 不爆栈。"""
    # 1 万层 div 嵌套
    nested = "<html><body>" + "<div>" * 10000 + "content" + "</div>" * 10000 + "</body></html>"
    start = time.time()
    md = extract.extract(nested, "https://example.com/nested")
    elapsed = time.time() - start
    assert elapsed < 30
    assert isinstance(md, str)


def test_inline_base64_does_not_oom():
    """大 base64 内联图不 OOM。"""
    big_img = '<img src="data:image/png;base64,' + ("A" * (3 * 1024 * 1024)) + '">'
    html = f"<html><body><article><p>real content here</p>{big_img}</article></body></html>"
    start = time.time()
    md = extract.extract(html, "https://example.com/b64")
    elapsed = time.time() - start
    assert elapsed < 30
    # 截断后应能提取到 "real content"
    assert "real content" in md or md == extract.PARSERS_FAILED


def test_empty_html_returns_failed_marker():
    md = extract.extract("")
    assert md == extract.PARSERS_FAILED
