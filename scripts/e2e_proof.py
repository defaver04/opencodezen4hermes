#!/usr/bin/env python3
"""Prove that every model in the picker actually answers *through the provider route*.

``scripts/verify_live.py`` proves the upstream lane answers when we talk to it
directly. This script proves the other half, the one the user feels: that a model
listed in the Hermes WebUI model menu answers when Hermes talks to the
``opencode-zen-free`` provider — i.e. through the sidecar, over the OpenAI
``/v1/chat/completions`` contract that ``providers/base.py`` speaks.

The difference matters. verify_live.py could pass while the shipped provider
profile pointed at the wrong port, or while the sidecar rejected a model the
picker still listed. This script takes ``base_url`` **from the plugin profile
itself** (imported with a stubbed ``providers`` package, so the value provably
comes from the shipped code and not from a copy-paste), then exercises that
endpoint exactly as the Hermes openai client would.

What it checks
--------------
1. ``base_url`` resolves from ``plugin/opencode-zen-free/__init__.py``.
2. ``GET {base_url}/models`` lists the nine contract ids.
3. Every one of the nine answers a chat completion with non-empty text. The
   caller asks for ``stream: true`` and the deltas are reassembled here — which
   is exactly the guarantee the sidecar sells, since the upstream lane refuses
   non-streaming requests outright.
4. One realistic agentic turn with real Hermes tool schemas: the model must emit
   a ``tool_call`` whose ``function.name`` is a Hermes tool name. This is the
   claim that no tool bridge is needed, and it is the one most likely to rot.

Constraints encoded here (learned the hard way, see ``docs/GATE.md``)
--------------------------------------------------------------------
* ``max_tokens`` must be >= 300. At 10 every model returns empty text because
  the reasoning models spend the whole budget on hidden tokens.
* The anonymous lane is rate-limited per egress IP, so the nine calls are paced
  and each one retries once on 403/429 with a short backoff before being
  recorded as a rejection.

Exit status: 0 only when every check above passed. Anything else is 1, so this
can gate a release.

Usage:
    python3 scripts/e2e_proof.py
    python3 scripts/e2e_proof.py --json
    python3 scripts/e2e_proof.py --skip-agentic --max-tokens 400
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import types
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN_PATH = os.path.join(
    _REPO_ROOT, "plugin", "opencode-zen-free", "__init__.py"
)

#: The nine free models, in picker order (docs/BLUEPRINT.md §3). Duplicated on
#: purpose: if the roster drifts, this script and the contract disagree loudly
#: instead of quietly verifying a shorter list.
CONTRACT_MODELS: tuple[str, ...] = (
    "beam-free",
    "big-pickle",
    "deepseek-v4.1-flash-free",
    "glm-5.3-flash-free",
    "longcat-2.5-preview-free",
    "mimo-v2.6-flash-free",
    "minimax-m3.1-flash-free",
    "nemotron-3.5-lightning-free",
    "nemotron-3.5-super-vl-free",
)

#: Real Hermes tool names, verified against hermes-agent
#: ``tools/file_tools.py`` (``"name": "read_file"`` at :1133, ``"name":
#: "write_file"`` at :1155, ``"name": "search_files"`` at :1270) and
#: ``tools/shell_tools.py`` (``bash``). The gate inside the sidecar only ever
#: needs a tool named ``read``; anything else here is a name Hermes must receive
#: back verbatim.
HERMES_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace and return its contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path of the file to read."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text to a file in the workspace, replacing it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path of the file to write."},
                    "content": {"type": "string", "description": "Full file contents."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search the workspace for a regular expression.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression to search for."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command and return its output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to execute."},
                },
                "required": ["command"],
            },
        },
    },
)

_HERMES_TOOL_NAMES = frozenset(t["function"]["name"] for t in HERMES_TOOLS)

AGENTIC_PROMPT = (
    "Read the file /etc/hostname using the read_file tool. "
    "Call the tool now; do not explain."
)


# -- plugin profile import --------------------------------------------------


def load_plugin_profile() -> Any:
    """Import the shipped plugin module with a stubbed ``providers`` package.

    ``plugin/opencode-zen-free/__init__.py`` does ``from providers import
    register_provider`` and ``from providers.base import ProviderProfile``, which
    only exist inside a hermes-agent tree. Stubbing them is how we can read the
    shipped ``base_url`` without installing Hermes — and it means the value under
    test is the one in the file, not one retyped here.
    """
    stub = types.ModuleType("providers")
    registered: list[Any] = []
    stub.register_provider = registered.append  # type: ignore[attr-defined]

    base_stub = types.ModuleType("providers.base")

    class ProviderProfile:  # noqa: D101 - stub mirroring the real field set
        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

        def fetch_models(self, **_kwargs: Any) -> list[str] | None:  # noqa: D102
            return None

    base_stub.ProviderProfile = ProviderProfile  # type: ignore[attr-defined]
    stub.base = base_stub  # type: ignore[attr-defined]

    saved_modules = {
        name: sys.modules.get(name) for name in ("providers", "providers.base")
    }
    sys.modules["providers"] = stub
    sys.modules["providers.base"] = base_stub
    try:
        spec = importlib.util.spec_from_file_location(
            "opencodezen4hermes_plugin_profile", _PLUGIN_PATH
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load plugin profile from {_PLUGIN_PATH!r}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, previous in saved_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    if not registered:
        raise RuntimeError(f"{_PLUGIN_PATH} did not call providers.register_provider()")
    return registered[-1]


# -- HTTP helpers -----------------------------------------------------------


@dataclass(slots=True)
class _Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def _open(request: urllib.request.Request, timeout: float) -> _Response:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _Response(
                status=getattr(response, "status", 200),
                body=response.read(),
                headers={k.lower(): v for k, v in response.headers.items()},
            )
    except urllib.error.HTTPError as error:
        return _Response(
            status=error.code,
            body=error.read() or b"",
            headers={k.lower(): v for k, v in (error.headers or {}).items()},
        )


def _is_retryable(status: int) -> bool:
    return status in (403, 429, 502, 503, 504)


def post_completion(
    base_url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    retries: int = 1,
    backoff: float = 1.5,
) -> _Response:
    """POST to the provider route, retrying the lane's soft rejections once."""
    url = base_url.rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer public",
        "user-agent": "hermes-cli/opencodezen4hermes",
    }
    attempt = 0
    while True:
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        response = _open(request, timeout)
        if response.status == 200 or attempt >= retries or not _is_retryable(response.status):
            return response
        attempt += 1
        time.sleep(backoff)
        # Small jitter so nine paced retries do not line up on the same instant.
        time.sleep(0.1 * attempt)


