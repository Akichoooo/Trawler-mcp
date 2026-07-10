import os

# Clean up NO_PROXY to prevent httpx from crashing on Windows due to IPv6 loopback (::1) parsing bug
if "NO_PROXY" in os.environ:
    no_proxy = os.environ["NO_PROXY"]
    cleaned_entries = [entry.strip() for entry in no_proxy.split(",") if ":" not in entry]
    os.environ["NO_PROXY"] = ",".join(cleaned_entries)

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """每测试独立 SQLite db (tmp 路径), 避免污染。"""
    from trawler import config, db
    db_path = tmp_path / "test.db"
    # monkeypatch 路径指向 tmp
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vault")
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    config.VAULT_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db(str(db_path))
    return db_path
