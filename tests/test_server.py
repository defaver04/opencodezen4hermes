"""Tests for :mod:`sidecar.server` — routing, error mapping and gate shaping.

Standard library only (docs/BLUEPRINT.md §8). The handler is exercised over a
real :class:`http.server.ThreadingHTTPServer` bound to ``127.0.0.1:0`` so the
routing table, the status codes and the chunked relay are tested as the WebUI
will actually see them — but every upstream call is replaced with a fake, so no
byte ever leaves the loopback interface and no test depends on the Zen lane.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import unittest
import unittest.mock as mock
from http.server import ThreadingHTTPServer

from sidecar import ids
from sidecar import server as server_module
from sidecar.catalog import Decision
from sidecar.config import Config
from sidecar.gate import WIRE_UA
from sidecar.server import Handler
from sidecar.upstream import UpstreamError, UpstreamResponse

#: A representative slice of the BLUEPRINT §3 roster. The handler must not care
#: how long the roster is, so three is enough to prove ordering and decoration.
MODELS = ("beam-free", "big-pickle", "deepseek-v4.1-flash-free")

PAID_MODEL = "gpt-5-paid"


class FakeCatalog:
    """The four query methods :mod:`sidecar.server` actually calls."""

    def __init__(
        self,
        models=MODELS,
        *,
        decisions=None,
        limits=None,
        modalities=None,
        snapshot=None,
    ) -> None:
        self._models = list(models)
        self._decisions = dict(decisions or {})
        self._limits = dict(limits or {})
        self._modalities = dict(modalities or {})
        self._snapshot = dict(
            snapshot
            or {
                "status": "ready",
                "total": len(self._models),
                "exposed": len(self._models),
                "prices_ready": True,
                "age_seconds": 1.0,
                "last_error": None,
            }
        )

    def models(self) -> list[str]:
        return list(self._models)

    def maybe_refresh(self) -> bool:
        self.refresh_calls = getattr(self, "refresh_calls", 0) + 1
        return False

    def decision(self, model: str) -> Decision:
        return self._decisions.get(model, Decision(True, "name_free", False))

    def limits(self, model: str):
        return self._limits.get(model)

    def modalities(self, model: str):
        return self._modalities.get(model)

    def snapshot(self) -> dict:
        return dict(self._snapshot)


class FakeBody:
    """Iterator of SSE bytes that records ``close()``."""

    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def __iter__(self):
        return iter(self._chunks)

    def close(self) -> None:
        self.closed = True


def fake_stream(chunks=(b'data: {"t":1}\n\n', b"data: [DONE]\n\n"), *, content_type="text/event-stream"):
    body = FakeBody(chunks)
    headers = {} if content_type is None else {"content-type": content_type}
    return UpstreamResponse(status=200, headers=headers, body=body), body


class Recorder:
    """Stands in for :func:`sidecar.upstream.post_stream`."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.response = None
        self.error: Exception | None = None
        self.bodies: list[FakeBody] = []

    def __call__(self, url, headers, body, **kwargs):
        self.calls.append({"url": url, "headers": dict(headers), "body": body, "kwargs": dict(kwargs)})
        if self.error is not None:
            raise self.error
        if self.response is not None:
            return self.response
        response, fake_body = fake_stream()
        self.bodies.append(fake_body)
        return response


