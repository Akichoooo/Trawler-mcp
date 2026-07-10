"""测试集 #3 — SQLite 并发锁。

4 进程同时密集插入 seen_urls, busy_timeout 生效, 不抛 Locked。
"""
import sqlite3


def _worker(db_path: str, worker_id: int, count: int) -> int:
    """子进程: 密集插入 seen_urls。返回成功条数。"""
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=15)
    conn.execute("PRAGMA busy_timeout=5000")
    ok = 0
    for i in range(count):
        try:
            conn.execute(
                "INSERT OR REPLACE INTO seen_urls (sha1_full, url, raw_id, crawled_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"w{worker_id}_{i:04d}", f"http://test/{worker_id}/{i}", f"raw_{i}", "2026-01-01T00:00:00+00:00", "h"),
            )
            ok += 1
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                pass  # busy_timeout 应让它等, 但极端情况下仍可能超
            else:
                raise
    conn.close()
    return ok


def test_concurrent_writes_no_locked(tmp_path):
    """4 进程并发写, 无 database is locked 异常。"""
    from trawler import db
    db_path = tmp_path / "conc.db"
    # 不改全局 config.DB_PATH (避免测试间污染); 子进程直接用传参 db_path
    db.init_db(str(db_path))

    PROCS = 4
    PER_PROC = 30
    
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=PROCS) as executor:
        futures = [executor.submit(_worker, str(db_path), wid, PER_PROC) for wid in range(PROCS)]
        for f in concurrent.futures.as_completed(futures):
            f.result() # Will raise exception if worker failed

    # 验证全部写入
    conn = sqlite3.connect(str(db_path))
    total = conn.execute("SELECT COUNT(*) FROM seen_urls").fetchone()[0]
    conn.close()
    assert total == PROCS * PER_PROC, f"expected {PROCS*PER_PROC}, got {total}"