def parse_sse_text(raw: bytes) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    """Reassemble an OpenAI SSE stream into text + tool calls + usage.

    This is what the sidecar guarantees to a non-streaming caller: the deltas are
    ordinary OpenAI chunks, so concatenating ``choices[0].delta.content`` is the
    whole contract.
    """
    text_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] | None = None

    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if not isinstance(chunk, dict):
            continue
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or ():
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
            for call in delta.get("tool_calls") or ():
                if not isinstance(call, dict):
                    continue
                index = call.get("index", 0)
                slot = tool_calls.setdefault(
                    index, {"name": "", "arguments": "", "id": ""}
                )
                if call.get("id"):
                    slot["id"] = call["id"]
                function = call.get("function") or {}
                if function.get("name"):
                    slot["name"] = function["name"]
                if function.get("arguments"):
                    slot["arguments"] += function["arguments"]
    return "".join(text_parts), list(tool_calls.values()), usage


# -- checks -----------------------------------------------------------------


@dataclass(slots=True)
class ModelResult:
    model: str
    status: int
    verdict: str
    ttft_ms: int
    text: str
    tokens: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "status": self.status,
            "verdict": self.verdict,
            "ttft_ms": self.ttft_ms,
            "text": self.text[:120],
            "tokens": self.tokens,
            "note": self.note,
        }