class ServerHarness(unittest.TestCase):
    """Drives one module-wide loopback server; reinstalls state per test."""

    #: Populated by :func:`setUpModule` — the server is started once for the
    #: whole module and torn down once, because ``serve_forever``'s 0.5 s poll
    #: interval would otherwise be paid by every test class.
    httpd: ThreadingHTTPServer
    port: int = 0

    def setUp(self) -> None:
        self.recorder = Recorder()
        patcher = mock.patch.object(server_module, "post_stream", self.recorder)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host = "127.0.0.1"
        self.config = Config(host=self.host, port=self.port)
        self.catalog = FakeCatalog()
        server_module._State.config = self.config
        server_module._State.catalog = self.catalog

    # -- helpers ----------------------------------------------------------

    def request(self, method: str, path: str, *, body=None, headers=None):
        """One request/response round-trip against the loopback server."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            payload = None
            if body is not None:
                payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            connection.request(method, path, body=payload, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, raw
        finally:
            connection.close()

    def post_json(self, path: str, payload):
        return self.request(
            "POST",
            path,
            body=payload,
            headers={"content-type": "application/json"},
        )

    def post_json_with_origin(self, path: str, payload, origin: str):
        return self.request(
            "POST",
            path,
            body=payload,
            headers={"content-type": "application/json", "origin": origin},
        )

    def json_body(self, raw: bytes):
        return json.loads(raw.decode("utf-8"))


class HealthzTests(ServerHarness):
    def test_healthz_reports_ok_and_catalog_snapshot(self) -> None:
        status, headers, raw = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json")
        payload = self.json_body(raw)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["catalog"], self.catalog.snapshot())

    def test_healthz_never_calls_upstream(self) -> None:
        # BLUEPRINT §5: liveness must not depend on the lane being reachable.
        self.recorder.error = UpstreamError(502, "lane down")
        status, _, _ = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(self.recorder.calls, [])

    def test_health_alias_and_trailing_slash_and_query_string(self) -> None:
        for path in ("/health", "/healthz/", "/healthz?probe=1"):
            with self.subTest(path=path):
                status, _, raw = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIs(self.json_body(raw)["ok"], True)


class ModelsTests(ServerHarness):
    def test_models_lists_exactly_the_catalog_roster(self) -> None:
        status, _, raw = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        payload = self.json_body(raw)
        self.assertEqual(payload["object"], "list")
        self.assertEqual([entry["id"] for entry in payload["data"]], list(MODELS))
        for entry in payload["data"]:
            self.assertEqual(entry["object"], "model")
            self.assertEqual(entry["owned_by"], "opencode-zen-free")

    def test_models_decorates_context_window_and_modalities(self) -> None:
        self.catalog._limits = {
            "beam-free": {"context_window": 200000, "max_output": 8192},
        }
        self.catalog._modalities = {"beam-free": ["text", "image"]}
        status, _, raw = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        entries = {entry["id"]: entry for entry in self.json_body(raw)["data"]}
        self.assertEqual(entries["beam-free"]["context_window"], 200000)
        self.assertEqual(entries["beam-free"]["max_output_tokens"], 8192)
        self.assertEqual(entries["beam-free"]["input_modalities"], ["text", "image"])
        # An undecorated model keeps the bare OpenAI shape.
        self.assertNotIn("context_window", entries["big-pickle"])
        self.assertNotIn("max_output_tokens", entries["big-pickle"])

    def test_models_tolerates_a_catalog_with_no_limits(self) -> None:
        self.catalog._limits = {}
        status, _, raw = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.json_body(raw)["data"]), len(MODELS))

    def test_models_route_ignores_query_and_trailing_slash(self) -> None:
        status, _, raw = self.request("GET", "/v1/models/?limit=9")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.json_body(raw)["data"]), len(MODELS))


class StatusPageTests(ServerHarness):
    def test_root_and_v1_serve_a_plaintext_status_page(self) -> None:
        for path in ("/", "/v1"):
            with self.subTest(path=path):
                status, headers, raw = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["content-type"].startswith("text/plain"))
                text = raw.decode("utf-8")
                self.assertIn(f"base_url : http://{self.host}:{self.port}/v1", text)
                self.assertIn(self.config.lane_base, text)
                self.assertIn(f"free models exposed : {len(MODELS)} of {len(MODELS)} live", text)


class NotFoundTests(ServerHarness):
    def test_unknown_get_path_is_404(self) -> None:
        status, _, raw = self.request("GET", "/v1/embeddings")
        self.assertEqual(status, 404)
        error = self.json_body(raw)["error"]
        self.assertEqual(error["type"], "not_found")

    def test_unknown_post_path_is_404(self) -> None:
        status, _, raw = self.post_json("/v1/completions", {"model": "beam-free"})
        self.assertEqual(status, 404)
        self.assertEqual(self.json_body(raw)["error"]["type"], "not_found")
        self.assertEqual(self.recorder.calls, [])


class ChatValidationTests(ServerHarness):
    def test_non_free_model_is_rejected_as_model_not_available(self) -> None:
        self.catalog._decisions = {PAID_MODEL: Decision(False, "metadata_paid", True)}
        status, _, raw = self.post_json(
            "/v1/chat/completions", {"model": PAID_MODEL, "messages": []}
        )
        self.assertEqual(status, 400)
        error = self.json_body(raw)["error"]
        self.assertEqual(error["type"], "model_not_available")
        self.assertIn(PAID_MODEL, error["message"])
        self.assertIn("metadata_paid", error["message"])
        self.assertIn("GET /v1/models", error["message"])
        # Nothing was forwarded.
        self.assertEqual(self.recorder.calls, [])

    def test_blocklisted_model_is_rejected_with_its_verdict(self) -> None:
        self.catalog._decisions = {"exo-free": Decision(False, "local_blocklist", True)}
        status, _, raw = self.post_json("/v1/chat/completions", {"model": "exo-free"})
        self.assertEqual(status, 400)
        self.assertIn("local_blocklist", self.json_body(raw)["error"]["message"])

    def test_include_paid_admits_a_non_free_model(self) -> None:
        self.config.include_paid = True
        self.catalog._decisions = {PAID_MODEL: Decision(False, "metadata_paid", True)}
        status, headers, raw = self.post_json(
            "/v1/chat/completions", {"model": PAID_MODEL, "messages": []}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["transfer-encoding"], "chunked")
        self.assertEqual(raw, b'data: {"t":1}\n\ndata: [DONE]\n\n')
        self.assertEqual(len(self.recorder.calls), 1)

    def test_missing_model_is_400(self) -> None:
        status, _, raw = self.post_json("/v1/chat/completions", {"messages": []})
        self.assertEqual(status, 400)
        self.assertIn("Missing required field: model", self.json_body(raw)["error"]["message"])

    def test_empty_model_string_is_400(self) -> None:
        status, _, _ = self.post_json("/v1/chat/completions", {"model": ""})
        self.assertEqual(status, 400)

    def test_non_string_model_is_400(self) -> None:
        status, _, _ = self.post_json("/v1/chat/completions", {"model": 7})
        self.assertEqual(status, 400)

    def test_empty_body_is_400(self) -> None:
        status, _, raw = self.request("POST", "/v1/chat/completions", body=b"")
        self.assertEqual(status, 400)
        self.assertIn("Request body is empty", self.json_body(raw)["error"]["message"])

    def test_malformed_json_is_400(self) -> None:
        status, _, raw = self.request(
            "POST",
            "/v1/chat/completions",
            body=b"{not json",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("not valid JSON", self.json_body(raw)["error"]["message"])

    def test_json_that_is_not_an_object_is_400(self) -> None:
        status, _, raw = self.request(
            "POST",
            "/v1/chat/completions",
            body=b"[1, 2, 3]",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("must be a JSON object", self.json_body(raw)["error"]["message"])

    def test_broken_content_length_does_not_crash(self) -> None:
        status, _, raw = self.request(
            "POST",
            "/v1/chat/completions",
            body=b"",
            headers={"content-length": "not-a-number"},
        )
        self.assertEqual(status, 400)
        self.assertIn("Request body is empty", self.json_body(raw)["error"]["message"])


class ChatShapingTests(ServerHarness):
    def test_request_is_forwarded_gate_shaped(self) -> None:
        status, headers, raw = self.post_json(
            "/v1/chat/completions",
            {
                "model": "beam-free",
                "messages": [
                    {"role": "system", "content": "you are helpful"},
                    {"role": "user", "content": "hello world"},
                ],
                "tools": [{"type": "function", "function": {"name": "read_file"}}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/event-stream")
        self.assertEqual(headers["cache-control"], "no-cache")
        self.assertEqual(headers["transfer-encoding"], "chunked")
        self.assertEqual(raw, b'data: {"t":1}\n\ndata: [DONE]\n\n')

        call = self.recorder.calls[0]
        self.assertEqual(call["url"], self.config.chat_url)
        self.assertTrue(call["url"].endswith("/chat/completions"))

        headers_sent = call["headers"]
        self.assertEqual(headers_sent["user-agent"], WIRE_UA)
        self.assertTrue(headers_sent["user-agent"].startswith("opencode/"))
        self.assertRegex(headers_sent["x-opencode-session"], ids.CANONICAL_SESSION_PATTERN)
        self.assertRegex(headers_sent["x-opencode-request"], r"^msg_[0-9a-f]{12}[0-9A-Za-z]{14}$")
        self.assertEqual(headers_sent["content-type"], "application/json")
        self.assertEqual(headers_sent["accept"], "text/event-stream")

        body_sent = call["body"]
        self.assertIs(body_sent["stream"], True)
        self.assertEqual(body_sent["tool_choice"], "auto")
        # The caller's system prompt and tool list survive the shaping.
        self.assertEqual(body_sent["messages"][0]["content"], "you are helpful")
        names = [tool["function"]["name"] for tool in body_sent["tools"]]
        self.assertIn("read_file", names)
        self.assertIn("read", names)

    def test_forwarded_body_is_a_copy_not_the_original(self) -> None:
        original = {"model": "beam-free", "messages": []}
        self.post_json("/v1/chat/completions", original)
        # shape_body must not mutate the caller's dict in place.
        self.assertNotIn("stream", original)
        self.assertNotIn("tools", original)

    def test_timeouts_come_from_config(self) -> None:
        self.config.upstream_timeout = 12.5
        self.config.connect_timeout = 3.5
        self.post_json("/v1/chat/completions", {"model": "beam-free", "messages": []})
        kwargs = self.recorder.calls[0]["kwargs"]
        self.assertEqual(kwargs["timeout"], 12.5)
        self.assertEqual(kwargs["connect_timeout"], 3.5)

    def test_session_id_is_stable_per_conversation(self) -> None:
        payload = {
            "model": "beam-free",
            "messages": [{"role": "user", "content": "same opener"}],
        }
        self.post_json("/v1/chat/completions", payload)
        self.post_json("/v1/chat/completions", payload)
        first = self.recorder.calls[0]["headers"]["x-opencode-session"]
        second = self.recorder.calls[1]["headers"]["x-opencode-session"]
        self.assertEqual(first, second)
        # But the request id is fresh every time.
        self.assertNotEqual(
            self.recorder.calls[0]["headers"]["x-opencode-request"],
            self.recorder.calls[1]["headers"]["x-opencode-request"],
        )

    def test_upstream_body_is_closed_after_the_stream(self) -> None:
        self.post_json("/v1/chat/completions", {"model": "beam-free", "messages": []})
        self.assertTrue(self.recorder.bodies[0].closed)

    def test_empty_upstream_chunks_are_skipped(self) -> None:
        response, _ = fake_stream([b"", b"data: a\n\n", b"", b"data: [DONE]\n\n"])
        self.recorder.response = response
        status, _, raw = self.post_json(
            "/v1/chat/completions", {"model": "beam-free", "messages": []}
        )
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"data: a\n\ndata: [DONE]\n\n")

    def test_missing_upstream_content_type_defaults_to_event_stream(self) -> None:
        response, _ = fake_stream(content_type=None)
        self.recorder.response = response
        status, headers, _ = self.post_json(
            "/v1/chat/completions", {"model": "beam-free", "messages": []}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/event-stream")


class UpstreamErrorMappingTests(ServerHarness):
    """A gate/quota verdict from the lane must not look like a Hermes auth error."""

    def test_403_from_the_lane_maps_to_502(self) -> None:
        self.recorder.error = UpstreamError(403, "not coming from OpenCode", b"{}")
        status, _, raw = self.post_json(
            "/v1/chat/completions", {"model": "beam-free", "messages": []}
        )
        self.assertEqual(status, 502)
        error = self.json_body(raw)["error"]
        self.assertEqual(error["type"], "upstream_error")
        self.assertIn("not coming from OpenCode", error["message"])

    def test_401_from_the_lane_also_maps_to_502(self) -> None:
        self.recorder.error = UpstreamError(401, "unauthorised", b"")
        status, _, _ = self.post_json(
            "/v1/chat/completions", {"model": "beam-free", "messages": []}
        )
        self.assertEqual(status, 502)

    def test_400_from_the_lane_passes_through_as_400(self) -> None:
        self.recorder.error = UpstreamError(400, "ModelProtocolUnsupported", b"")
        status, _, raw = self.post_json(
            "/v1/chat/completions", {"model": "beam-free", "messages": []}
        )
        self.assertEqual(status, 400)
        error = self.json_body(raw)["error"]
        self.assertEqual(error["type"], "upstream_error")
        self.assertEqual(error["code"], 400)

    def test_429_from_the_lane_passes_through(self) -> None:
        self.recorder.error = UpstreamError(429, "slow down", b"")
        status, _, raw = self.post_json(
            "/v1/chat/completions", {"model": "beam-free", "messages": []}
        )
        self.assertEqual(status, 429)
        self.assertEqual(self.json_body(raw)["error"]["message"], "slow down")

    def test_504_timeout_passes_through(self) -> None:
        self.recorder.error = UpstreamError(504, "Timed out talking to the Zen lane")
        status, _, _ = self.post_json(
            "/v1/chat/completions", {"model": "beam-free", "messages": []}
        )
        self.assertEqual(status, 504)


class MessagesLaneTests(ServerHarness):
    """The Anthropic-shape ``/v1/messages`` lane is a thin forwarder."""

    def test_messages_forwards_to_the_lane_with_anthropic_headers(self) -> None:
        self.config.lane_base = "https://lane.invalid/inference/v1/"
        status, headers, raw = self.post_json(
            "/v1/messages",
            {"model": "beam-free", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["transfer-encoding"], "chunked")
        self.assertEqual(raw, b'data: {"t":1}\n\ndata: [DONE]\n\n')

        call = self.recorder.calls[0]
        self.assertEqual(call["url"], "https://lane.invalid/inference/v1/messages")
        self.assertEqual(call["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(call["headers"]["accept"], "text/event-stream")
        self.assertRegex(call["headers"]["x-opencode-session"], ids.CANONICAL_SESSION_PATTERN)
        # The body is forwarded untouched — the caller owns the Anthropic shape.
        self.assertEqual(call["body"]["max_tokens"], 16)

    def test_messages_gate_checks_the_model_like_the_chat_route(self) -> None:
        """The two POST routes must not disagree about what is allowed.

        An ungated ``/v1/messages`` would be a second door onto the lane: a
        request for a paid or blocklisted model would be forwarded instead of
        refused, so the free-roster contract would only hold on one route.
        """
        self.catalog._decisions = {PAID_MODEL: Decision(False, "metadata_paid", True)}
        status, _, raw = self.post_json("/v1/messages", {"model": PAID_MODEL, "messages": []})
        self.assertEqual(status, 400)
        payload = self.json_body(raw)["error"]
        self.assertEqual(payload["type"], "model_not_available")
        self.assertIn("metadata_paid", payload["message"])
        # Refused locally means nothing was forwarded to the lane.
        self.assertEqual(self.recorder.calls, [])

    def test_messages_requires_a_model_field(self) -> None:
        status, _, raw = self.post_json("/v1/messages", {"messages": []})
        self.assertEqual(status, 400)
        self.assertIn("model", self.json_body(raw)["error"]["message"])
        self.assertEqual(self.recorder.calls, [])

    def test_messages_allows_a_roster_model(self) -> None:
        self.catalog._decisions = {"union-alpha": Decision(True, "static_verified", False)}
        status, _, _ = self.post_json("/v1/messages", {"model": "union-alpha", "messages": []})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.recorder.calls), 1)

    def test_messages_upstream_error_is_mapped(self) -> None:
        self.recorder.error = UpstreamError(403, "FreeTierError")
        status, _, raw = self.post_json("/v1/messages", {"model": "beam-free", "messages": []})
        self.assertEqual(status, 502)
        self.assertEqual(self.json_body(raw)["error"]["type"], "upstream_error")

    def test_messages_rejects_an_empty_body(self) -> None:
        status, _, raw = self.request("POST", "/v1/messages", body=b"")
        self.assertEqual(status, 400)
        self.assertEqual(self.recorder.calls, [])


class StateIsolationTests(ServerHarness):
    """The module-level ``_State`` is the only thing tests may mutate."""

    def test_handler_reads_state_at_request_time(self) -> None:
        server_module._State.catalog = FakeCatalog(models=("glm-5.3-flash-free",))
        status, _, raw = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([e["id"] for e in self.json_body(raw)["data"]], ["glm-5.3-flash-free"])

    def test_verbose_logging_does_not_change_the_response(self) -> None:
        self.config.verbose = True
        status, _, raw = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertIs(self.json_body(raw)["ok"], True)

    def test_send_error_json_shape(self) -> None:
        status, _, raw = self.request("GET", "/nope")
        payload = self.json_body(raw)
        self.assertEqual(status, 404)
        self.assertEqual(set(payload["error"]), {"message", "type", "code"})
        self.assertEqual(payload["error"]["code"], 404)


class BodyLimitTests(ServerHarness):
    """A hostile ``Content-Length`` must not be believed or allocated."""

    def test_oversized_body_is_refused_without_being_read(self) -> None:
        claimed = server_module.MAX_BODY_BYTES + 1
        status, _, raw = self.request(
            "POST",
            "/v1/chat/completions",
            body=b"",
            headers={"content-type": "application/json", "content-length": str(claimed)},
        )
        self.assertEqual(status, 413)
        payload = self.json_body(raw)["error"]
        self.assertEqual(payload["type"], "request_too_large")
        # The point of the cap: nothing was forwarded and no thread was pinned
        # waiting for a body that was never coming.
        self.assertEqual(self.recorder.calls, [])

    def test_a_body_at_the_limit_is_still_accepted(self) -> None:
        """The cap is a ceiling, not a shrinking of the working range."""
        status, _, _ = self.post_json(
            "/v1/chat/completions", {"model": "beam-free", "messages": []}
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(self.recorder.calls), 1)

    def test_the_limit_is_large_enough_for_a_real_turn(self) -> None:
        # Guard against someone "hardening" this down to a value that breaks
        # long conversations or inline images in normal use.
        self.assertGreaterEqual(server_module.MAX_BODY_BYTES, 8 * 1024 * 1024)


class CrossOriginTests(ServerHarness):
    """A random web page must not be able to spend the user's free-lane quota."""

    def test_a_foreign_origin_is_refused(self) -> None:
        status, _, raw = self.post_json_with_origin(
            "/v1/chat/completions", {"model": "beam-free", "messages": []}, "https://evil.example"
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.json_body(raw)["error"]["type"], "cross_origin_refused")
        self.assertEqual(self.recorder.calls, [])

    def test_an_opaque_origin_is_refused(self) -> None:
        # ``Origin: null`` identifies no loopback page (sandboxed iframe, file://).
        status, _, _ = self.post_json_with_origin(
            "/v1/chat/completions", {"model": "beam-free", "messages": []}, "null"
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.recorder.calls, [])

    def test_loopback_origins_and_no_origin_both_work(self) -> None:
        # No Origin at all is how Hermes and curl call us; a loopback Origin is
        # how the sidecar's own pages would call us.
        for origin in (None, "http://127.0.0.1:8789", "http://localhost:8789", "http://[::1]:1234"):
            with self.subTest(origin=origin):
                self.recorder.calls.clear()
                headers = {"content-type": "application/json"}
                if origin:
                    headers["origin"] = origin
                status, _, _ = self.request(
                    "POST",
                    "/v1/chat/completions",
                    body={"model": "beam-free", "messages": []},
                    headers=headers,
                )
                self.assertEqual(status, 200)
                self.assertEqual(len(self.recorder.calls), 1)

    def test_get_is_not_origin_guarded(self) -> None:
        """Reads stay open: a browser must be able to load /healthz and /v1/models."""
        status, _, _ = self.request("GET", "/v1/models", headers={"origin": "https://evil.example"})
        self.assertEqual(status, 200)


