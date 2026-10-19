#!/usr/bin/env python3
"""Prove the OpenCode Zen anonymous free lane actually answers, model by model.

This script talks to the **real** lane (``https://opencode.ai/inference/v1``) and
deliberately does **not** go through the sidecar: it is the end-to-end proof that
the recipe in ``docs/GATE.md`` still works today, from this host, with no API key.

Three things it encodes, learned the hard way (see ``docs/GATE.md``):

* **Streaming is mandatory.** A non-streaming request to the lane is *always*
  rejected with ``403 FreeTierError`` — it is not a weaker test, it is an invalid
  one. Every probe here sends ``stream: true`` and reads SSE.
* **``max_tokens`` 10 returns empty text for every model.** Reasoning models
  spend the whole budget on hidden tokens before emitting a visible one. The
  default is therefore 300, and ``--max-tokens`` below 100 prints a warning.
  An empty answer at a low budget is expected behaviour, not a broken model.
* **The gate is four rules.** ``user-agent`` begins ``opencode/``, the
  ``x-opencode-session`` header matches ``^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$``,
  the body has ``stream: true``, and the body's ``tools`` contains a function
  named exactly ``read``. The recipe is imported from ``sidecar.gate`` /
  ``sidecar.ids`` rather than copied, so this script verifies the *shipped*
  envelope.

Exit status: ``0`` when at least one model produced non-empty text, ``1``
otherwise. A ``403`` is reported honestly as a lane/quota fact — the lane is
rate-limited per egress IP (``docs/DESIGN.md`` §4.4) — and never as a bug in the
sidecar.

Usage:
    python3 scripts/verify_live.py
    python3 scripts/verify_live.py --model deepseek-v4.1-flash-free
    python3 scripts/verify_live.py --model big-pickle --model beam-free --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:  # the repo ships the recipe; a missing import is a broken checkout, not a lane fact
    from sidecar import ids
    from sidecar.gate import (
        DEFAULT_LANE_BASE,
        GATE_REQUIRED_TOOL,
        WIRE_UA,
        build_headers,
        shape_body,
    )
    from sidecar.upstream import explain_error
except ImportError as exc:  # pragma: no cover - only on a partial checkout
    print(
        f"ERROR: cannot import the sidecar package from {_REPO_ROOT!r}: {exc}\n"
        "       run this script from a checkout of opencodezen4hermes "
        "(the gate recipe lives in sidecar/gate.py).",
        file=sys.stderr,
    )
    raise SystemExit(1) from None

#: The nine free models, in picker order (docs/BLUEPRINT.md §3). Duplicated here
#: on purpose: if the roster ever drifts, this script and the contract disagree
#: loudly instead of quietly verifying a shorter list.
DEFAULT_MODELS: tuple[str, ...] = (
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

DEFAULT_PROMPT = "Reply with the single word: pong"
DEFAULT_MAX_TOKENS = 300

#: Below this, reasoning models answer with empty text by design (docs/GATE.md).
EMPTY_BELOW = 100

#: Statuses that mean "the lane refused this request", as opposed to a failure.
REJECT_STATUSES = frozenset({400, 401, 403, 404, 410, 422, 429})


@dataclass(slots=True)
class Result:
    """One model's outcome."""

    model: str
    status: int = 0
    verdict: str = "error"
    ttft_ms: float | None = None
    elapsed_ms: float | None = None
    text: str = ""
    text_chars: int = 0
    reasoning_chars: int = 0
    deltas: int = 0
    total_tokens: int | None = None
    completion_tokens: int | None = None
    error: str = ""
    attempts: int = 1

    @property
    def first_text(self) -> str:
        collapsed = " ".join(self.text.split())
        return collapsed[:48]


@dataclass(slots=True)
class Run:
    """Everything the run saw, ready for ``--json``."""

    base_url: str
    prompt: str
    max_tokens: int
    session_id: str
    request_id: str
    user_agent: str
    required_tool: str
    started_at: str = ""
    results: list[Result] = field(default_factory=list)

    def summary(self) -> dict[str, int | bool]:
        counts = {"works": 0, "empty": 0, "rejected": 0, "error": 0}
        for result in self.results:
            counts[result.verdict] = counts.get(result.verdict, 0) + 1
        return {
            "models": len(self.results),
            **counts,
            "ok": counts["works"] > 0,
        }


# --------------------------------------------------------------------------- #
# Wire
# --------------------------------------------------------------------------- #


