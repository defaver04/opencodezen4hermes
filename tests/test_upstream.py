"""Tests for :mod:`sidecar.upstream` — the POST + SSE passthrough layer.

Standard library only (docs/BLUEPRINT.md §8). Nothing here opens a socket: the
one network-touching function is exercised by replacing ``urllib.request.urlopen``
with a fake for the duration of a test, so no bytes leave the process.
"""

from __future__ import annotations

import io
import json
import socket
import unittest
import urllib.error
import urllib.request

from sidecar import upstream
from sidecar.upstream import UpstreamError, UpstreamResponse, explain_error, post_stream


def _json_error(error_type: str, message: str, status: int = 403) -> bytes:
    return json.dumps({"error": {"type": error_type, "message": message}}).encode("utf-8")


class ExplainErrorTests(unittest.TestCase):
    """A 403 from the lane means three different things; the message must say which."""

    def test_free_tier_error_explains_egress_ip_quota(self) -> None:
        text = explain_error(403, _json_error("FreeTierError", "request rejected"))
        # The lane's own words survive...
        self.assertIn("request rejected", text)
        # ...and we add the actionable part: this is a quota/IP limit, not a bug.
        self.assertIn("not coming from OpenCode", text)
        self.assertIn("rate-limited per source IP", text)
        self.assertIn("OPENCODEZEN4HERMES_USER_AGENT", text)

    def test_protocol_unsupported_says_reasoning_model(self) -> None:
        text = explain_error(400, _json_error("ModelProtocolUnsupported", "bad shape", status=400))
        self.assertIn("bad shape", text)
        self.assertIn("reasoning-only model", text)
        self.assertIn("different free model", text)

    def test_deprecated_and_unavailable_hints(self) -> None:
        deprecated = explain_error(410, _json_error("ModelDeprecated", "gone", status=410))
        self.assertIn("delisted upstream", deprecated)
        unavailable = explain_error(503, _json_error("ModelUnavailable", "down", status=503))
        self.assertIn("temporarily unavailable", unavailable)

    def test_region_block_overrides_the_type_hint(self) -> None:
        # A region block can arrive under any error type; the message wins.
        body = _json_error("FreeTierError", "This model is not available in your country")
        text = explain_error(403, body)
        self.assertIn("region-blocked", text)
        self.assertNotIn("not coming from OpenCode", text)

    def test_word_region_also_triggers_region_hint(self) -> None:
        text = explain_error(403, _json_error("Other", "blocked by region policy"))
        self.assertIn("region-blocked", text)

    def test_429_and_rate_wording_produce_rate_limit_hint(self) -> None:
        by_status = explain_error(429, _json_error("Other", "slow down", status=429))
        self.assertIn("Rate-limited by the anonymous lane", by_status)
        by_wording = explain_error(403, _json_error("Other", "rate limit exceeded"))
        self.assertIn("Rate-limited by the anonymous lane", by_wording)

    def test_non_json_body_is_returned_truncated_not_raised(self) -> None:
        body = b"<html>502 Bad Gateway</html>"
        text = explain_error(502, body)
        self.assertEqual(text, "<html>502 Bad Gateway</html>")

    def test_long_non_json_body_is_truncated_to_400_characters(self) -> None:
        body = ("x" * 1000).encode("utf-8")
        text = explain_error(500, body)
        self.assertEqual(len(text), 400)
        self.assertEqual(text, "x" * 400)

    def test_empty_body_falls_back_to_the_status(self) -> None:
        self.assertEqual(explain_error(503, b""), "HTTP 503")

    def test_flat_message_shape_is_understood(self) -> None:
        body = json.dumps({"message": "no capacity"}).encode("utf-8")
        self.assertEqual(explain_error(500, body), "no capacity")

    def test_undecodable_bytes_do_not_raise(self) -> None:
        text = explain_error(400, b"\xff\xfe\x00bad")
        self.assertIsInstance(text, str)

    def test_json_that_is_a_list_is_tolerated(self) -> None:
        text = explain_error(400, b'["a", "b"]')
        self.assertIsInstance(text, str)


class UpstreamErrorTests(unittest.TestCase):
    def test_carries_status_message_and_payload(self) -> None:
        error = UpstreamError(403, "denied by gate", b'{"detail": true}')
        self.assertEqual(error.status, 403)
        self.assertEqual(error.message, "denied by gate")
        self.assertEqual(error.payload, b'{"detail": true}')
        self.assertEqual(str(error), "denied by gate")

    def test_payload_defaults_to_empty_bytes(self) -> None:
        error = UpstreamError(504, "timed out")
        self.assertEqual(error.payload, b"")
        self.assertIsInstance(error, Exception)


