FROM python:3.13-slim

# 安装系统依赖和 tini (解决进程僵尸/孤儿化问题)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 拷贝项目文件
COPY . /app

# 使用 pip 安装核心依赖 + 浏览器档依赖
RUN pip install --no-cache-dir -e ".[heavy]"

# 安装 patchright 的浏览器依赖
RUN python -m patchright install --with-deps chromium

# 兜底 Chrome 泄漏：让 tini 作为 PID 1 接管所有信号和僵尸进程
ENTRYPOINT ["/usr/bin/tini", "--"]

RUN useradd -m trawler && chown -R trawler:trawler /app /ms-playwright
USER trawler

# 启动 MCP server
CMD ["trawler"]
