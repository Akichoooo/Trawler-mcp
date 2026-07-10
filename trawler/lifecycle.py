"""启动清理 — 僵尸 job / profile LRU / 残缺 .tmp。

MCP server 启动时调一次, 保持存储干净。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3

from trawler import config, db, jobs

log = logging.getLogger("trawler.lifecycle")


def startup_cleanup(conn: sqlite3.Connection) -> dict:
    """启动清理。返回各步清理计数。"""
    result = {
        "zombie_jobs": jobs.fail_running_jobs(conn),
        "profiles_removed": _clean_profiles(),
        "tmp_files_removed": _clean_tmp_files(),
    }
    log.info("startup cleanup: %s", result)
    return result


def _clean_profiles() -> int:
    """account_vault LRU: 只留最近活跃 Top N 域的 profile 目录。

    依据: domain_rules.last_success_at。其他域只删 profile/, 保留 storage_state.json (KB级)。
    """
    if not config.VAULT_DIR.exists():
        return 0

    # 拿所有有 profile 的域, 按 last_success_at 排序
    conn = db.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        active = conn.execute(
            "SELECT domain FROM domain_rules WHERE last_success_at IS NOT NULL "
            "ORDER BY last_success_at DESC LIMIT ?",
            (config.PROFILE_TOPN,),
        ).fetchall()
    finally:
        conn.close()
    keep = {r["domain"] for r in active}

    removed = 0
    for entry in config.VAULT_DIR.iterdir():
        if not entry.is_dir():
            continue
        domain = entry.name
        if domain in keep:
            continue
        profile_dirs = [entry / "profile"]
        accounts_dir = entry / "accounts"
        if accounts_dir.exists():
            profile_dirs.extend(accounts_dir.glob("*/profile"))
        for profile_dir in profile_dirs:
            if profile_dir.exists():
                shutil.rmtree(profile_dir, ignore_errors=True)
                removed += 1
    return removed


def _clean_tmp_files() -> int:
    """删 raw/ 下残缺的 .tmp 文件 (上次崩溃留下的)。"""
    removed = 0
    for tmp in config.RAW_DIR.glob("*.tmp"):
        try:
            tmp.unlink()
            removed += 1
        except OSError:
            pass
    return removed