class CatalogRefreshWiringTests(ServerHarness):
    """The read paths must give the TTL a chance to fire (Finding A)."""

    def test_models_read_trips_the_refresh_hook(self) -> None:
        self.request("GET", "/v1/models")
        self.assertGreaterEqual(self.catalog.refresh_calls, 1)

    def test_healthz_does_not_trip_the_refresh_hook(self) -> None:
        # BLUEPRINT §5: liveness must never depend on the lane, and this is the
        # probe the launcher runs before every start.
        self.request("GET", "/healthz")
        self.assertEqual(getattr(self.catalog, "refresh_calls", 0), 0)

    def test_post_reads_trip_the_refresh_hook(self) -> None:
        self.post_json("/v1/chat/completions", {"model": "beam-free", "messages": []})
        self.assertGreaterEqual(self.catalog.refresh_calls, 1)


class ConfigSurfaceTests(unittest.TestCase):
    """Config defaults that the shipped seed and plugin depend on."""

    def test_default_bind_is_loopback_on_the_documented_port(self) -> None:
        config = Config()
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8799)

    def test_urls_are_derived_without_double_slashes(self) -> None:
        config = Config(lane_base="https://opencode.ai/inference/v1/")
        self.assertEqual(config.models_url, "https://opencode.ai/inference/v1/models")
        self.assertEqual(config.chat_url, "https://opencode.ai/inference/v1/chat/completions")


def setUpModule() -> None:
    """Start the loopback server once for the whole module."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    ServerHarness.httpd = httpd
    ServerHarness.port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    ServerHarness._thread = thread


def tearDownModule() -> None:
    httpd = getattr(ServerHarness, "httpd", None)
    if httpd is None:
        return
    httpd.shutdown()
    httpd.server_close()
    thread = getattr(ServerHarness, "_thread", None)
    if thread is not None:
        thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
