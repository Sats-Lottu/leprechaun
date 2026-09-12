import logging
import threading
from collections import defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pls.settings import get_settings

log = logging.getLogger(get_settings().SERVICE_NAME)
_lock = threading.Lock()
_counters: defaultdict[tuple[str, tuple[tuple[str, str], ...]], int]
_counters = defaultdict(int)
_server: ThreadingHTTPServer | None = None


def inc_counter(name: str, **labels: object) -> None:
    normalized_labels = tuple(
        sorted((key, str(value)) for key, value in labels.items())
    )
    with _lock:
        _counters[(name, normalized_labels)] += 1


def _labels_text(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    escaped = [
        (key, value.replace("\\", "\\\\").replace('"', '\\"'))
        for key, value in labels
    ]
    values = ",".join(
        f'{key}="{value}"'
        for key, value in escaped
    )
    return f"{{{values}}}"


def render_metrics() -> str:
    with _lock:
        counters = dict(_counters)

    lines = [
        "# HELP pls_events_total PLS operational events.",
        "# TYPE pls_events_total counter",
    ]
    for (name, labels), value in sorted(counters.items()):
        metric_labels = (("event", name), *labels)
        lines.append(f"pls_events_total{_labels_text(metric_labels)} {value}")
    return "\n".join(lines) + "\n"


class ObservabilityHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_text("ok\n", HTTPStatus.OK)
            return
        if self.path == "/metrics":
            self._send_text(render_metrics(), HTTPStatus.OK)
            return
        self._send_text("not found\n", HTTPStatus.NOT_FOUND)

    def log_message(self, _format: str, *_args: object) -> None:  # noqa: PLR6301
        return

    def _send_text(self, body: str, status: HTTPStatus) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_observability_server() -> ThreadingHTTPServer | None:
    global _server  # noqa: PLW0603

    settings = get_settings()
    if not settings.OBSERVABILITY_ENABLED:
        return None
    if _server is not None:
        return _server

    _server = ThreadingHTTPServer(
        (settings.OBSERVABILITY_HOST, settings.OBSERVABILITY_PORT),
        ObservabilityHandler,
    )
    thread = threading.Thread(
        target=_server.serve_forever,
        name="pls-observability",
        daemon=True,
    )
    thread.start()
    log.info(
        "observability_server_started host=%s port=%s",
        settings.OBSERVABILITY_HOST,
        settings.OBSERVABILITY_PORT,
    )
    return _server