class PostStreamTests(unittest.TestCase):
    """post_stream is driven through a fake urlopen — no sockets, no network."""

    def setUp(self) -> None:
        self._real_urlopen = urllib.request.urlopen

    def tearDown(self) -> None:
        urllib.request.urlopen = self._real_urlopen

    def _install(self, behaviour) -> None:
        urllib.request.urlopen = behaviour

    def test_success_wraps_status_headers_and_bytes(self) -> None:
        closed: list[bool] = []

        class _FakeResponse:
            status = 200

            def __init__(self) -> None:
                self.headers = {"Content-Type": "text/event-stream", "X-Unused": "1"}
                self._chunks = [b"data: a\n\n", b"data: [DONE]\n\n"]

            def __iter__(self):
                return iter(self._chunks)

            def close(self) -> None:
                closed.append(True)

        def fake_urlopen(request, timeout=None):
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.full_url, "https://lane.invalid/v1/chat/completions")
            self.assertEqual(json.loads(request.data), {"model": "beam-free", "stream": True})
            self.assertEqual(request.get_header("User-agent"), "opencode/1.19.3")
            return _FakeResponse()

        # The fake response iterates and records close(), which is legal.
        self._install(fake_urlopen)
        result = post_stream(
            "https://lane.invalid/v1/chat/completions",
            {"user-agent": "opencode/1.19.3", "content-type": "application/json"},
            {"model": "beam-free", "stream": True},
        )
        self.assertIsInstance(result, UpstreamResponse)
        self.assertEqual(result.status, 200)
        # Header names are lowercased so lookups are case-insensitive downstream.
        self.assertEqual(result.headers["content-type"], "text/event-stream")
        self.assertEqual(list(result.body), [b"data: a\n\n", b"data: [DONE]\n\n"])
        result.close()
        self.assertEqual(closed, [True])

    def test_response_close_delegates_to_body_close(self) -> None:
        closed = []

        class _Body:
            def __iter__(self):
                return iter(())

            def close(self) -> None:
                closed.append(True)

        response = UpstreamResponse(status=200, headers={}, body=_Body())
        response.close()
        self.assertEqual(closed, [True])

    def test_http_error_becomes_upstream_error_with_lane_explanation(self) -> None:
        payload = _json_error("FreeTierError", "quota exceeded for this IP")

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 403, "Forbidden", {}, io.BytesIO(payload)
            )

        self._install(fake_urlopen)
        with self.assertRaises(UpstreamError) as caught:
            post_stream("https://lane.invalid/v1/chat/completions", {}, {"model": "x"})
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("quota exceeded for this IP", caught.exception.message)
        self.assertIn("rate-limited per source IP", caught.exception.message)
        self.assertEqual(caught.exception.payload, payload)

    def test_http_error_status_is_preserved_for_4xx(self) -> None:
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {}, io.BytesIO(b"no such model")
            )

        self._install(fake_urlopen)
        with self.assertRaises(UpstreamError) as caught:
            post_stream("https://lane.invalid/v1/chat/completions", {}, {"model": "x"})
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.message, "no such model")

    def test_timeout_url_error_becomes_504(self) -> None:
        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError(socket.timeout("timed out"))

        self._install(fake_urlopen)
        with self.assertRaises(UpstreamError) as caught:
            post_stream("https://lane.invalid/v1/chat/completions", {}, {"model": "x"})
        self.assertEqual(caught.exception.status, 504)
        self.assertIn("Timed out talking to the Zen lane", caught.exception.message)

    def test_plain_timeout_exception_becomes_504(self) -> None:
        def fake_urlopen(request, timeout=None):
            raise TimeoutError("read timed out")

        self._install(fake_urlopen)
        with self.assertRaises(UpstreamError) as caught:
            post_stream("https://lane.invalid/v1/chat/completions", {}, {"model": "x"})
        self.assertEqual(caught.exception.status, 504)

    def test_unreachable_lane_becomes_502(self) -> None:
        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

        self._install(fake_urlopen)
        with self.assertRaises(UpstreamError) as caught:
            post_stream("https://lane.invalid/v1/chat/completions", {}, {"model": "x"})
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("Could not reach the Zen lane", caught.exception.message)

    def test_body_is_sent_as_serialised_json(self) -> None:
        seen = {}

        class _FakeResponse:
            status = 200
            headers: dict[str, str] = {}

            def __iter__(self):
                return iter(())

        def fake_urlopen(request, timeout=None):
            seen["data"] = request.data
            seen["timeout"] = timeout
            return _FakeResponse()

        self._install(fake_urlopen)
        post_stream(
            "https://lane.invalid/v1/chat/completions",
            {},
            {"model": "beam-free", "messages": [{"role": "user", "content": "héllo"}]},
            timeout=42.0,
            connect_timeout=3.0,
        )
        self.assertEqual(json.loads(seen["data"]), {
            "model": "beam-free",
            "messages": [{"role": "user", "content": "héllo"}],
        })
        # urllib takes one timeout; post_stream uses the larger of the two.
        self.assertEqual(seen["timeout"], 42.0)


class ModuleSurfaceTests(unittest.TestCase):
    def test_error_hints_cover_the_documented_types(self) -> None:
        self.assertEqual(
            set(upstream._ERROR_HINTS),
            {"FreeTierError", "ModelProtocolUnsupported", "ModelDeprecated", "ModelUnavailable"},
        )


if __name__ == "__main__":
    unittest.main()
