import json
import logging
import sys
import time
from typing import Any

from fastapi import Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

from ledger.settings import get_settings

HTTP_REQUESTS = Counter(
    "ledger_http_requests_total",
    "Total HTTP requests handled by the ledger.",
    ("method", "path", "status_code"),
)
HTTP_REQUEST_DURATION = Histogram(
    "ledger_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "path"),
)
LEDGER_ERRORS = Counter(
    "ledger_errors_total",
    "Ledger errors by code.",
    ("code",),
)
HOLDS_EXPIRED = Counter(
    "ledger_holds_expired_total",
    "Balance holds expired by the operational expiration routine.",
)
HOLD_EXPIRATION_BATCHES = Counter(
    "ledger_hold_expiration_batches_total",
    "Hold expiration batches by result.",
    ("result",),
)
TRANSACTIONS_POSTED = Counter(
    "ledger_transactions_posted_total",
    "Ledger transactions posted successfully.",
)
TRANSACTIONS_REVERSED = Counter(
    "ledger_transactions_reversed_total",
    "Ledger transactions reversed successfully.",
)
PAYMENT_EVENTS = Counter(
    "ledger_payment_events_total",
    "Payment events consumed by the ledger.",
    ("event_type", "result"),
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "service": get_settings().SERVICE_NAME,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key.startswith("_") and key != "_ledger_extra":
                continue
            if key == "_ledger_extra" and isinstance(value, dict):
                payload.update(value)

        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging() -> None:
    logging.basicConfig(
        level=get_settings().LOG_LEVEL,
        stream=sys.stdout,
        force=True,
    )
    formatter = JsonFormatter()
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    logger.log(
        level,
        event,
        extra={"_ledger_extra": {"event": event, **fields}},
    )


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def route_path(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    return request.url.path


async def record_http_metrics(
    request: Request,
    call_next,
) -> Response:
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    path = route_path(request)
    status_code = str(response.status_code)

    HTTP_REQUESTS.labels(
        method=request.method,
        path=path,
        status_code=status_code,
    ).inc()
    HTTP_REQUEST_DURATION.labels(
        method=request.method,
        path=path,
    ).observe(elapsed)
    return response