def check_models_endpoint(base_url: str, *, timeout: float) -> tuple[bool, list[str], str]:
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(
        url,
        headers={
            "authorization": "Bearer public",
            "user-agent": "hermes-cli/opencodezen4hermes",
            "accept": "application/json",
        },
    )
    response = _open(request, timeout)
    if response.status != 200:
        return False, [], f"GET {url} -> HTTP {response.status}: {response.body[:200]!r}"
    try:
        payload = json.loads(response.body)
    except ValueError as error:
        return False, [], f"GET {url} returned non-JSON: {error}"
    items = payload.get("data") if isinstance(payload, dict) else None
    ids = [
        item["id"]
        for item in (items or ())
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    missing = [m for m in CONTRACT_MODELS if m not in ids]
    if missing:
        return False, ids, f"missing from /v1/models: {', '.join(missing)}"
    return True, ids, ""


def probe_model(
    base_url: str,
    model: str,
    *,
    max_tokens: int,
    timeout: float,
    tools: tuple[dict[str, Any], ...] = HERMES_TOOLS,
) -> ModelResult:
    payload: dict[str, Any] = {
        "model": model,
        "stream": True,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": 'Reply with the single word: pong'}],
    }
    if tools:
        payload["tools"] = list(tools)
        payload["tool_choice"] = "auto"

    started = time.monotonic()
    response = post_completion(base_url, payload, timeout=timeout)
    if response.status != 200:
        body = response.body.decode("utf-8", "replace").strip()
        return ModelResult(
            model=model,
            status=response.status,
            verdict="rejected" if response.status in (403, 429) else "error",
            ttft_ms=int((time.monotonic() - started) * 1000),
            text="",
            tokens=0,
            note=body[:200],
        )
    ttft = int((time.monotonic() - started) * 1000)
    text, _calls, usage = parse_sse_text(response.body)
    tokens = 0
    if isinstance(usage, dict):
        tokens = int(usage.get("total_tokens") or 0)
    cleaned = text.strip()
    if not cleaned:
        return ModelResult(
            model=model,
            status=200,
            verdict="empty",
            ttft_ms=ttft,
            text="",
            tokens=tokens,
            note="200 but no visible text — raise --max-tokens (reasoning models)",
        )
    return ModelResult(
        model=model,
        status=200,
        verdict="works",
        ttft_ms=ttft,
        text=cleaned,
        tokens=tokens,
    )


def agentic_turn(base_url: str, model: str, *, max_tokens: int, timeout: float) -> dict[str, Any]:
    """One realistic tool-using turn. The point is the tool NAME that comes back."""
    payload = {
        "model": model,
        "stream": True,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": AGENTIC_PROMPT}],
        "tools": list(HERMES_TOOLS),
        "tool_choice": "auto",
    }
    response = post_completion(base_url, payload, timeout=timeout, retries=1)
    if response.status != 200:
        return {
            "ok": False,
            "model": model,
            "status": response.status,
            "note": response.body.decode("utf-8", "replace")[:200],
        }
    text, calls, _usage = parse_sse_text(response.body)
    names = [c.get("name") for c in calls if c.get("name")]
    hermes_names = [n for n in names if n in _HERMES_TOOL_NAMES]
    return {
        "ok": bool(hermes_names),
        "model": model,
        "status": response.status,
        "tool_calls": names,
        "hermes_tool_calls": hermes_names,
        "arguments": [c.get("arguments", "")[:160] for c in calls],
        "text": text.strip()[:200],
        "note": "" if hermes_names else "model emitted no Hermes-named tool_call",
    }


# -- presentation -----------------------------------------------------------


def _print_table(results: list[ModelResult]) -> None:
    print(
        f"{'model':<30}{'http':<6}{'ttft':<9}{'tokens':<8}{'verdict':<11}text"
    )
    print("-" * 100)
    for item in results:
        first = (item.text or "").replace("\n", " ")[:34]
        print(
            f"{item.model:<30}{item.status:<6}{item.ttft_ms:>5}ms  "
            f"{item.tokens:<8}{item.verdict:<11}{first}"
        )
        if item.note and item.verdict != "works":
            print(f"{'':<30}note: {item.note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="e2e_proof.py",
        description=(
            "Prove every picker-visible model answers through the "
            "opencode-zen-free provider route (the sidecar), the way Hermes "
            "talks to it. Complements scripts/verify_live.py, which probes the "
            "upstream lane directly."
        ),
        epilog=(
            "max_tokens must be >= 300: at 10 every model returns empty text "
            "because reasoning models spend the budget on hidden tokens. The "
            "nine calls are paced because the anonymous lane is rate-limited "
            "per egress IP."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="provider base URL; default is read from the shipped plugin profile",
    )
    parser.add_argument("--max-tokens", type=int, default=300, help="default 300 (see epilog)")
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request timeout (s)")
    parser.add_argument(
        "--agentic-model",
        default="deepseek-v4.1-flash-free",
        help="model used for the tool-calling turn (default deepseek-v4.1-flash-free)",
    )
    parser.add_argument("--skip-agentic", action="store_true", help="skip the tool-calling turn")
    parser.add_argument(
        "--pace",
        type=float,
        default=1.0,
        help="seconds to sleep between models (default 1.0; the lane is per-IP limited)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.max_tokens < 100:
        print(
            "WARNING: --max-tokens below 100 usually yields empty text for every "
            "model (reasoning models spend the budget on hidden tokens).",
            file=sys.stderr,
        )

    try:
        profile = load_plugin_profile()
    except Exception as error:  # noqa: BLE001 - report, do not traceback at the user
        print(f"FAIL cannot load the plugin profile: {error}", file=sys.stderr)
        return 1

    base_url = args.base_url or getattr(profile, "base_url", None)
    if not base_url:
        print("FAIL the plugin profile exposes no base_url", file=sys.stderr)
        return 1

    fallback = tuple(getattr(profile, "fallback_models", ()) or ())
    if fallback != CONTRACT_MODELS:
        print(
            "WARNING: plugin fallback_models differs from docs/BLUEPRINT.md §3\n"
            f"  plugin   : {list(fallback)}\n"
            f"  contract : {list(CONTRACT_MODELS)}",
            file=sys.stderr,
        )

    if not args.json:
        print(f"provider : {getattr(profile, 'display_name', profile.name)}")
        print(f"slug     : {profile.name}")
        print(f"base_url : {base_url}")
        print(f"max_tokens: {args.max_tokens}")
        print()

    ok_models, listed, problem = check_models_endpoint(base_url, timeout=args.timeout)
    if not ok_models:
        print(f"FAIL /v1/models check: {problem}", file=sys.stderr)
        return 1
    if not args.json:
        print(f"/v1/models: {len(listed)} model(s), all nine contract ids present")
        print()

    results: list[ModelResult] = []
    for index, model in enumerate(CONTRACT_MODELS):
        if index and args.pace:
            time.sleep(args.pace)
        results.append(
            probe_model(
                base_url,
                model,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
            )
        )

    agentic: dict[str, Any] | None = None
    if not args.skip_agentic:
        if args.pace:
            time.sleep(args.pace)
        agentic = agentic_turn(
            base_url,
            args.agentic_model,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
        )

    works = [r for r in results if r.verdict == "works"]
    failures = [r for r in results if r.verdict != "works"]
    agentic_ok = bool(agentic and agentic.get("ok"))

    if args.json:
        print(
            json.dumps(
                {
                    "base_url": base_url,
                    "slug": getattr(profile, "name", "opencode-zen-free"),
                    "models_endpoint": {"ok": ok_models, "listed": listed},
                    "models": [r.to_dict() for r in results],
                    "agentic": agentic,
                    "summary": {
                        "probed": len(results),
                        "works": len(works),
                        "failed": len(failures),
                        "agentic_ok": agentic_ok,
                    },
                },
                indent=2,
            )
        )
    else:
        _print_table(results)
        print()
        if agentic is not None:
            print(f"agentic turn — model: {agentic.get('model')}  http: {agentic.get('status')}")
            if agentic.get("ok"):
                print(
                    f"  tool_calls       : {agentic.get('tool_calls')}\n"
                    f"  Hermes-named     : {agentic.get('hermes_tool_calls')}\n"
                    f"  arguments sample : {agentic.get('arguments')}"
                )
            else:
                print(f"  FAILED           : {agentic.get('note')}")
            print()
        print(
            f"summary: {len(results)} probed, {len(works)} works, "
            f"{sum(1 for r in results if r.verdict == 'empty')} empty, "
            f"{sum(1 for r in results if r.verdict == 'rejected')} rejected, "
            f"{sum(1 for r in results if r.verdict == 'error')} error"
            f" | agentic tool call: {'ok' if agentic_ok else 'failed'}"
            f" -> {'PASS' if not failures and agentic_ok else 'FAIL'}"
        )

    return 0 if not failures and agentic_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
