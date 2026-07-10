"""db_writer — 异步批量队列写入器 (Async Batch-Write Queue)

通过后台单线程独占一条连接执行所有被代理的 SQLite 写入操作，
从而根除并发下的 `database is locked` 错误，并释放主事件循环的 I/O。
"""

import asyncio
import inspect
import logging
from typing import Any

from trawler import db

log = logging.getLogger("trawler.db_writer")

_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None

def start_worker() -> None:
    global _worker_task
    if _worker_task is None:
        # 获取当前运行的事件循环
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return # 没有 running loop 时不启动
        global _queue
        if _queue is None:
            _queue = asyncio.Queue()
        _worker_task = loop.create_task(_db_worker())

async def stop_worker() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None

def _execute_batch(batch: list) -> None:
    """单线程独占写入"""
    if not batch:
        return
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for fn, args, kwargs, fut in batch:
            if fut.cancelled():
                continue
            try:
                # 统一首参数注入 conn，因为 crawl_url 的 _db_write 把 conn 剥离了
                res = fn(conn, *args, **kwargs)
                if inspect.iscoroutine(res):
                    raise RuntimeError(
                        f"{fn} returned a coroutine, which is not supported in batch sync writer"
                    )
                if not fut.done():
                    fut.get_loop().call_soon_threadsafe(fut.set_result, res)
            except Exception as e:
                log.error("DB batch item failed: %s -> %s", fn, e)
                if not fut.done():
                    fut.get_loop().call_soon_threadsafe(fut.set_exception, e)
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        log.error("DB batch commit failed: %s", e)
        for _, _, _, fut in batch:
            if not fut.done():
                fut.get_loop().call_soon_threadsafe(fut.set_exception, e)
    finally:
        conn.close()

async def _db_worker() -> None:
    log.info("AsyncQueueDBWriter started")
    global _queue
    while True:
        try:
            # 1. 阻塞等待第一个任务
            first_item = await _queue.get()
            batch = [first_item]
            
            # 2. 短暂等待收集更多任务，最多合并 100 条
            while len(batch) < 100:
                try:
                    item = _queue.get_nowait()
                    batch.append(item)
                except asyncio.QueueEmpty:
                    break
            
            # 卸载到线程池执行同步 I/O
            await asyncio.to_thread(_execute_batch, batch)
            
            for _ in batch:
                _queue.task_done()
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("AsyncQueueDBWriter error: %s", e)
            await asyncio.sleep(1)

async def submit(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """提交写入任务并等待结果"""
    loop = asyncio.get_running_loop()
    global _worker_task, _queue
    
    # 检测 worker 是否存在、是否已结束、或者是否属于不同的事件循环 (针对 pytest-asyncio 环境)
    if _worker_task is None or _worker_task.done() or _worker_task.get_loop() != loop:
        if _worker_task and not _worker_task.done():
            _worker_task.cancel()
        _worker_task = None
        _queue = None
        start_worker()

    
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    if _queue is None:
        _queue = asyncio.Queue()
    _queue.put_nowait((fn, args, kwargs, fut))
    return await fut
