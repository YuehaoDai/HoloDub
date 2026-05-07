"""Observability primitives for the ml-service.

Provides:
  - Prometheus metrics (call counts / latency / GPU wait / TTS warmup gauge).
  - A request-ID middleware that injects a stable correlation ID into log
    records and outbound responses.
  - Helpers used by route handlers to time GPU and inference work.

Kept separate from app/main.py so that future work (OpenTelemetry traces,
custom log formatter) has a single dedicated module to extend.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

try:  # prometheus_client is an optional but recommended runtime dep
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover — graceful fallback when not installed
    _PROM_AVAILABLE = False

    class _Stub:
        def __init__(self, *_, **__):
            pass

        def labels(self, *_, **__):
            return self

        def inc(self, *_, **__):
            pass

        def observe(self, *_, **__):
            pass

        def set(self, *_, **__):
            pass

        def time(self):
            class _Ctx:
                def __enter__(self_inner):
                    self_inner.t0 = time.monotonic()
                    return self_inner

                def __exit__(self_inner, *_):
                    return False

            return _Ctx()

    Counter = Gauge = Histogram = _Stub  # type: ignore[assignment]
    CONTENT_TYPE_LATEST = "text/plain"

    def generate_latest() -> bytes:  # type: ignore[no-redef]
        return b""


REQUEST_ID_HEADER = "X-Request-Id"


# --- Metrics ---------------------------------------------------------------

http_requests_total = Counter(
    "holodub_ml_http_requests_total",
    "ml-service HTTP requests by method, path, status.",
    ["method", "path", "status"],
)

http_request_duration_seconds = Histogram(
    "holodub_ml_http_request_duration_seconds",
    "ml-service HTTP request latency in seconds.",
    ["method", "path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)

inference_duration_seconds = Histogram(
    "holodub_ml_inference_duration_seconds",
    "Per-stage inference time inside the ml-service worker thread.",
    ["stage"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)

gpu_wait_seconds = Histogram(
    "holodub_ml_gpu_wait_seconds",
    "Time a request waited to acquire the GPU semaphore.",
    ["stage"],
    buckets=(0.001, 0.01, 0.1, 0.5, 1, 5, 30, 120),
)

tts_warmup_status = Gauge(
    "holodub_ml_tts_warmup_status",
    "IndexTTS2 inline warm-up state. 0=idle, 1=loading, 2=ready, 3=error.",
)


def set_warmup_status(status: str) -> None:
    """Translate the string warmup state into a numeric gauge value."""
    mapping = {"idle": 0, "loading": 1, "ready": 2, "error": 3}
    tts_warmup_status.set(mapping.get(status, 0))


@asynccontextmanager
async def observe_inference(stage: str):
    """Record inference latency for a code block.

    Use as ``async with observe_inference("tts"):`` around CPU/GPU bound
    work. Wraps Prometheus Histogram.time() so async cancellation also
    completes the observation.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        inference_duration_seconds.labels(stage=stage).observe(time.monotonic() - started)


# --- Middleware ------------------------------------------------------------


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Inject a stable request id into the request scope and response.

    If the caller (the Go control plane in the typical deployment) sends an
    ``X-Request-Id`` header we propagate it; otherwise we generate a UUIDv4.
    Down-stream log records can grab the value via
    ``request.state.request_id`` or the dedicated logging filter.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        rid = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = rid

        start = time.monotonic()
        response = await call_next(request)
        elapsed = time.monotonic() - start

        # Populate metrics with the matched path template (if any) so we
        # don't blow up cardinality on path parameters.
        route = request.scope.get("route")
        path_template = getattr(route, "path", request.url.path)
        http_requests_total.labels(
            method=request.method,
            path=path_template,
            status=str(response.status_code),
        ).inc()
        http_request_duration_seconds.labels(
            method=request.method, path=path_template
        ).observe(elapsed)

        response.headers[REQUEST_ID_HEADER] = rid
        return response


class _RequestIDLogFilter(logging.Filter):
    """A logging filter that no-ops but exists so future log formatters
    can pull request IDs from contextvars without runtime errors.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        return True


def install_log_filter() -> None:
    logging.getLogger().addFilter(_RequestIDLogFilter())


# --- /metrics endpoint -----------------------------------------------------


def metrics_response() -> Response:
    """Return the Prometheus exposition payload for the /metrics endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
