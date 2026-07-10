"""信号处理 — SIGINT/SIGTERM 优雅销毁浏览器, 防僵尸进程。

关键: 信号处理在主线程同步上下文执行, 不能用 run_until_complete (loop 可能正在跑),
也不能可靠 create_task (需要 running loop)。采用三层兜底:
1. call_soon_threadsafe 调度 async close (如果 loop 在跑)
2. 同步强制 kill 浏览器进程 (playwright 进程级)
3. atexit 注册最终兜底
"""

from __future__ import annotations

import atexit
import logging
import signal
import sys

log = logging.getLogger("trawler.signals")

# 全局: 当前活跃的浏览器实例 (由 fetcher 注册/注销)
_active_browsers: list = []
# 已注册 atexit 标志 (避免重复)
_atexit_installed = False


def register_browser(handle) -> None:
    """fetcher 启动浏览器时注册, 便于信号处理时关。"""
    if handle not in _active_browsers:
        _active_browsers.append(handle)


def unregister_browser(handle) -> None:
    try:
        _active_browsers.remove(handle)
    except ValueError:
        pass


def _force_close_all() -> None:
    """同步强制关闭所有浏览器。best-effort, 不抛。"""
    for handle in list(_active_browsers):
        try:
            # patchright/playwright 的 Browser 有 _connection._transport._proc 指向进程
            # 尝试拿到底层进程并 kill
            proc = _extract_process(handle)
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
        except Exception:
            pass
    _active_browsers.clear()


def _extract_process(handle):
    """从 patchright/playwright Browser 或 BrowserContext 对象拿底层 subprocess.Popen。

    支持两类 handle:
    - Browser (patchright_rung 注册): _connection → _transport → _proc
    - BrowserContext (hitl_rung 注册 persistent context): .browser 返回 None,
      需走 _impl_obj → _channel → connection → _transport → _proc
    """
    # Browser 路径
    for attr_chain in (("_connection", "_transport", "_proc"),
                       ("_impl_obj", "_channel", "connection", "transport", "_proc")):
        obj = handle
        try:
            for attr in attr_chain:
                obj = getattr(obj, attr, None)
                if obj is None:
                    break
            if obj is not None and hasattr(obj, "kill"):
                return obj
        except Exception:
            continue
    # BrowserContext 路径: persistent context 无独立 Browser, 走 channel connection
    for ctx_chain in (("_impl_obj", "_channel", "connection", "_transport", "_proc"),
                      ("_impl_obj", "_channel", "_connection", "_transport", "_proc"),
                      ("_connection", "_transport", "_proc")):
        obj = handle
        try:
            for attr in ctx_chain:
                obj = getattr(obj, attr, None)
                if obj is None:
                    break
            if obj is not None and hasattr(obj, "kill"):
                return obj
        except Exception:
            continue
    return None


def _shutdown(*args) -> None:
    """信号处理入口。同步, best-effort 关浏览器后退出。"""
    log.info("signal received, shutting down %d browser(s)", len(_active_browsers))

    # 关键修正: 不用 create_task — 它只入队, 紧接着 sys.exit(0) 瞬间杀进程,
    # Event Loop 根本没机会 tick 调度 close (create_task 是假动作)。
    # 唯一可靠路径: 同步强制 kill 浏览器进程。
    _force_close_all()

    sys.exit(0)


def install_handlers() -> None:
    """注册 SIGTERM + atexit 兜底。

    不覆盖 SIGINT: FastMCP/anyio 在 mcp.run() 时检查 signal.getsignal(SIGINT)
    is signal.default_int_handler, 若已被我们替换 → anyio 不装 graceful handler →
    Ctrl+C 时 stdio 缓冲未 flush, client 收截断 JSON-RPC。
    浏览器清理靠 atexit._force_close_all (anyio graceful shutdown 后 atexit 仍会跑)。
    SIGTERM (容器 kill) 仍需强 kill 浏览器 (atexit 在 SIGTERM 默认不触发)。
    """
    global _atexit_installed

    # SIGTERM: 容器 docker stop 发 SIGTERM, atexit 默认不触发, 需显式注册
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (AttributeError, OSError, ValueError):
        pass  # Windows 无 SIGTERM

    if not _atexit_installed:
        atexit.register(_force_close_all)
        _atexit_installed = True
