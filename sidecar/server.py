"""Local OpenAI-compatible proxy in front of the Zen anonymous free lane.

Endpoints (``base_url`` for Hermes is ``http://127.0.0.1:<port>/v1``):

  GET  /healthz              liveness + catalog state (not OpenAI-shaped)
  GET  /v1/models            the free roster, OpenAI list shape
  POST /v1/chat/completions  gate-shaped proxy, SSE passthrough
  POST /v1/messages          Anthropic shape, for the ``union-alpha`` lane
  GET  /                     a tiny status page so a browser visit is not a 404

Everything the gate needs is applied in :mod:`sidecar.gate`; this module is
transport and error mapping only.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import ids
from .catalog import Catalog
from .config import Config, resolve
from .gate import build_headers, shape_body
from .upstream import UpstreamError, post_stream

logger = logging.getLogger("opencodezen4hermes")

#: Long-lived SSE streams would otherwise pin a thread per token forever.
DAEMON_THREADS = True

#: Refuse request bodies larger than this instead of trusting the client's
#: ``Content-Length``. 32 MiB is far beyond any chat turn Hermes sends (the
#: largest realistic case is a long conversation plus a few inline images) and
#: small enough that a hostile or broken client cannot make the sidecar
#: allocate unbounded memory or stall a worker thread.
MAX_BODY_BYTES = 32 * 1024 * 1024

#: How long a client may take to deliver the body it announced. Applied only
#: while the request body is being read, then cleared, so it cannot clip the
#: long-lived SSE response that follows.
BODY_READ_TIMEOUT = 30.0

#: Browser origins allowed to call the sidecar. The WebUI talks to the sidecar
#: from its own Python process (no ``Origin`` header), so a browser request can
#: only ever be a third-party page reaching loopback. Such a page holds no
#: secret, but it could burn the user's free-lane quota, so cross-origin
#: browser callers are refused while non-browser clients stay unaffected.
_LOOPBACK_ORIGIN_RE = re.compile(r"^https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$")


class _State:
    """Process-wide state: the config and one catalog instance."""

    config: Config
    catalog: Catalog


def _openai_model_entry(model_id: str, catalog: Catalog) -> dict[str, Any]:
    limits = catalog.limits(model_id) or {}
    entry: dict[str, Any] = {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "opencode-zen-free",
    }
    if limits.get("context_window"):
        entry["context_window"] = limits["context_window"]
    if limits.get("max_output"):
        entry["max_output_tokens"] = limits["max_output"]
    modalities = catalog.modalities(model_id)
    if modalities:
        entry["input_modalities"] = modalities
    return entry


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "opencodezen4hermes"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        if _State.config.verbose:
            logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, status: int, message: str, *, kind: str = "invalid_request_error") -> None:
        self._send_json(status, {"error": {"message": message, "type": kind, "code": status}})

    def _cross_origin(self) -> str | None:
        """The offending ``Origin`` when a browser page on another site calls us.

        Non-browser clients (Hermes, curl, the test harness) send no ``Origin``
        header at all, and the sidecar's own pages are served from loopback, so
        only a genuine third-party page is rejected. ``Origin: null`` (opaque
        origin: sandboxed iframe, ``file://``) is treated as cross-origin
        because it identifies no loopback page.
        """
        origin = self.headers.get("origin")
        if not origin:
            return None
        return None if _LOOPBACK_ORIGIN_RE.match(origin) else origin

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            # Do not read it: a client that announces 900 MB must not be able to
            # make the sidecar allocate it or hold a worker thread waiting.
            self._send_error_json(
                413,
                f"Request body of {length} bytes exceeds the {MAX_BODY_BYTES}-byte limit.",
                kind="request_too_large",
            )
            return None
        if length:
            # Bound only the body read; the SSE response that follows may idle
            # for as long as OPENCODEZEN4HERMES_TIMEOUT allows.
            try:
                self.connection.settimeout(BODY_READ_TIMEOUT)
            except OSError:
                pass
            try:
                raw = self.rfile.read(length)
            except (socket.timeout, TimeoutError):
                self._send_error_json(
                    408,
                    "Timed out reading the request body.",
                    kind="request_timeout",
                )
                return None
            finally:
                try:
                    self.connection.settimeout(None)
                except OSError:
                    pass
        else:
            raw = b""
        if not raw:
            self._send_error_json(400, "Request body is empty; expected JSON.")
            return None
        try:
            parsed = json.loads(raw)
        except ValueError as error:
            self._send_error_json(400, f"Request body is not valid JSON: {error}")
            return None
        if not isinstance(parsed, dict):
            self._send_error_json(400, "Request body must be a JSON object.")
            return None
        return parsed

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in {"/healthz", "/health"}:
            self._send_json(200, {"ok": True, "catalog": _State.catalog.snapshot()})
            return
        if path == "/v1/models":
            # Stale-while-revalidate: answer from the current snapshot and let a
            # lapsed TTL re-derive it in the background for the next caller.
            _State.catalog.maybe_refresh()
            models = _State.catalog.models()
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [_openai_model_entry(m, _State.catalog) for m in models],
                },
            )
            return
        if path in {"/", "/v1"}:
            snapshot = _State.catalog.snapshot()
            body = (
                "opencodezen4hermes sidecar\n\n"
                f"base_url : http://{_State.config.host}:{_State.config.port}/v1\n"
                f"upstream : {_State.config.lane_base}\n"
                f"free models exposed : {snapshot['exposed']} of {snapshot['total']} live\n\n"
                "Endpoints: GET /healthz, GET /v1/models, POST /v1/chat/completions\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_error_json(404, f"No route for GET {path}", kind="not_found")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0].rstrip("/")
        origin = self._cross_origin()
        if origin is not None:
            self._send_error_json(
                403,
                f"Cross-origin request from {origin!r} refused. The sidecar only "
                "serves the local WebUI.",
                kind="cross_origin_refused",
            )
            return
        _State.catalog.maybe_refresh()
        if path == "/v1/chat/completions":
            self._handle_chat()
            return
        if path == "/v1/messages":
            self._handle_messages()
            return
        self._send_error_json(404, f"No route for POST {path}", kind="not_found")

    # -- handlers ---------------------------------------------------------

    def _handle_chat(self) -> None:
        body = self._read_json()
        if body is None:
            return

        model = body.get("model")
        if not isinstance(model, str) or not model:
            self._send_error_json(400, "Missing required field: model.")
            return

        verdict = _State.catalog.decision(model)
        if not verdict.allowed and not _State.config.include_paid:
            self._send_error_json(
                400,
                f"Model {model!r} is not on the free OpenCode Zen roster "
                f"(verdict: {verdict.source}). Call GET /v1/models for the "
                "current list.",
                kind="model_not_available",
            )
            return

        session_id = ids.canonical_session_id(ids.conversation_signal(body.get("messages") or []))
        request_id = ids.generate_request_id()
        shaped = shape_body(body, session_id=session_id, request_id=request_id)
        headers = build_headers(session_id, request_id)

        started = time.monotonic()
        try:
            upstream = post_stream(
                _State.config.chat_url,
                headers,
                shaped,
                timeout=_State.config.upstream_timeout,
                connect_timeout=_State.config.connect_timeout,
            )
        except UpstreamError as error:
            self._relay_upstream_error(error)
            return

        try:
            self._relay_stream(upstream)
        finally:
            upstream.close()
        if _State.config.verbose:
            logger.info(
                "chat %s -> %s in %.2fs",
                model,
                upstream.status,
                time.monotonic() - started,
            )

    def _handle_messages(self) -> None:
        """Anthropic-shape lane.

        Kept deliberately thin — the caller owns the Anthropic body shape and
        the response is streamed back untouched — but the same free-roster
        decision check as ``_handle_chat`` applies. Without it this route would
        be an unguarded second door onto the lane, so a request for a paid or
        blocklisted model would be forwarded instead of refused, contradicting
        the one contract the sidecar exists to enforce.
        """
        body = self._read_json()
        if body is None:
            return
        model = body.get("model")
        if not isinstance(model, str) or not model:
            self._send_error_json(400, "Missing required field: model.")
            return
        verdict = _State.catalog.decision(model)
        if not verdict.allowed and not _State.config.include_paid:
            self._send_error_json(
                400,
                f"Model {model!r} is not on the free OpenCode Zen roster "
                f"(verdict: {verdict.source}). Call GET /v1/models for the "
                "current list.",
                kind="model_not_available",
            )
            return
        session_id = ids.generate_session_id()
        request_id = ids.generate_request_id()
        headers = build_headers(session_id, request_id)
        headers["accept"] = "text/event-stream"
        headers["anthropic-version"] = "2023-06-01"
        url = _State.config.lane_base.rstrip("/") + "/messages"
        try:
            upstream = post_stream(
                url,
                headers,
                body,
                timeout=_State.config.upstream_timeout,
                connect_timeout=_State.config.connect_timeout,
            )
        except UpstreamError as error:
            self._relay_upstream_error(error)
            return
        try:
            self._relay_stream(upstream)
        finally:
            upstream.close()

    # -- relay ------------------------------------------------------------

    def _relay_upstream_error(self, error: UpstreamError) -> None:
        # 403 from the lane is a gate/quota verdict, not an auth problem, and
        # the WebUI renders 401/403 specially — so map it to 502 with the
        # lane's own explanation attached. A genuine 4xx from upstream (bad
        # model, bad shape) passes through with its status intact.
        status = error.status
        if status in {401, 403}:
            status = HTTPStatus.BAD_GATEWAY
        self._send_error_json(status, error.message, kind="upstream_error")

    def _relay_stream(self, upstream: Any) -> None:
        self.send_response(200)
        self.send_header("content-type", upstream.headers.get("content-type", "text/event-stream"))
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        # No content-length: the response is chunked and open-ended.
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

        try:
            for chunk in upstream.body:
                if not chunk:
                    continue
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The browser tab closed mid-stream. Not an error worth logging at
            # anything above debug.
            logger.debug("client disconnected mid-stream")

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


def _port_free(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _already_running(host: str, port: int) -> bool:
    """True when something already answers /healthz on this port."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        return bool(payload.get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def serve(config: Config) -> int:
    _State.config = config
    cache_dir = os_environ_cache_dir()
    _State.catalog = Catalog(
        config.lane_base,
        cache_dir,
        ttl=config.catalog_ttl,
        user_agent=config.user_agent,
        include_paid=config.include_paid,
    )

    if _already_running(config.host, config.port):
        logger.info("sidecar already listening on %s:%s", config.host, config.port)
        return 0
    if not _port_free(config.host, config.port):
        logger.error(
            "port %s on %s is taken by another process; set OPENCODEZEN4HERMES_PORT",
            config.port,
            config.host,
        )
        return 2

    # Warm the catalog before the first request so the picker is never empty,
    # but do not block startup on a slow network.
    _State.catalog.warm(timeout=5.0)

    ThreadingHTTPServer.daemon_threads = DAEMON_THREADS
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    logger.info(
        "opencodezen4hermes sidecar on http://%s:%s/v1 -> %s",
        config.host,
        config.port,
        config.lane_base,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()
    return 0


def os_environ_cache_dir() -> str:
    import os

    override = os.environ.get("OPENCODEZEN4HERMES_CACHE_DIR")
    if override:
        return override
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "opencodezen4hermes")


def main(argv: list[str] | None = None) -> int:
    config, args = resolve(argv)
    if getattr(args, "print_config", False):
        print(
            json.dumps(
                {
                    "host": config.host,
                    "port": config.port,
                    "lane_base": config.lane_base,
                    "catalog_ttl": config.catalog_ttl,
                    "include_paid": config.include_paid,
                    "verbose": config.verbose,
                    "base_url": f"http://{config.host}:{config.port}/v1",
                },
                indent=2,
            )
        )
        return 0
    logging.basicConfig(
        level=logging.DEBUG if config.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    return serve(config)


if __name__ == "__main__":
    raise SystemExit(main())
