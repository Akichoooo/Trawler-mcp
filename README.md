# Trawler-mcp

项目总览：[docs/PROJECT_OVERVIEW.md](./docs/PROJECT_OVERVIEW.md)

> **拖网渔船** — 纯爬虫薄壳 MCP server。给 AI agent 从网页抓取干净 markdown 正文,存成 raw 文件交给下游"图书馆"模块处理。

零 LLM。设计宪法见 [DESIGN.md](./DESIGN.md)。

## 安装

```bash
# 轻量核心 (纯 API 抓取: 文档/维基/博客, 无需浏览器)
uv sync

# 完整能力 (含 patchright 反检测浏览器 + trafilatura 提取链)
uv sync --extra heavy --extra dev
patchright install chrome
```

## 运行 (作为 MCP server)

```bash
uv run trawler
# 或
uv run python -m trawler
```

## 测试

```bash
uv run --extra dev python -m pytest
```

## 接入 Cursor / Claude Desktop

在 MCP 配置 (Cursor: `~/.cursor/mcp.json`, Claude Desktop: `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "trawler": {
      "command": "uv",
      "args": ["--directory", "./", "run", "trawler"]
    }
  }
}
```

## 工具

| 工具 | 说明 |
|---|---|
| `crawl_url(url, use_proxy?, cache_mode?, mode?)` | 抓单页, 返回 legacy markdown 字符串；`crawl_url_structured` 额外返回 structuredContent |
| `crawl_site(start_url, max_pages=20, same_domain_only=true, max_depth=-1, include_paths?, exclude_paths?, include_subdomains=false, ignore_query_parameters=false)` | 持久 frontier 多页爬取, 异步返回 job_id |
| `crawl_site_structured(...)` / `crawl_site_indexed_structured(...)` | 多页 crawl 的 structuredContent 双轨入口 |
| `crawl_site_indexed(start_url, max_seed_urls=200, ...)` | 先发现 sitemap/feed seed, 再按同一 crawl policy 入队 |
| `map_site(start_url, max_links=200, ...)` | 单页链接预览, 支持同样的 path/subdomain/query 过滤 |
| `wait_for_job(job_id, timeout=120)` | 阻塞等异步作业 |
| `get_job_status(job_id)` | 非阻塞查进度 |
| `list_raw()` / `get_raw(path)` | 看 raw 归档 (路径白名单) |
| `get_engine_status()` | 引擎健康度 |

资源: `raw://{raw_id}` — 客户端原生侧边栏文件树。

## 内容安全与合规防御层 (Content Safety & Defense Module)

Trawler 内置了开箱即用的内容安全防护模块，位于 `trawler/parser/safety.py`。该模块作为一个独立的管道阶段拦截并净化爬取到的网页文本，以保护下游 Agent 免受恶意注入或不合规数据污染。

该模块将所有安全策略聚合为 **四个主要防护维度**：

1. **防间接提示词注入 (Indirect Prompt Injection Protection)**：
   - 拦截并打断 LLM 聊天控制 Token（如 `<|im_start|>`、`[INST]`、`<s>` 等）。
   - 匹配指令覆盖短语（如 `ignore all previous instructions`），自动插入零宽空格（`\u200b`）切碎 Token 语义，使其在人眼阅读时保持可读，但对 LLM 而言失效。
   - 转义 HTML/XML 伪造系统角色标签（如 `<system>` -> `&lt;system&gt;`）。
2. **PII（个人敏感数据）脱敏掩码**：
   - **手机号**：脱敏中间 4 位（例如 `138****5678`）。
   - **身份证**：脱敏出生年月日与尾部共 10 位（例如 `110101**********45`）。
   - **邮箱**：脱敏邮箱用户名部分（例如 `j******e@example.com`）。
   - **银行卡/信用卡**：模糊化卡号中间所有数值。
3. **恶意 HTML 样式消除**：
   - 在解析 HTML DOM 时，自动移除 `display:none`、`visibility:hidden` 以及 `font-size:0px` 等隐藏样式的元素，彻底切断恶意网页在背景中植入隐形指令的途径。
4. **敏感词与合规词库匹配 (Hot-Reloadable Wordlist Filtering)**：
   - 自动匹配由 `TOXIC`（涉黄涉暴）、`COMPLIANCE`（违法与黑灰产）、`JAILBREAK`（越狱指令）、`CUSTOM`（自定义机密）标记的词汇，并替换为 `[MASKED_类别名]`。

### 🤖 给其他 Agent 的维护与使用手册 (For Other Agents)
若接管本项目的 Agent 需要动态更新敏感词、合规屏蔽词，**无需修改任何代码，也无需重启服务器**：
- **操作方式**：直接向本地/服务器目录下的 **`data/sensitive_words.txt`** 读写敏感词数据。
- **格式规范**：以 `类别:词汇` 写入，如 `COMPLIANCE:科学上网` 或 `CUSTOM:项目机密数据`。
- **生效机制**：Trawler 系统采用了热重载（Hot-Reloading）机制。在下一次执行网页提取时，系统会自动检测 `sensitive_words.txt` 的 `mtime`（修改时间），并在内存中实时重构超高速的匹配正则，无缝生效。

## 环境变量

见 [.env.example](./.env.example)。关键:

- `TRAWLER_ALLOW_LOCAL=1` — 放行内网 (SSRF 守卫 opt-in)
- `TRAWLER_CAPSOLVER_KEY=...` — HITL 自动过码 (opt-in, 责任自负)

## 边界

Trawler **只做** 抓取 + 存 raw。**不做** OKF/检索/写文章/PDF 解析/LLM 调用/UI。
