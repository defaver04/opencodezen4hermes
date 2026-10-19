"""Upstream POST + SSE passthrough.

The lane only speaks SSE (``stream:true`` is mandatory), so there is no
non-streaming code path to write. What we do have to get right:

* **No buffering.** The WebUI renders tokens as they arrive; holding a chunk
  until the next one lands is the difference between "streaming" and a spinner.
* **No re-encoding.** The bytes are already the OpenAI chunk shape Hermes
  expects, so they are copied through untouched. Re-serialising JSON here would
  only add latency and risk.
* **Honest errors.** A gate rejection and a quota exhaustion both surface as
  HTTP 403 with different bodies, and the user needs to know which one happened.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

logger = logging.getLogger(__name__)

#: Bodies the lane returns that mean something more specific than "403".
_ERROR_HINTS = {
    "FreeTierError": (
        "The Zen free lane rejected this request as not coming from OpenCode. "
        "Most often this is a quota or egress-IP limit rather than a bug: the "
        "anonymous lane is rate-limited per source IP. Retry later, or set "
        "OPENCODEZEN4HERMES_USER_AGENT to match your installed CLI version."
    ),
    "ModelProtocolUnsupported": (
        "This model rejects the chat-completions request shape (it is a "
        "reasoning-only model). Pick a different free model."
    ),
    "ModelDeprecated": "This model has been delisted upstream. Refresh the model list.",
    "ModelUnavailable": "Upstream reports the model temporarily unavailable. Retry shortly.",
}


@dataclass(slots=True)
class UpstreamResponse:
    status: int
    headers: dict[str, str]
    body: Iterator[bytes]

    def close(self) -> None:
        closer = getattr(self.body, "close", None)
        if callable(closer):
            closer()


def explain_error(status: int, payload: bytes) -> str:
    """Turn a lane error body into an actionable message."""
    text = payload.decode("utf-8", "replace").strip()
    error_type = ""
    message = ""
    try:
        parsed = json.loads(text)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            error_type = str(error.get("type") or "")
            message = str(error.get("message") or "")
        elif isinstance(parsed, dict):
            message = str(parsed.get("message") or "")
    except ValueError:
        message = text[:400]

    hint = _ERROR_HINTS.get(error_type)
    if "region" in message.lower() or "not available in your country" in message.lower():
        hint = "This model is region-blocked from your network. Try another free model."
    if "rate" in message.lower() or status == 429:
        hint = "Rate-limited by the anonymous lane. Wait a moment and retry."

    detail = message or text[:400] or f"HTTP {status}"
    if hint:
        return f"{detail}\n\n{hint}"
    return detail


def post_stream(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    timeout: float = 300.0,
    connect_timeout: float = 15.0,
) -> UpstreamResponse:
    """POST a streaming chat completion and hand back an iterator of raw bytes."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        response = urllib.request.urlopen(request, timeout=max(connect_timeout, timeout))
    except urllib.error.HTTPError as error:
        # Read the body here so the caller gets the lane's own explanation
        # rather than a bare urllib message.
        payload = error.read() or b""
        raise UpstreamError(error.code, explain_error(error.code, payload), payload) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, socket.timeout) or isinstance(error, (socket.timeout, TimeoutError)):
            raise UpstreamError(504, f"Timed out talking to the Zen lane: {reason}") from None
        raise UpstreamError(502, f"Could not reach the Zen lane: {reason}") from None

    return UpstreamResponse(
        status=getattr(response, "status", 200),
        headers={k.lower(): v for k, v in response.headers.items()},
        body=response,
    )


class UpstreamError(Exception):
    """An upstream failure already translated into a user-facing message."""

    def __init__(self, status: int, message: str, payload: bytes = b"") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload
