"""Trawler-mcp 入口。

用法:
  uv run trawler         # 启动 MCP server (stdio)
  uv run python -m trawler  # 同上
"""

from __future__ import annotations

from dotenv import load_dotenv


def main() -> None:
    """启动 Trawler MCP server。"""
    load_dotenv()  # 加载 .env (可选)
    
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass  # On Windows or if not installed

    from trawler.server import main as server_main
    server_main()


if __name__ == "__main__":
    main()
