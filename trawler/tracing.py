"""tracing — Context vars for trace propagation + telemetry context manager.

业务富字段: tool_name / request_id / agent_id / latency_ms
JSON 日志的 TracingFilter 读取这些 contextvar 注入到每条日志记录。
telemetry_context 在工具入口设置, 退出时输出 summary log (含 latency_ms + result)。
"""

import contextlib
import contextvars
import logging
import time
import uuid

trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
span_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("span_id", default=None)

# 业务富字段 (供 JSON 日志 TracingFilter 读取)
tool_name_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("tool_name", default=None)
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
agent_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent_id", default=None)

_log = logging.getLogger("trawler.telemetry")


@contextlib.contextmanager
def telemetry_context(
    tool_name: str,
    *,
    agent_id: str = "",
    request_id: str = "",
):
    """设置 telemetry contextvar, 退出时输出 summary log (含 latency_ms)。

    用法:
        with telemetry_context("crawl_url", agent_id=account_id):
            ...

    退出时自动复位 contextvar (防泄漏到调用方 Task 上下文外)。
    summary log 带 tool_name / request_id / agent_id / latency_ms, 供 Loki/Prometheus 聚合。
    """
    if not request_id:
        request_id = str(uuid.uuid4())
    tool_token = tool_name_var.set(tool_name)
    req_token = request_id_var.set(request_id)
    agent_token = agent_id_var.set(agent_id)
    start = time.perf_counter()
    status = "ok"
    error_type = ""
    try:
        yield
    except BaseException as e:
        status = "error"
        error_type = type(e).__name__
        raise
    finally:
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        tool_name_var.reset(tool_token)
        request_id_var.reset(req_token)
        agent_id_var.reset(agent_token)
        _log.info(
            "tool_call completed: %s status=%s latency=%.2fms request_id=%s agent_id=%s",
            tool_name, status, latency_ms, request_id, agent_id or "-",
            extra={
                "tool_name": tool_name,
                "request_id": request_id,
                "agent_id": agent_id or "",
                "latency_ms": latency_ms,
                "status": status,
                "error_type": error_type,
            },
        )
