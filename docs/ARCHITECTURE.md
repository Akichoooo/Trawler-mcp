# Trawler-mcp 2026 全局架构设计白皮书

> [!IMPORTANT]
> 本文档基于 2026 年最新重构版本，旨在为开发者提供全景式的架构剖析。
> 所有分析均附带源码路径，可用作学习或二次开发的权威参考。

## 1. 顶层设计：FastMCP 骨架与 SSE 流式层

Trawler-mcp 的核心是一个标准的 [Model Context Protocol (MCP)](https://github.com/modelcontextprotocol) Server，旨在让 AI Agent 通过 MCP 协议调用爬虫能力。

### 1.1 FastMCP 实例化
项目在 [`trawler/server.py:L14`](file:///d:/.//trawler/server.py#L14) 实例化了 `FastMCP`：
```python
mcp = FastMCP("Trawler", dependencies=["uvloop"])
```
- **uvloop 注入**: 在 Linux/Mac 环境下，项目会通过 `pyproject.toml` 引入 `uvloop`，并在启动时自动接管标准 `asyncio` 事件循环，提升 2-4 倍的 I/O 吞吐能力（详见 `trawler/__main__.py` 平台检测）。
- **工具暴露**: 通过 `@mcp.tool()` 将 `crawl_url` 方法暴露给外界。

### 1.2 SSE Streamable 传输层
在开发阶段及特定的客户端对接中，Trawler 支持基于 SSE (Server-Sent Events) 的双向流式通信。
见 [`trawler/server.py:L26-L34`](file:///d:/.//trawler/server.py#L26)：
如果环境变量 `MCP_TRANSPORT=sse`，服务器会启动 Starlette 驱动的 HTTP 服务器（暴露 `/sse` 和 `/messages` 接口），取代默认的 `stdio` 管道，允许跨机器调用。

---

## 2. 请求阶梯降级流水线 (Adaptive Fallback)

网络爬虫最怕的不是被封，而是“不知为何被封”。Trawler 实现了极具弹性的四阶梯降级策略，主干逻辑位于 [`trawler/crawl_url.py`](file:///d:/.//trawler/crawl_url.py)。

### 2.1 流水线设计 (`_do_crawl`)
当请求进入 [`crawl_url.py:L263`](file:///d:/.//trawler/crawl_url.py#L263) `_do_crawl` 后，会严格按照以下阶梯调度（Rung 0 -> Rung 3）：

1. **Rung 0 (`curlcffi_rung`)**: 
   - *定位*：极速 HTTP/2 轻量爬虫，自带 JA4 级 TLS 伪装。
   - *速度*：毫秒级，依托全局 Session 连接池。
2. **Rung 1 (`patchright_rung`)**: 
   - *定位*：重型真机浏览器。基于魔改 Chromium，主要用于突破 Cloudflare JS 质询。
   - *速度*：2-5 秒，结合了 Browserforge 拟人指纹。
3. **Rung 2 (`jina_rung`)**: 
   - *定位*：外部商业级 AI 爬虫 (jina.ai)，专治各种疑难杂症与深度渲染页。
   - *条件*：仅在前两者被强风控（L1/L2 Block）且未禁用时介入。
4. **Rung 3 (`hitl_rung`)**: 
   - *定位*：Human-in-the-Loop (有头模式)，弹出带界面的浏览器供真人处理 CAPTCHA。
   - *触发*：极度严苛的 `bypass_l3=True`，自带 WebRTC 防泄漏和 ServiceWorker 阻断机制。

### 2.2 防 LLM 幻觉协议
在 `crawl_url.py` 的返回协议中，设计了严格的防多态契约：
- **成功**：`__TRAWLER_OK__:\n\n<markdown_content>`
- **失败**：`__TRAWLER_ERROR__:{json_metadata}`
详见 [`trawler/errors.py`](file:///d:/.//trawler/errors.py)。这种严格的字符串包裹强制 LLM 明确区分系统错误和网页实际文本。

---

## 3. 高并发数据持久化：异步队列与单线程事务

随着压测深入（如 100 并发），传统的 SQLite `WAL` 模式（即便带重试机制）依然会在激烈的写锁竞争中抛出 `database is locked`（`SQLITE_BUSY`）。
为此，Trawler-mcp 在 2026 年重构中引入了终极解法：**后台守护线程批量排队写入**。

### 3.1 核心组件 `AsyncQueueDBWriter`
源码位置：[`trawler/db_writer.py`](file:///d:/.//trawler/db_writer.py)

1. **完全解耦主循环**: `submit()` 方法接受写入函数，立即将其放入内存 `asyncio.Queue`，主协程继续执行爬虫。
2. **微批处理 (Micro-batching)**:
   守护协程 `_db_worker()` 阻塞等待队列（[`db_writer.py:L79`](file:///d:/.//trawler/db_writer.py#L79)），一旦拿到首个任务，会在短暂瞬间（如 `get_nowait` 循环）尝试收集最多 100 个堆积的写入任务。
3. **BEGIN IMMEDIATE 独占写**:
   合并任务后，交由 `asyncio.to_thread(_execute_batch)` 同步线程执行。该线程通过 `conn.execute("BEGIN IMMEDIATE")` 获取排他写锁，一次性 commit 全局更改。
   此设计实现了：**高吞吐量（减少事务开销） + 0 锁争用**。

### 3.2 跨事件循环 (Event Loop) 污染免疫
在 `pytest-asyncio` 的执行流中，多个测试会频繁新建/销毁事件循环。若 `db_writer` 的模块级 `_queue` 存活到下一轮，会导致死锁。
详见 [`db_writer.py:L108-L113`](file:///d:/.//trawler/db_writer.py#L108-L113)：
系统在 `submit` 时会智能检测 `_worker_task.get_loop() != loop`。一旦发现 Loop 变更（或热重启），旧的队列与 Worker 立即注销并懒重启，保障测试与生产环境极致稳健。

---

## 4. 全局 Audit 审计管道

为了监控爬虫的阶梯降级和拦截状况，每一次操作都会录入 Audit 日志。
所有的写操作现在都必须经过 `_db_write` 包装层（[`crawl_url.py:L246-L260`](file:///d:/.//trawler/crawl_url.py#L246-L260)），即使是异常捕捉路径（如 `TimeoutError`）也不例外，从源头断绝了同步 I/O 阻塞事件循环的可能。

> [!TIP]
> 架构设计准则：主线程 (Event Loop) 中永远禁止存在盘旋式（Spinning）重试或同步 SQLite 写入，所有的数据库 Write 操作必须经过 `db_writer`。
