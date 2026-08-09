"""Small OpenTelemetry bridge with a dependency-free trace fallback.

The durable identifiers are useful even when no collector is configured.  If
the OpenTelemetry SDK is installed, the same context is exported through the
configured global tracer provider; otherwise no network or background thread
is created.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str


_current_trace: contextvars.ContextVar[Optional[TraceContext]] = (
    contextvars.ContextVar('chengzhu_trace_context', default=None)
)
_configure_lock = threading.Lock()
_configured = False


def configure_telemetry(default_service_name: str = 'chengzhu-backend') -> bool:
    """Install one OTLP tracer provider when the standard env opts in."""

    global _configured
    if str(os.environ.get('OTEL_SDK_DISABLED', 'false')).lower() == 'true':
        return False
    if not str(os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT') or '').strip():
        return False
    with _configure_lock:
        if _configured:
            return True
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(resource=Resource.create({
                'service.name': str(
                    os.environ.get('OTEL_SERVICE_NAME') or default_service_name
                )[:120],
                'service.version': '1.0.0',
                'deployment.environment': str(
                    os.environ.get('OTEL_DEPLOYMENT_ENVIRONMENT') or 'competition'
                )[:80],
            }))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(provider)
        except (ImportError, RuntimeError, ValueError):
            return False
        _configured = True
        return True


def current_trace() -> Optional[TraceContext]:
    return _current_trace.get()


def new_trace_id() -> str:
    return secrets.token_hex(16)


@contextlib.contextmanager
def traced_span(
    name: str,
    *,
    trace_id: Optional[str] = None,
    attributes: Optional[Dict[str, Any]] = None,
) -> Iterator[TraceContext]:
    """Create a safe span without ever recording payload or prompt content."""

    parent = current_trace()
    fallback = TraceContext(
        trace_id=str(trace_id or (parent.trace_id if parent else new_trace_id())),
        span_id=secrets.token_hex(8),
    )
    token = _current_trace.set(fallback)
    try:
        try:
            from opentelemetry import trace

            tracer = trace.get_tracer('chengzhu.agentteams')
            with tracer.start_as_current_span(name) as span:
                for key, value in (attributes or {}).items():
                    if isinstance(value, (str, bool, int, float)):
                        span.set_attribute(str(key)[:120], value)
                context = span.get_span_context()
                active = fallback
                if getattr(context, 'is_valid', False):
                    active = TraceContext(
                        trace_id=f'{context.trace_id:032x}',
                        span_id=f'{context.span_id:016x}',
                    )
                    _current_trace.set(active)
                yield active
        except ImportError:
            yield fallback
    finally:
        _current_trace.reset(token)
