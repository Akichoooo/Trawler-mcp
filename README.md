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

## 环境变量

见 [.env.example](./.env.example)。关键:

- `TRAWLER_ALLOW_LOCAL=1` — 放行内网 (SSRF 守卫 opt-in)
- `TRAWLER_CAPSOLVER_KEY=...` — HITL 自动过码 (opt-in, 责任自负)

## 边界

Trawler **只做** 抓取 + 存 raw。**不做** OKF/检索/写文章/PDF 解析/LLM 调用/UI。
