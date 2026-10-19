"""Gate constants and request shaping for the Zen anonymous lane.

Decoded live on 2026-10-20 — see ``docs/GATE.md`` for the probe matrix. The lane
enforces exactly four things:

1. ``user-agent`` starts with ``opencode/``
2. ``x-opencode-session`` matches ``^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$``
3. ``stream`` is ``true``
4. ``tools`` contains a function named exactly ``read``

The official 11-tool set, the 9.5 KB official system prompt, ``tool_choice``,
``x-opencode-client``, ``x-opencode-request`` and ``x-opencode-project`` are all
optional. We send the full documented envelope anyway: it is free, it keeps us
aligned if upstream re-tightens, and it makes our traffic indistinguishable from
the real CLI for debugging.
"""

from __future__ import annotations

import json
import os
from typing import Any

#: Base URL of the anonymous lane. The upstream plugin migrated from
#: ``https://opencode.ai/zen/v1`` to ``/inference/v1`` in 0.4.0-fork.6.
DEFAULT_LANE_BASE = "https://opencode.ai/inference/v1"

#: Full wire UA as captured from opencode 1.19.3. Only the ``opencode/<semver>``
#: prefix is actually validated (``opencode/1`` is rejected, ``opencode/1.19.3``
#: passes), so this is the belt-and-braces form.
WIRE_UA = "opencode/1.19.3 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14"

#: The one tool name the gate actually requires.
GATE_REQUIRED_TOOL = "read"

#: Minimal schema used when the caller's tool list has no ``read`` entry and we
#: have to inject one. Descriptions and schemas are not compared by the gate.
GATE_TOOL_FALLBACK: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": GATE_REQUIRED_TOOL,
        "description": "Read a file from the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to read."},
            },
            "required": ["file_path"],
        },
    },
}


def gate_tool_names() -> list[str]:
    """Names of the 11 official CLI tools, loaded from the captured fixture."""
    return [t["name"] for t in _official_tools_flat()]


def _official_tools_flat() -> list[dict[str, Any]]:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate", "official_tools.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def official_chat_tools() -> list[dict[str, Any]]:
    """The 11 official tools in chat-completions shape.

    Not required by the gate any more, but still sent as the *tail* of the tool
    list so a model that was trained on the CLI framing behaves normally.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in _official_tools_flat()
    ]


def _has_gate_tool(tools: Any) -> bool:
    for tool in tools or ():
        if not isinstance(tool, dict):
            continue
        # Tolerate both chat shape ({type,function:{name}}) and the flat
        # Responses shape ({type,name}), since we proxy for both callers.
        function = tool.get("function")
        name = (function or {}).get("name") if isinstance(function, dict) else None
        name = name or tool.get("name")
        if name == GATE_REQUIRED_TOOL:
            return True
    return False


def build_headers(session_id: str, request_id: str, *, api_key: str | None = None) -> dict[str, str]:
    """Headers for one upstream call.

    Every header except ``user-agent`` and ``x-opencode-session`` is optional per
    the probe matrix; we still send them.
    """
    return {
        "authorization": f"Bearer {api_key or 'public'}",
        "content-type": "application/json",
        "accept": "text/event-stream",
        "user-agent": WIRE_UA,
        "x-opencode-client": "cli",
        "x-opencode-session": session_id,
        "x-opencode-request": request_id,
        "x-opencode-project": "global",
    }


def shape_body(body: dict[str, Any], *, session_id: str, request_id: str) -> dict[str, Any]:
    """Return ``body`` rewritten to satisfy the gate, without mutating the input.

    Deliberately minimal. We do **not** replace the caller's system prompt and we
    do **not** clobber the caller's tool list: the gate only needs one tool named
    ``read``, so Hermes tool names survive and the agent keeps working.

    ``stream`` is forced on, because a non-streaming request is rejected
    outright and the caller may not know that.
    """
    shaped = dict(body)

    # 1. Streaming is mandatory.
    shaped["stream"] = True
    # Keep include_usage when we are the ones adding stream_options; it costs
    # nothing and some clients (the WebUI token meter) want the usage frame.
    shaped.setdefault("stream_options", {"include_usage": True})

    # 2. The tool list must contain a function named exactly "read". Anything
    #    else the caller sent is preserved in order, so Hermes tool names pass
    #    through to the model unchanged.
    tools = shaped.get("tools")
    if not isinstance(tools, list):
        tools = []
    if not _has_gate_tool(tools):
        tools = [*tools, GATE_TOOL_FALLBACK]
    shaped["tools"] = tools

    # 3. tool_choice is not validated; "auto" is the CLI's value and the only
    #    sane one when we advertise a tool set we cannot force.
    shaped.setdefault("tool_choice", "auto")

    return shaped