def _text_of(value: Any) -> str:
    """Flatten a streamed ``content`` field (str, or a list of typed parts)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _reasoning_of(delta: dict[str, Any]) -> str:
    """Reasoning models leak their hidden tokens under a few different keys."""
    for key in ("reasoning_content", "reasoning", "thinking"):
        text = _text_of(delta.get(key))
        if text:
            return text
    return ""


def _consume(response: Any, result: Result, started: float) -> None:
    """Drain the SSE stream, filling ``result`` as tokens arrive.

    ``urlopen`` hands back an ``http.client.HTTPResponse``, which is line
    buffered: ``readline`` returns as soon as one SSE line lands, so ``ttft`` is
    a real time-to-first-token and not a time-to-full-response.
    """
    content_type = (response.headers.get("content-type") or "").lower()
    if "event-stream" not in content_type:
        # The lane answered with one JSON document. Handle it rather than
        # pretending the stream was empty.
        payload = response.read().decode("utf-8", "replace")
        result.elapsed_ms = (time.monotonic() - started) * 1000
        result.error = payload[:400]
        return

    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if not isinstance(chunk, dict):
            continue

        usage = chunk.get("usage")
        if isinstance(usage, dict):
            if isinstance(usage.get("total_tokens"), int):
                result.total_tokens = usage["total_tokens"]
            if isinstance(usage.get("completion_tokens"), int):
                result.completion_tokens = usage["completion_tokens"]

        for choice in chunk.get("choices") or ():
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                # A non-streaming-shaped chunk.
                delta = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            text = _text_of(delta.get("content"))
            reasoning = _reasoning_of(delta)
            if not text and not reasoning:
                continue
            if result.ttft_ms is None:
                result.ttft_ms = (time.monotonic() - started) * 1000
            result.deltas += 1
            result.text += text
            result.reasoning_chars += len(reasoning)

        if result.total_tokens is not None:
            # include_usage arrives last; keep draining for [DONE] but we
            # already have what we need.
            continue


def probe(
    *,
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
    session_id: str,
    request_id: str,
) -> Result:
    """Send one gate-shaped streaming completion and classify the outcome."""
    result = Result(model=model)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    # Shape with the shipped recipe: force stream, guarantee a tool named "read".
    shaped = shape_body(body, session_id=session_id, request_id=request_id)
    headers = build_headers(session_id, request_id)

    url = base_url.rstrip("/") + "/chat/completions"
    data = json.dumps(shaped).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    started = time.monotonic()

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result.status = int(getattr(response, "status", 200))
            _consume(response, result, started)
    except urllib.error.HTTPError as error:
        result.status = int(error.code)
        payload = b""
        try:
            payload = error.read() or b""
        except OSError:
            pass
        result.error = explain_error(result.status, payload)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        result.status = 0
        result.error = f"could not reach the lane: {getattr(error, 'reason', error)}"

    if result.elapsed_ms is None:
        result.elapsed_ms = (time.monotonic() - started) * 1000

    if result.status == 200:
        result.text_chars = len(result.text)
        result.verdict = "works" if result.text.strip() else "empty"
    elif result.status in REJECT_STATUSES:
        result.verdict = "rejected"
    else:
        result.verdict = "error"
    return result


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def _fmt_ms(value: float | None) -> str:
    return f"{value:.0f}ms" if value is not None else "-"


def _fmt_int(value: int | None) -> str:
    return str(value) if value is not None else "-"


def print_table(run: Run) -> None:
    rows = [
        (
            r.model,
            str(r.status) if r.status else "---",
            _fmt_ms(r.ttft_ms),
            _fmt_int(r.total_tokens),
            r.verdict,
            (r.first_text or ("(" + " ".join(r.error.split())[:44] + ")") if r.verdict != "works" else ""),
        )
        for r in run.results
    ]
    header = ("model", "http", "ttft", "tokens", "verdict", "first text")
    widths = [max(len(header[i]), *(len(row[i]) for row in rows)) for i in range(len(header))]
    line = "  ".join(h.ljust(w) for h, w in zip(header, widths)).rstrip()
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())


def print_report(run: Run, max_tokens: int) -> None:
    print()
    print(f"lane      : {run.base_url}")
    print(f"session   : {run.session_id}  (request {run.request_id})")
    print(f"user-agent: {run.user_agent}")
    print(f"recipe    : stream=true, tools contain a function named {run.required_tool!r}")
    print(f"prompt    : {run.prompt!r}  max_tokens={max_tokens}")
    print()
    print_table(run)

    notes: list[str] = []
    if max_tokens < EMPTY_BELOW:
        notes.append(
            f"max_tokens={max_tokens} is below {EMPTY_BELOW}: empty answers are "
            "expected here, reasoning models spend the budget on hidden tokens. "
            "Re-run with the default 300."
        )
    rejected = [r for r in run.results if r.verdict == "rejected"]
    if rejected:
        codes = sorted({r.status for r in rejected})
        notes.append(
            f"{len(rejected)} model(s) rejected with HTTP {codes}: this is the "
            "lane's own answer. A 403 is almost always egress-IP or quota "
            "(docs/DESIGN.md 4.4), not a broken build."
        )
    empty = [r for r in run.results if r.verdict == "empty"]
    if empty and max_tokens >= EMPTY_BELOW:
        notes.append(
            f"{len(empty)} model(s) returned 200 with no visible text at "
            f"max_tokens={max_tokens}; raise --max-tokens to see them answer."
        )
    if notes:
        print()
        for note in notes:
            print(f"note: {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_live.py",
        description=(
            "Send one gate-shaped STREAMING chat completion per free model to the "
            "real OpenCode Zen anonymous lane and report what came back."
        ),
        epilog=(
            "Notes:\n"
            "  * Streaming is mandatory: a non-streaming request to this lane is\n"
            "    always rejected with 403 FreeTierError, so this script always\n"
            "    streams (docs/GATE.md).\n"
            "  * Keep --max-tokens at the default 300. Reasoning models emit no\n"
            "    visible text at max_tokens 10 (or 32) because the budget is\n"
            "    spent on hidden tokens; empty output there is expected, not a\n"
            "    failing model.\n"
            "  * The lane is rate-limited per egress IP. A 403 usually means\n"
            "    quota or a datacenter IP, not a bug.\n"
            "  * Exit status is 0 when at least one model produced non-empty\n"
            "    text, 1 otherwise.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--model",
        action="append",
        metavar="ID",
        help="model id to probe; repeatable (default: the nine free models)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        metavar="N",
        help=(
            f"completion budget per request (default {DEFAULT_MAX_TOKENS}). "
            "Do not go below 100: reasoning models return EMPTY text at 10, "
            "because they spend the whole budget on hidden tokens"
        ),
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="user message to send")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENCODEZEN4HERMES_LANE_BASE") or DEFAULT_LANE_BASE,
        help=f"lane base URL (default {DEFAULT_LANE_BASE}, or $OPENCODEZEN4HERMES_LANE_BASE)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="socket timeout per request (default 120)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="pause between models, to stay under the per-IP rate limit (default 1.0)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        metavar="N",
        help="extra attempts after a 403/429, with backoff (default 1)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    models = args.model or list(DEFAULT_MODELS)
    session_id = ids.generate_session_id()
    request_id = ids.generate_request_id()

    run = Run(
        base_url=args.base_url,
        prompt=args.prompt,
        max_tokens=args.max_tokens,
        session_id=session_id,
        request_id=request_id,
        user_agent=WIRE_UA,
        required_tool=GATE_REQUIRED_TOOL,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    if not args.json:
        print(f"probing {len(models)} model(s) on {args.base_url} (streaming, max_tokens={args.max_tokens})")

    for index, model in enumerate(models):
        attempt = 0
        while True:
            attempt += 1
            result = probe(
                base_url=args.base_url,
                model=model,
                prompt=args.prompt,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
                session_id=session_id,
                request_id=request_id,
            )
            result.attempts = attempt
            if result.status not in {403, 429} or attempt > max(1, args.retries):
                break
            # 403 is usually quota rather than a burst limit, but one short
            # backoff costs little and rides out the per-IP window.
            time.sleep(min(2.0 * attempt, 5.0))
        run.results.append(result)
        if not args.json:
            marker = {"works": "ok  ", "empty": "empty", "rejected": "deny", "error": "err"}[result.verdict]
            detail = result.first_text if result.verdict == "works" else " ".join(result.error.split())[:60]
            print(
                f"  [{marker}] {model:32s} {str(result.status):>3s}  "
                f"ttft={_fmt_ms(result.ttft_ms):>7s}  {detail}"
            )
        if args.delay and index < len(models) - 1:
            time.sleep(args.delay)

    summary = run.summary()

    if args.json:
        payload = {
            "schema": 1,
            "base_url": run.base_url,
            "started_at": run.started_at,
            "prompt": run.prompt,
            "max_tokens": run.max_tokens,
            "recipe": {
                "user_agent": run.user_agent,
                "x_opencode_session": run.session_id,
                "stream": True,
                "required_tool": run.required_tool,
            },
            "results": [
                {
                    "model": r.model,
                    "status": r.status,
                    "verdict": r.verdict,
                    "ttft_ms": round(r.ttft_ms, 1) if r.ttft_ms is not None else None,
                    "elapsed_ms": round(r.elapsed_ms, 1) if r.elapsed_ms is not None else None,
                    "text": r.text,
                    "text_chars": r.text_chars,
                    "reasoning_chars": r.reasoning_chars,
                    "deltas": r.deltas,
                    "total_tokens": r.total_tokens,
                    "completion_tokens": r.completion_tokens,
                    "error": r.error,
                    "attempts": r.attempts,
                }
                for r in run.results
            ],
            "summary": summary,
        }
        print(json.dumps(payload, indent=2))
        return 0 if summary["ok"] else 1

    print_report(run, args.max_tokens)
    print()
    print(
        f"summary: {summary['models']} probed, {summary['works']} works, "
        f"{summary['empty']} empty, {summary['rejected']} rejected, {summary['error']} error "
        f"-> {'PASS' if summary['ok'] else 'FAIL'}"
    )
    if not summary["ok"]:
        print(
            "hint: not one model answered with text. Check the network path first, "
            "then read docs/GATE.md — and remember a non-streaming probe proves nothing."
        )
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
