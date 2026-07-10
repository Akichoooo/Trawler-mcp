# Trawler-mcp 核心实现细节指南 (02_CORE_IMPLEMENTATION.md)

> [!IMPORTANT]
> 本文档专门剖析 Trawler-mcp 中最硬核的“反检测 (Anti-Detect)”、“安全防线 (Security Sandbox)” 以及“上下文回流 (Context Backflow)” 的代码级实现细节。所有的讲解均带有源码锚点，请结合代码阅读。

## 1. 底层网络隐匿：TLS/JA4 与 HTTP/2 伪造

现代 WAF (如 Cloudflare, Akamai) 首道防线是识别非浏览器的 TLS Client Hello 握手特征。

### 1.1 `curl_cffi` 的 impersonate 注入
源码位置：[`trawler/fetcher/curlcffi_rung.py`](file:///d:/.//trawler/fetcher/curlcffi_rung.py)

在 `Rung 0` 中，为了兼顾高吞吐与极速响应，放弃了传统的 `aiohttp`，转而采用支持原生 impersonate 的 `curl_cffi`。
```python
# curlcffi_rung.py:68
session = AsyncSession(
    impersonate=impersonate,  # e.g., "chrome120"
    verify=True,
    timeout=timeout_val,
    proxies=proxies
)
```
- **LRU 连接池化**: 每次创建底层 Socket 和协商 TLS 会消耗大量 CPU 时间（约 200ms）。通过 `@alru_cache`，系统按照 `(proxy_url, impersonate)` 缓存 Session 对象，实现了 HTTP/2 连接的 Keep-Alive 存活，后续请求直接利用现成管道，性能提升数倍。
- **Accept-Encoding 原生解压**: [`curlcffi_rung.py:L126`](file:///d:/.//trawler/fetcher/curlcffi_rung.py#L126) 注入了 `Accept-Encoding: gzip, deflate, br, zstd`，降低了传输带宽，并使得请求头部顺序严格符合 Chrome 浏览器的排列。

## 2. 动态拟人突防引擎：二次贝塞尔曲线

对于 `Rung 1 (patchright)`，一旦遭遇 Cloudflare JS 质询或 Turnstile 验证码，机械式的 `mouse.click(x, y)` 会被瞬间拦截。

### 2.1 基于数学的拟人轨迹算法
源码位置：[`trawler/fetcher/patchright_rung.py:L268`](file:///d:/.//trawler/fetcher/patchright_rung.py#L268) (`_inject_bezier_movement`)

系统不是走直线，而是通过二次贝塞尔曲线 (Quadratic Bezier Curve) 模拟人类手腕拖拽的弧度：
1. **控制点随机偏移**: 在起点和终点之间随机生成一个控制点 (Control Point)，并加入 `random.uniform(-50, 50)` 的震颤。
2. **变速缓动 (Easing)**: 利用 `math.sin(t * math.pi / 2)` 实现先快后慢的非线性时间差。
3. **分段打点 (Steps)**: 将轨迹拆解为 10-20 个散点，每个散点 `await page.mouse.move(x, y)`，中间随机 sleep `0.01s - 0.03s`。
这种微观的数学级扰动，在 WAF 收集到的鼠标事件阵列（`mousemove` array）中，统计学特征完全符合人类生理结构。

## 3. 防 SSRF / DNS Rebinding 沙箱防线

在提供对外暴露的 MCP 爬虫接口时，最致命的漏洞是 Server-Side Request Forgery (SSRF) 以及 DNS 重绑定。

### 3.1 Python 层的深层 DNS 拦截
源码位置：[`trawler/ssrf.py:L57`](file:///d:/.//trawler/ssrf.py#L57)

- `curl_cffi` 阶段，通过接管底层的 `asyncio.getaddrinfo` 或手动劫持主机解析，在发起实际的 TCP SYN 之前，严格阻断了 `10.0.0.0/8`, `127.0.0.0/8`, `169.254.169.254`（云厂商 Metadata 服务器）等私有 IP 空间。
- **防重定向**: 对于 301/302 重定向（[`curlcffi_rung.py:L218`](file:///d:/.//trawler/fetcher/curlcffi_rung.py#L218)），由于 `curl_cffi` 自身的 `allow_redirects=True` 可能会绕开 DNS 预检，重构后彻底关闭了底层库的自动跳转，改为 **Python 手动接管循环重定向**，并在每一次 `Location` 解析时重新调用 `ssrf.check_url_safe()`。

### 3.2 浏览器沙箱层的网络阻断
源码位置：[`trawler/fetcher/patchright_rung.py:L142`](file:///d:/.//trawler/fetcher/patchright_rung.py#L142) 和 [`hitl_rung.py`](file:///d:/.//trawler/fetcher/hitl_rung.py)

由于 headless 浏览器运行在操作系统的隔离进程中，它不受 Python 层 DNS 的管控。因此施加了以下铁腕策略：
1. **阻断 WebRTC 泄露**: 通过 `page.route` 和 JS 注入拦截 WebRTC 请求，防止内网 IP 被探测或反向打通。
2. **禁用 Service Worker**: 启动参数注入 `service_workers="block"` 防止目标网站在本地种下持久化的恶意 worker 节点作为局域网扫描跳板。

## 4. 上下文池化与 Cookie 凭证接力流转

`BrowserContext` 创建极其耗时（启动一个渲染引擎进程树，约 500-800ms）。

### 4.1 LRU Context 缓存架构
在 [`patchright_rung.py:L380`](file:///d:/.//trawler/fetcher/patchright_rung.py#L380) (位于 `_get_context` 内部逻辑)，系统根据三元组 `(id(browser), proxy_server, storage_state_path)` 将构建好的 Context 缓存起来。
为了防止缓存带来的状态污染：
- 在分配给任务前：`await context.clear_cookies()` 彻底洗白历史 Cookie。
- 只有带有明确 `account_vault` 的请求，才会预加载指定的 `storage_state` 凭证。

### 4.2 凭证回流 (Credential Backflow)
当 Rung 1 (`patchright`) 历经千辛万苦通过了 Turnstile 拿到高价值的 `cf_clearance` 凭证后，如何反哺给 Rung 0 (`curl_cffi`)？
- 在页面加载完毕后，爬虫会抽取所有 Cookie。
- 虽然当前架构中 `Rung` 是单向降级的，但在后续设计和同域名的并发限制下，抽取的 `cf_clearance` 可通过 `account_vault` 被其他高并发 `Rung 0` 请求利用，实现“一处突防，全盘受益”。

## 5. Site Rules 动态路由策略

并非所有网站都走统一策略。[`trawler/site_rules.py`](file:///d:/.//trawler/site_rules.py) 提供了强大的领域特定降维打击能力。
- 系统会在启动时从 `data/site_rules/*.yaml` 动态加载规则。
- 匹配到目标域名（如 `twitter.com`）时，可以硬性指定 `gear_hint` 强制直通 Rung 1 (patchright)，跳过毫无意义的 Rung 0 挣扎。
- 也可定义 `wait_strategy: "networkidle"` 让复杂 SPA 应用充分渲染后再抽取 DOM。

## 6. 内容安全与防御层 (Content Safety & Defense Module)

为了对抓取内容进行安全合规审计并防止下游 Agent 被恶意网页内容劫持（间接提示词注入攻击），Trawler 引入了开箱即用的内容安全防护层。

### 6.1 拦截与防注入逻辑
源码位置：[`trawler/parser/safety.py`](file:///d:/.//trawler/parser/safety.py)

1. **聊天控制 Token 消除**：恶意网页可能会植入类似 `<|im_start|>` 等特殊标识符欺骗 LLM 提前终止当前对话或模拟系统发言。系统通过 `CONTROL_TOKENS_RE` 对这些控制字符进行转义破坏。
2. **仿冒系统标签转义**：检测 `FAKE_SYSTEM_TAGS_RE`（如 `<system>`, `<instruction>`），将其转换为普通的展示实体。
3. **零宽空格 (ZWS) 指令干扰**：在敏感指令前缀（如 `ignore instructions`）中间植入 `\u200b`，切碎 LLM Tokenizer，破坏大模型的指令语义而保持人眼可读。

### 6.2 敏感信息 (PII) 智能脱敏
系统使用高并发优化的正则表达式识别并脱敏以下敏感数据：
- **中国大陆手机号**：匹配 `PHONE_RE` 并替换中间四位为 `****`。
- **中国大陆身份证**：匹配 `ID_CARD_RE` 拦截 18 位号码，保留前六位区域及尾部两位校验码，中间十位（包含生日）以 `**********` 遮蔽。
- **电子邮箱**：通过 `EMAIL_RE` 将邮箱账户名称的中间字符替换为 `*`，保留第一位、最后一位及域名。
- **银行卡/信用卡**：匹配 16-19 位卡号并模糊中间部分。

### 6.3 词库热加载 (Hot-Reloading Sensitive Words)
- **词库文件**：[`data/sensitive_words.txt`](file:///d:/.//data/sensitive_words.txt)
- **编译级正则匹配**：读取 `sensitive_words.txt` 后，程序将其按词长降序（防止短词覆盖）编译为单一正则捕获组。在下一次 Markdown 清洗时，通过正则的替换，自动屏蔽包含 `TOXIC`, `COMPLIANCE`, `JAILBREAK`, `CUSTOM` 等类别的词组。
- **检测 `mtime` 触发**：系统每次执行内容提取前，通过 `os.path.getmtime` 校验词库文件，检测到变更时自动重新载入内存，做到零重启实时维护。

### 6.4 安全过滤挂载点
- **HTML DOM 前置清洗**：在 [`trawler/parser/extract.py:L43`](file:///d:/.//trawler/parser/extract.py#L43) 调用 `safety.sanitize_html_soup()`，移除含有 `display:none`、`visibility:hidden` 以及 `font-size:0px` 等隐藏样式的元素，防止其在背景中注入隐藏指令。
- **Markdown 最终净化**：在 [`trawler/parser/extract.py:L58`](file:///d:/.//trawler/parser/extract.py#L58) 返回最终文本前，对其整体应用 `safety.sanitize_markdown()` 掩码过滤。
