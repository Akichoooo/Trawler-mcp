"""原子写工具 — .tmp → os.replace。

防脏读/写一半崩溃。POSIX 保证 rename 原子; Windows 上 os.replace 也是原子,
但 Windows 对"目标已存在且被占用"会 PermissionError, 需重试。
raw/<id>.md 和 storage_state.json 都用这个。
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path


def atomic_write(path: str | Path, data: str | bytes, *, encoding: str = "utf-8") -> None:
    """原子写: 先写 .tmp (uuid 后缀防并发撞), flush+sync 后 os.replace 覆盖目标。

    text: 传 str + encoding
    binary: 传 bytes (encoding 被忽略)

    Windows 兼容: os.replace 撞锁时短暂重试 (最多 1s)。
    uuid 后缀: 多个并发写同一 target 时 .tmp 互不覆盖。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # uuid 后缀防并发同 target 的 .tmp 互相覆盖
    tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}.tmp")

    mode = "wb" if isinstance(data, bytes) else "w"
    with open(tmp, mode, encoding=None if isinstance(data, bytes) else encoding) as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass  # 网络文件系统不支持 fsync

    _replace_with_retry(tmp, target)


def _replace_with_retry(tmp: Path, target: Path, *, max_retries: int = 20, delay: float = 0.05) -> None:
    """os.replace 带 Windows 重试。处理目标占用 + tmp 丢失。

    uuid 后缀已保证 tmp 对当前写唯一, 不会被另一 atomic_write 删。
    故 FileNotFound 几乎必然是外部清理脚本删了 tmp = 数据可能丢, 不能静默,
    必须抛出让上层感知 (避免写盘假成功)。
    """
    last_err = None
    for _ in range(max_retries):
        if not tmp.exists():
            # tmp 不见了 (uuid 唯一, 不该被并发删) = 外部删了, 数据丢
            raise RuntimeError(f"Atomic write failed: tmp {tmp} disappeared (data possibly lost)")
        try:
            os.replace(tmp, target)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(delay)
    if last_err and tmp.exists():
        try:
            os.replace(tmp, target)
        except PermissionError:
            raise last_err

