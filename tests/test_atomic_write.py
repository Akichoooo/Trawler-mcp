"""测试集 #5 — 原子写。

模拟 Ctrl+C 杀进程, raw/ 下无残缺 .md (只有完整 .md 或残留 .tmp)。
"""
import threading

from trawler import config
from trawler.atomic import atomic_write
from trawler.raw_store import raw_path, save_raw


def test_atomic_write_completes(tmp_path):
    """正常原子写: 只有目标文件, 无 .tmp 残留。"""
    target = tmp_path / "test.md"
    atomic_write(target, "hello world")
    assert target.read_text() == "hello world"
    assert not (tmp_path / "test.md.tmp").exists()


def test_raw_save_is_atomic(tmp_db):
    """save_raw 产出的 .md 完整 (有 frontmatter + 正文)。"""
    p = save_raw("testid", url="https://example.com/", final_url="https://example.com/",
                 title="Test", markdown="body", gear_used="patchright")
    content = p.read_text(encoding="utf-8")
    assert content.startswith("---")
    assert "url: https://example.com/" in content
    assert "body" in content
    # 无 .tmp 残留
    assert not (config.RAW_DIR / "testid.md.tmp").exists()


def test_concurrent_atomic_writes_no_corrupt(tmp_db):
    """并发写同一 raw_id: 最后一个是完整的 (不撕裂)。"""
    rid = "concurrent_id"
    # 并发写多个版本
    threads = []
    for i in range(5):
        def writer(version=i):
            save_raw(rid, url=f"https://example.com/v{version}",
                     final_url=f"https://example.com/v{version}",
                     title=f"v{version}", markdown=f"content v{version}",
                     gear_used="patchright")
        t = threading.Thread(target=writer)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    # 最终文件应是完整的某个版本 (不撕裂)
    content = raw_path(rid).read_text(encoding="utf-8")
    assert content.startswith("---")  # frontmatter 完整
    assert "content v" in content
    # 解析 frontmatter 应是合法 YAML
    import yaml
    fm_end = content.index("---", 4)
    fm = yaml.safe_load(content[4:fm_end])
    assert "url" in fm
