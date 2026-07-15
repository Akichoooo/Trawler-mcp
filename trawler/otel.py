"""otel — OpenTelemetry 集成 (可选依赖, 优雅降级)。

启动时初始化 TracerProvider + OTLP exporter (如配置 endpoint)。
工具入口用 span_context 创建 span, 桥接现有 trace_id_var。

无 opentelemetry 依赖时: span_context 降级为 no-op, 不影响功能。
"""

from __future__ import annotations

import contextlib
import logging
import os

log = logging.getLogger("trawler.otel")

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        _OTLP_AVAILABLE = True
    except ImportError:
        _OTLP_AVAILABLE = False
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    _OTLP_AVAILABLE = False
    trace = None  # type: ignore

_tracer = None
_initialized = False


def init_otel() -> None:
    """初始化 OTel provider。无依赖或无配置时降级为 no-op。"""
    global _tracer, _initialized
    if _initialized:
        return
    _initialized = True

    if not _OTEL_AVAILABLE:
        log.info("OpenTelemetry not installed — tracing disabled (pip install opentelemetry-api opentelemetry-sdk)")
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "trawler-mcp")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint and _OTLP_AVAILABLE:
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        log.info("OpenTelemetry OTLP exporter configured: %s", endpoint)
    else:
        log.info("OpenTelemetry tracing enabled (no OTLP exporter — spans in-process only)")

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("trawler")


def get_tracer():
    """获取 tracer, 未初始化或无依赖时返回 None。"""
    return _tracer


@contextlib.contextmanager
def span_context(name: str, **attributes):
    """创建 OTel span, 未初始化时降级为 no-op。

    用法:
        with span_context("crawl_url", url=url, domain=domain):
            ...

    桥接: span 启动时从 trace_id_var/span_id_var 读取现有 trace 关联。
    异常时: set_status(ERROR) + record_exception, 让 OTel 链路追踪能看到错误 span。
    """
    if not _tracer:
        yield None
        return
    from trawler.tracing import trace_id_var, span_id_var

    with _tracer.start_as_current_span(name) as span:
        for k, v in attributes.items():
            if v is not None:
                try:
                    span.set_attribute(k, str(v) if not isinstance(v, (int, float, bool)) else v)
                except Exception:
                    pass
        # 桥接现有 trace_id 到 OTel (如 crawl_url 已设置 trace_id_var)
        tid = trace_id_var.get()
        if tid:
            span.set_attribute("trawler.trace_id", tid)
        sid = span_id_var.get()
        if sid:
            span.set_attribute("trawler.span_id", sid)
        try:
            yield span
        except BaseException as e:
            # P3: 异常时标记 span 为错误 + 记录异常 (OTel 链路追踪可看到错误 span)
            try:
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
            except Exception:
                pass
            raise
