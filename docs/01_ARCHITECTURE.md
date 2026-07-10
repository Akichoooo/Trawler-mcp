# Trawler-mcp 全景架构深度解析指南 (01_ARCHITECTURE.md)

> [!NOTE]
> 本文档旨在为核心开发者提供 **100% 毫无保留** 的架构深度解析。所有的机制说明均带有源码链接，作为确凿的技术证据。

## 一、 系统宏观定位与全景图

Trawler-mcp 的本质是一个 **AI Agent 专用的、极度抗封控的异步网络请求网关**。
通过标准的 Model Context Protocol (MCP)，外界 Agent 可以把 Trawler 当作一把“瑞士军刀”，将任意 URL 投喂给它，Trawler 则在黑盒内部完成所有与反爬对抗、页面渲染、内容净化相关的脏活累活。

### 1.1 FastMCP 与入口点 (Entry Points)
系统的主要入口位于 `trawler/server.py` 和 `trawler/__main__.py`。
- **FastMCP 实例化**: [`trawler/server.py:L14`](file:///d:/.//trawler/server.py#L14) 
  `mcp = FastMCP("Trawler", dependencies=["uvloop"])`
- **uvloop 注入**: 在 Linux/Mac 环境下，项目会通过 `pyproject.toml` 引入 `uvloop`，并在启动时自动接管标准 `asyncio` 事件循环，提升 2-4 倍的 I/O 吞吐能力。
- **MCP 协议传输层**: 在 [`trawler/server.py`](file:///d:/.//trawler/server.py) 中，系统支持双模传输：
  - 默认的标准输入/输出流 (`stdio`) 模式，适用于本地光速调用。
  - 通过环境变量 `MCP_TRANSPORT=sse` 开启基于 Starlette 的 Server-Sent Events (SSE) HTTP 服务，允许跨机器、跨容器解耦部署。

### 1.2 防幻觉设计契约 (Anti-Hallucination Contract)
为了防止 LLM 在看到复杂的错误堆栈时产生逻辑幻觉，Trawler 在 [`trawler/crawl_url.py`](file:///d:/.//trawler/crawl_url.py) (Line 3-6) 中定死了出参协议：
- 成功抓取必定返回字符串：`__TRAWLER_OK__:\n\n<内容>`
- 失败抓取必定返回 JSON：`__TRAWLER_ERROR__:{ "errorType": "...", "retryable": bool }` (见 [`trawler/errors.py`](file:///d:/.//trawler/errors.py))。这种严格的字符串包裹强制 LLM 明确区分系统错误和网页实际文本。

---

## 二、 核心管道：请求阶梯降级流 (Adaptive Fallback Pipeline)

所有抓取流量统一流入 [`trawler/crawl_url.py:L172`](file:///d:/.//trawler/crawl_url.py#L172) 的 `crawl_url()` 函数。这个函数是整个工程的心脏，内部通过 `asyncio.wait_for` 施加了 35 秒的全局墙钟超时，防止整个任务挂死。

具体执行流 `_do_crawl()` 实现了 4 层梯度的优雅降级。它们按顺序依次尝试，任何一层成功则直接短路返回，失败则向下跌落。

### 梯度 0: `curlcffi_rung` (极速/隐匿轻骑兵)
- **源码**: [`trawler/fetcher/curlcffi_rung.py`](file:///d:/.//trawler/fetcher/curlcffi_rung.py)
- **定位**: 默认第一顺位拦截器。
- **机制**: 采用 `curl_cffi`，在 Socket 层面伪造 Chrome 120 的 TLS 握手特征（JA4）和 HTTP/2 头顺序。自带 LRU 会话连接池（`_SESSION_POOL`），吞吐量极高。

### 梯度 1: `patchright_rung` (重装真机破盾者)
- **源码**: [`trawler/fetcher/patchright_rung.py`](file:///d:/.//trawler/fetcher/patchright_rung.py)
- **定位**: 专治 Cloudflare 5秒盾和 Turnstile 质询。
- **机制**: 基于抹除了 WebDriver 痕迹的魔改版 Chromium (`patchright`)。通过集成 `browserforge` 随机注入与屏幕、系统、Canvas 指纹高度一致的组合套装。此层支持**凭证回流**，突破 CF 后留下的 Cookie (`cf_clearance`) 可反哺系统。

### 梯度 2: `jina_rung` (外部算力 AI 爬虫)
- **源码**: [`trawler/fetcher/jina_rung.py`](file:///d:/.//trawler/fetcher/jina_rung.py)
- **定位**: SaaS 级兜底。当目标网页结构极度复杂或本地算力/IP 被彻底封杀时，借助 `r.jina.ai` 等外部服务代工。

### 梯度 3: `hitl_rung` (Human-in-the-Loop 真人模式)
- **源码**: [`trawler/fetcher/hitl_rung.py`](file:///d:/.//trawler/fetcher/hitl_rung.py)
- **触发**: 当参数带有 `bypass_l3=True` 且上述阶梯全盘崩溃时触发。
- **机制**: 强行弹出一个非 Headless 的可视浏览器，给用户 60 秒的黄金时间手动解决图形验证码（CAPTCHA）。内置了严苛的 WebRTC 阻断与 ServiceWorker 封锁以防真实内网特征泄露。

---

## 三、 高并发数据库架构：排队批量写入 (Micro-batching)

为了兼顾海量的审计日志 (`audit.py`) 留痕与 SQLite 本地单文件存储的限制，Trawler-mcp 重写了底层写锁模型，抛弃了传统的互斥锁，转向单向流水线模型。

### 3.1 痛点与重构
在早期，高并发下 SQLite 会因为线程来不及释放写锁抛出 `database is locked`。
为此，项目抽离出了 [`trawler/db_writer.py`](file:///d:/.//trawler/db_writer.py)。

### 3.2 AsyncQueueDBWriter 核心工作流
1. **彻底解耦**: 所有的写库操作必须通过 [`crawl_url.py`](file:///d:/.//trawler/crawl_url.py) 的 `_db_write()` 包装，本质上是调用 `db_writer.submit()`。它将写任务和参数打包成一个 tuple，塞进纯内存 `asyncio.Queue` 中，立即释放主事件循环协程。
2. **批量归集**: 在 `_db_worker()` 后台任务中，遇到首个写任务后，它会在极短时间内（通过 `get_nowait` 循环）尝试收集最多 100 个堆积的写入任务。
3. **BEGIN IMMEDIATE 独占写**:
   合并任务后，交由 `asyncio.to_thread(_execute_batch)` 同步线程执行。该线程通过 `conn.execute("BEGIN IMMEDIATE")` 获取排他写锁，一次性 commit 全局更改。
   此设计实现了：**高吞吐量（减少事务开销） + 0 锁争用**。

### 3.3 跨事件循环 (Event Loop) 污染免疫
在 `pytest-asyncio` 的执行流中，多个测试会频繁新建/销毁事件循环。若 `db_writer` 的模块级 `_queue` 存活到下一轮，会导致死锁。
详见 [`db_writer.py:L108`](file:///d:/.//trawler/db_writer.py#L108)：
系统在 `submit` 时会智能检测 `_worker_task.get_loop() != loop`。一旦发现 Loop 变更（或热重启），旧的队列与 Worker 立即注销并懒重启，保障测试与生产环境极致稳健。

---

## 四、 后处理防爆内存引擎 (OOM-Safe Pipeline)

从目标站抓到的往往是几兆的杂乱 HTML，甚至塞满了 Base64 图片，直接丢给大模型必然 OOM (Out Of Memory)。

- **源码**: [`trawler/parser/extract.py`](file:///d:/.//trawler/parser/extract.py)
- **极致净空**: 解析流程会在极早期介入 `strip_base64_images`，用正则表达式粗暴且极速地割除任何以 `data:image/` 开头的巨量文本。
- **层级清洗**: HTML 交由 [`trawler/parser/extract.py`](file:///d:/.//trawler/parser/extract.py) (内置 readability 算法) 抽离出最干净的正文 Markdown。

> [!TIP]
> **全景总结**：Trawler-mcp 的主逻辑是一个“漏斗”：海量的并发请求被送进异步网络池 (`_SESSION_POOL` + 阶梯 Rung) -> 解析阶段被强力榨干冗余数据 -> 结果和审计流被排队塞进 `AsyncQueueDBWriter` 批量存盘。整个全生命周期没有任何阻塞点。
