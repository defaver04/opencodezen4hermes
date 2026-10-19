"""Timestamp-valid OpenCode session/request ids.

Port of ``opencodezen4dsh/packages/plugin/src/adapter/ids.ts``. The Zen free lane
rejects a ``ses_`` id whose first 12 hex characters are not a plausible current
timestamp: a conversation-seed hash yields a 1970 timestamp and a
``403 FreeTierError``. This module therefore *never* derives the id from the
conversation; it mints a fresh timestamp-valid id and caches one per
conversation so prompt-cache affinity survives.

Wire shape: ``ses_`` + 12 lowercase hex + 14 Base62 = ``^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$``.
"""

from __future__ import annotations

import secrets
import threading
import time

BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

#: Canonical session shape the lane requires (ids.ts CANONICAL_SESSION_PATTERN).
CANONICAL_SESSION_PATTERN = r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$"

_LOCK = threading.Lock()
_last_ms = 0
_per_ms_counter = 0
_session_cache: dict[str, str] = {}

#: Upper bound on the per-process conversation cache. A long-lived WebUI session
#: can talk to many conversations; the cache is only an affinity aid, so evict
#: rather than grow without bound.
_MAX_CACHED_SESSIONS = 512


def _tu_suffix(descending: bool, now_ms: int) -> str:
    """``tU()`` from the official CLI (``ids.ts tUSuffix``).

    ``base = now * 0x1000 + counter`` so four ids can never collide inside one
    millisecond. ``ses_`` uses the bitwise complement (descending, matching the
    CLI's ``oU()``) and ``msg_`` the raw value (ascending, ``u_()``).
    """
    global _last_ms, _per_ms_counter
    with _LOCK:
        if now_ms != _last_ms:
            _last_ms = now_ms
            _per_ms_counter = 0
        _per_ms_counter += 1
        counter = _per_ms_counter

    base = now_ms * 0x1000 + counter
    wired = ~base if descending else base
    # Six big-endian bytes = the 12 hex characters the server validates.
    time_part = "".join(f"{(wired >> (40 - 8 * i)) & 0xFF:02x}" for i in range(6))
    random_part = "".join(BASE62_ALPHABET[b % 62] for b in secrets.token_bytes(14))
    return time_part + random_part


def generate_session_id(now_ms: int | None = None) -> str:
    """Fresh timestamp-valid session id (``ses_`` + descending tU)."""
    return "ses_" + _tu_suffix(True, int(time.time() * 1000) if now_ms is None else now_ms)


def generate_request_id(now_ms: int | None = None) -> str:
    """Fresh timestamp-valid request id (``msg_`` + ascending tU).

    The prefix is ``msg_`` and not ``req_``: on the wire ``x-opencode-request``
    carries the CLI's user message id (``msg_<26>``).
    """
    return "msg_" + _tu_suffix(False, int(time.time() * 1000) if now_ms is None else now_ms)


def canonical_session_id(signal: str, now_ms: int | None = None) -> str:
    """Map an arbitrary conversation signal to one timestamp-valid session id.

    A signal already shaped like a real session id passes through untouched so
    upstream prompt-cache affinity is preserved. Anything else gets one freshly
    minted id, cached per signal.
    """
    import re

    if signal and re.match(CANONICAL_SESSION_PATTERN, signal):
        return signal
    with _LOCK:
        cached = _session_cache.get(signal)
        if cached is not None:
            return cached
    fresh = generate_session_id(now_ms)
    with _LOCK:
        if len(_session_cache) >= _MAX_CACHED_SESSIONS:
            # Drop the oldest insertion; affinity for a stale conversation is
            # worth less than a bounded cache.
            _session_cache.pop(next(iter(_session_cache)), None)
        _session_cache[signal] = fresh
    return fresh


def conversation_signal(messages: list[dict]) -> str:
    """Stable key for the conversation: JSON of its first user turn.

    Using the first user message keeps a multi-turn conversation stable as its
    history grows while separating conversations with different openings
    (``ids.ts conversationSeed``).
    """
    import json

    for message in messages or ():
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        encoded = json.dumps(message.get("content"), sort_keys=True)
        if encoded and encoded != "null":
            return encoded
    return ""


def reset_state() -> None:
    """Clear the id counter + session cache (tests only)."""
    global _last_ms, _per_ms_counter
    with _LOCK:
        _last_ms = 0
        _per_ms_counter = 0
        _session_cache.clear()
