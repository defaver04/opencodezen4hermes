"""Tests for :mod:`sidecar.gate` — request shaping and the wire header set.

The gate is documented in docs/GATE.md: exactly four things are enforced, and
three of them are the sidecar's job. What these tests lock down is the shaping
policy that makes the port *usable*, not merely accepted:

* ``stream`` is forced on — the lane 403s a non-streaming body outright;
* a tool named exactly ``read`` is present — the lane 403s without one;
* the caller's own tools survive **verbatim and in order**, because Hermes
  calls ``read_file``/``write_file``/``bash`` and rewriting them to the
  official CLI set would break every agent tool call;
* the caller's system prompt and messages are not touched (docs/GATE.md, "the
  official system prompt is optional ... we pass it through untouched").

Stdlib only, no network (BLUEPRINT §8).
"""

from __future__ import annotations

import copy
import json
import re
import unittest

from sidecar import gate
from sidecar.ids import CANONICAL_SESSION_PATTERN

SESSION_RE = re.compile(CANONICAL_SESSION_PATTERN)

#: Hermes-named tools, i.e. the names that must survive untouched.
HERMES_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    },
]


def _base_body() -> dict:
    return {
        "model": "deepseek-v4.1-flash-free",
        "messages": [
            {"role": "system", "content": "You are a Hermes agent."},
            {"role": "user", "content": "hello"},
        ],
        "temperature": 0.2,
        "max_tokens": 300,
    }


def _tool_names(tools) -> list:
    names = []
    for tool in tools:
        if not isinstance(tool, dict):
            names.append(tool)
            continue
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        names.append(name if name is not None else tool.get("name"))
    return names


class ConstantsTest(unittest.TestCase):
    def test_required_tool_is_exactly_read(self) -> None:
        self.assertEqual(gate.GATE_REQUIRED_TOOL, "read")

    def test_fallback_tool_uses_the_chat_completions_shape(self) -> None:
        fallback = gate.GATE_TOOL_FALLBACK
        self.assertEqual(fallback["type"], "function")
        self.assertEqual(fallback["function"]["name"], "read")
        self.assertIsInstance(fallback["function"]["parameters"], dict)

    def test_wire_user_agent_starts_with_opencode_slash(self) -> None:
        self.assertTrue(gate.WIRE_UA.startswith("opencode/"))
        # The documented full UA from BLUEPRINT §2.
        self.assertEqual(
            gate.WIRE_UA,
            "opencode/1.19.3 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14",
        )

    def test_default_lane_base_is_the_inference_path(self) -> None:
        # BLUEPRINT §2: upstream lane base. The old /zen/v1 path is gone.
        self.assertEqual(gate.DEFAULT_LANE_BASE, "https://opencode.ai/inference/v1")
        self.assertNotIn("/zen/", gate.DEFAULT_LANE_BASE)

    def test_official_tool_fixture_has_the_eleven_cli_tools(self) -> None:
        names = gate.gate_tool_names()
        self.assertEqual(len(names), 11)
        self.assertEqual(
            names,
            [
                "bash",
                "edit",
                "glob",
                "grep",
                "read",
                "skill",
                "task",
                "todowrite",
                "webfetch",
                "websearch",
                "write",
            ],
        )

    def test_official_chat_tools_are_chat_shaped(self) -> None:
        tools = gate.official_chat_tools()
        self.assertEqual(len(tools), 11)
        for tool in tools:
            with self.subTest(tool=tool):
                self.assertEqual(tool["type"], "function")
                self.assertIsInstance(tool["function"]["name"], str)
                self.assertTrue(tool["function"]["name"])
                self.assertIn("parameters", tool["function"])
                # The flat fixture shape must not leak through.
                self.assertNotIn("name", tool)


class StreamForcingTest(unittest.TestCase):
    def test_stream_false_is_forced_true(self) -> None:
        # docs/GATE.md: stream:false and stream:absent both 403.
        shaped = gate.shape_body(
            {"model": "m", "stream": False}, session_id="ses_x", request_id="msg_x"
        )
        self.assertIs(shaped["stream"], True)

    def test_absent_stream_is_added_true(self) -> None:
        shaped = gate.shape_body({"model": "m"}, session_id="ses_x", request_id="msg_x")
        self.assertIs(shaped["stream"], True)

    def test_stream_true_stays_true(self) -> None:
        shaped = gate.shape_body(
            {"model": "m", "stream": True}, session_id="ses_x", request_id="msg_x"
        )
        self.assertIs(shaped["stream"], True)

    def test_stream_options_added_only_when_absent(self) -> None:
        shaped = gate.shape_body({"model": "m"}, session_id="ses_x", request_id="msg_x")
        self.assertEqual(shaped["stream_options"], {"include_usage": True})

    def test_existing_stream_options_are_preserved(self) -> None:
        # setdefault, not assignment: a caller that opted out must be honoured.
        shaped = gate.shape_body(
            {"model": "m", "stream_options": {"include_usage": False}},
            session_id="ses_x",
            request_id="msg_x",
        )
        self.assertEqual(shaped["stream_options"], {"include_usage": False})

    def test_empty_stream_options_are_preserved(self) -> None:
        shaped = gate.shape_body(
            {"model": "m", "stream_options": {}}, session_id="ses_x", request_id="msg_x"
        )
        self.assertEqual(shaped["stream_options"], {})


class ToolInjectionTest(unittest.TestCase):
    def test_read_is_injected_when_tools_absent(self) -> None:
        shaped = gate.shape_body({"model": "m"}, session_id="ses_x", request_id="msg_x")
        self.assertEqual(_tool_names(shaped["tools"]), ["read"])

    def test_read_is_injected_when_tools_empty(self) -> None:
        shaped = gate.shape_body(
            {"model": "m", "tools": []}, session_id="ses_x", request_id="msg_x"
        )
        self.assertEqual(_tool_names(shaped["tools"]), ["read"])

    def test_read_is_injected_when_no_read_present(self) -> None:
        shaped = gate.shape_body(
            {"model": "m", "tools": copy.deepcopy(HERMES_TOOLS)},
            session_id="ses_x",
            request_id="msg_x",
        )
        self.assertEqual(_tool_names(shaped["tools"]), ["read_file", "write_file", "read"])

    def test_read_is_not_duplicated_when_already_present(self) -> None:
        shaped = gate.shape_body(
            {"model": "m", "tools": copy.deepcopy(HERMES_TOOLS) + [copy.deepcopy(gate.GATE_TOOL_FALLBACK)]},
            session_id="ses_x",
            request_id="msg_x",
        )
        self.assertEqual(_tool_names(shaped["tools"]).count("read"), 1)

    def test_existing_read_in_chat_shape_is_detected(self) -> None:
        caller_read = {
            "type": "function",
            "function": {"name": "read", "parameters": {"type": "object", "properties": {}}},
        }
        shaped = gate.shape_body(
            {"model": "m", "tools": [caller_read]}, session_id="ses_x", request_id="msg_x"
        )
        self.assertEqual(len(shaped["tools"]), 1)
        # Not replaced by the fallback: the caller's schema wins.
        self.assertIs(shaped["tools"][0], caller_read)

    def test_existing_read_in_flat_responses_shape_is_detected(self) -> None:
        # The sidecar proxies both chat and Responses shapes, so the detector
        # tolerates {type,name} as well as {type,function:{name}}.
        flat_read = {"type": "function", "name": "read", "parameters": {}}
        shaped = gate.shape_body(
            {"model": "m", "tools": [flat_read]}, session_id="ses_x", request_id="msg_x"
        )
        self.assertEqual(len(shaped["tools"]), 1)
        self.assertIs(shaped["tools"][0], flat_read)

    def test_near_miss_names_do_not_count_as_read(self) -> None:
        # docs/GATE.md: read_file, Read, READ, read_ all 403 upstream.
        for near_miss in ("read_file", "Read", "READ", "read_", "read ", " read"):
            with self.subTest(name=near_miss):
                shaped = gate.shape_body(
                    {"model": "m", "tools": [{"type": "function", "function": {"name": near_miss}}]},
                    session_id="ses_x",
                    request_id="msg_x",
                )
                self.assertEqual(_tool_names(shaped["tools"]), [near_miss, "read"])

    def test_injected_tool_matches_the_fallback_constant(self) -> None:
        shaped = gate.shape_body({"model": "m"}, session_id="ses_x", request_id="msg_x")
        self.assertEqual(shaped["tools"][0], gate.GATE_TOOL_FALLBACK)

    def test_non_list_tools_is_replaced_rather_than_crashing(self) -> None:
        for bogus in ("nope", 42, {}, None):
            with self.subTest(bogus=bogus):
                shaped = gate.shape_body(
                    {"model": "m", "tools": bogus}, session_id="ses_x", request_id="msg_x"
                )
                self.assertEqual(_tool_names(shaped["tools"]), ["read"])

    def test_junk_tool_entries_are_tolerated(self) -> None:
        shaped = gate.shape_body(
            {"model": "m", "tools": [None, "junk", 7, {"no_name": True}, {"function": "not-a-dict"}]},
            session_id="ses_x",
            request_id="msg_x",
        )
        names = _tool_names(shaped["tools"])
        self.assertEqual(names[-1], "read")
        self.assertEqual(len(names), 6)


class ToolPreservationTest(unittest.TestCase):
    def test_caller_tool_names_and_order_survive(self) -> None:
        # The single most important property of this port: Hermes tools are
        # advertised by their real names, so tool calls come back callable.
        shaped = gate.shape_body(
            {"model": "m", "tools": copy.deepcopy(HERMES_TOOLS)},
            session_id="ses_x",
            request_id="msg_x",
        )
        self.assertEqual(_tool_names(shaped["tools"])[:2], ["read_file", "write_file"])

    def test_caller_tool_objects_are_preserved_verbatim(self) -> None:
        caller_tools = copy.deepcopy(HERMES_TOOLS)
        shaped = gate.shape_body(
            {"model": "m", "tools": copy.deepcopy(caller_tools)},
            session_id="ses_x",
            request_id="msg_x",
        )
        self.assertEqual(shaped["tools"][:2], caller_tools)
        self.assertEqual(shaped["tools"][0]["function"]["description"], "Read a file from the workspace.")

    def test_official_eleven_tool_set_is_not_imposed(self) -> None:
        # The upstream ensureFreeLaneShape rewrote the list to the official 11;
        # doing that here would destroy the Hermes names.
        shaped = gate.shape_body(
            {"model": "m", "tools": copy.deepcopy(HERMES_TOOLS)},
            session_id="ses_x",
            request_id="msg_x",
        )
        names = _tool_names(shaped["tools"])
        self.assertEqual(len(names), 3)
        self.assertNotEqual(sorted(names), sorted(gate.gate_tool_names()))

    def test_caller_tool_choice_is_preserved(self) -> None:
        shaped = gate.shape_body(
            {"model": "m", "tool_choice": "required"}, session_id="ses_x", request_id="msg_x"
        )
        self.assertEqual(shaped["tool_choice"], "required")

    def test_tool_choice_defaults_to_auto(self) -> None:
        shaped = gate.shape_body({"model": "m"}, session_id="ses_x", request_id="msg_x")
        self.assertEqual(shaped["tool_choice"], "auto")


class NonMutationTest(unittest.TestCase):
    def test_input_body_is_not_mutated(self) -> None:
        body = _base_body()
        body["tools"] = copy.deepcopy(HERMES_TOOLS)
        snapshot = copy.deepcopy(body)
        gate.shape_body(body, session_id="ses_x", request_id="msg_x")
        self.assertEqual(body, snapshot)
        self.assertNotIn("stream", body)
        self.assertNotIn("tool_choice", body)
        self.assertNotIn("stream_options", body)

    def test_writing_a_top_level_key_does_not_touch_the_input(self) -> None:
        body = _base_body()
        shaped = gate.shape_body(body, session_id="ses_x", request_id="msg_x")
        shaped["model"] = "changed"
        shaped["temperature"] = 0.9
        self.assertEqual(body["model"], "deepseek-v4.1-flash-free")
        self.assertEqual(body["temperature"], 0.2)

    def test_shaping_never_mutates_nested_message_or_tool_data(self) -> None:
        # ``shape_body`` copies one level (``dict(body)``) and then only ever
        # rebinds top-level keys, so nested values are shared by reference but
        # never written to. That is the property the no-mutation docstring
        # promises; a caller that mutates the shaped copy's messages itself is
        # outside the contract.
        body = _base_body()
        body["tools"] = copy.deepcopy(HERMES_TOOLS)
        messages_before = copy.deepcopy(body["messages"])
        tools_before = copy.deepcopy(body["tools"])
        shaped = gate.shape_body(body, session_id="ses_x", request_id="msg_x")
        self.assertEqual(shaped["messages"], messages_before)
        self.assertEqual(shaped["tools"][:2], tools_before)

    def test_caller_tool_list_is_not_aliased(self) -> None:
        body = {"model": "m", "tools": copy.deepcopy(HERMES_TOOLS)}
        shaped = gate.shape_body(body, session_id="ses_x", request_id="msg_x")
        self.assertIsNot(shaped["tools"], body["tools"])
        self.assertEqual(len(body["tools"]), 2)  # no injected read leaked back


class MessagePreservationTest(unittest.TestCase):
    def test_system_prompt_passes_through_untouched(self) -> None:
        # docs/GATE.md: the 9.5 KB official prompt is optional; Hermes sends its
        # own and we must not replace or prepend to it.
        body = _base_body()
        shaped = gate.shape_body(body, session_id="ses_x", request_id="msg_x")
        self.assertEqual(shaped["messages"], body["messages"])
        self.assertEqual(shaped["messages"][0]["content"], "You are a Hermes agent.")

    def test_no_system_message_is_injected(self) -> None:
        shaped = gate.shape_body(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            session_id="ses_x",
            request_id="msg_x",
        )
        self.assertEqual(len(shaped["messages"]), 1)
        self.assertEqual(shaped["messages"][0]["role"], "user")

    def test_missing_messages_is_not_invented(self) -> None:
        shaped = gate.shape_body({"model": "m"}, session_id="ses_x", request_id="msg_x")
        self.assertNotIn("messages", shaped)

    def test_other_sampling_parameters_survive(self) -> None:
        body = _base_body()
        shaped = gate.shape_body(body, session_id="ses_x", request_id="msg_x")
        for key in ("model", "temperature", "max_tokens"):
            with self.subTest(key=key):
                self.assertEqual(shaped[key], body[key])


class HeadersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session_id = "ses_" + "0123456789ab" + "b" * 14
        self.request_id = "msg_" + "0123456789ab" + "c" * 14
        self.headers = gate.build_headers(self.session_id, self.request_id)

    def test_user_agent_begins_with_opencode_slash(self) -> None:
        ua = self.headers["user-agent"]
        self.assertTrue(ua.startswith("opencode/"))
        self.assertNotIn("Mozilla", ua)

    def test_user_agent_parses_as_name_slash_version(self) -> None:
        # docs/GATE.md: the lane looks like a real name/version parse plus a
        # version floor, so the leading token must be opencode/<semver>.
        self.assertRegex(self.headers["user-agent"], r"^opencode/\d+\.\d+\.\d+")

    def test_session_header_matches_the_canonical_pattern(self) -> None:
        self.assertRegex(self.headers["x-opencode-session"], SESSION_RE)
        self.assertEqual(self.headers["x-opencode-session"], self.session_id)

    def test_request_header_carries_the_msg_id(self) -> None:
        self.assertEqual(self.headers["x-opencode-request"], self.request_id)

    def test_content_type_and_accept(self) -> None:
        self.assertEqual(self.headers["content-type"], "application/json")
        self.assertEqual(self.headers["accept"], "text/event-stream")

    def test_authorization_defaults_to_the_public_sentinel(self) -> None:
        # BLUEPRINT §7: api_key "public" is a sentinel, but a non-empty route
        # field is what api/config.py needs, so a Bearer is always sent.
        self.assertEqual(self.headers["authorization"], "Bearer public")

    def test_explicit_api_key_is_used_when_supplied(self) -> None:
        headers = gate.build_headers(self.session_id, self.request_id, api_key="sk-test")
        self.assertEqual(headers["authorization"], "Bearer sk-test")

    def test_optional_opencode_headers_are_sent(self) -> None:
        # Not required by the gate, but sent so traffic is indistinguishable
        # from the real CLI.
        self.assertEqual(self.headers["x-opencode-client"], "cli")
        self.assertEqual(self.headers["x-opencode-project"], "global")

    def test_header_names_are_lowercase_for_http_clients(self) -> None:
        for name in self.headers:
            with self.subTest(name=name):
                self.assertEqual(name, name.lower())

    def test_headers_are_all_strings(self) -> None:
        for name, value in self.headers.items():
            with self.subTest(name=name):
                self.assertIsInstance(value, str)

    def test_headers_are_json_safe(self) -> None:
        # urllib rejects a non-ASCII header value at request time; catching it
        # here beats catching it in the middle of a live stream.
        json.dumps(self.headers)
        for value in self.headers.values():
            with self.subTest(value=value):
                self.assertEqual(value, value.encode("ascii", "ignore").decode())


class EndToEndShapingTest(unittest.TestCase):
    def test_shaped_body_satisfies_all_four_gate_rules(self) -> None:
        shaped = gate.shape_body(
            {"model": "m", "stream": False, "messages": [{"role": "user", "content": "x"}]},
            session_id="ses_x",
            request_id="msg_x",
        )
        headers = gate.build_headers(
            "ses_" + "0123456789ab" + "b" * 14, "msg_x"
        )
        # 1. UA prefix
        self.assertTrue(headers["user-agent"].startswith("opencode/"))
        # 2. canonical session
        self.assertRegex(headers["x-opencode-session"], SESSION_RE)
        # 3. stream true
        self.assertIs(shaped["stream"], True)
        # 4. a tool named exactly read
        self.assertIn("read", _tool_names(shaped["tools"]))

    def test_shaped_body_is_json_serialisable(self) -> None:
        shaped = gate.shape_body(_base_body(), session_id="ses_x", request_id="msg_x")
        encoded = json.dumps(shaped)
        self.assertIn('"stream": true', encoded)

    def test_shaping_is_idempotent(self) -> None:
        once = gate.shape_body(_base_body(), session_id="ses_x", request_id="msg_x")
        twice = gate.shape_body(once, session_id="ses_x", request_id="msg_x")
        self.assertEqual(_tool_names(once["tools"]).count("read"), 1)
        self.assertEqual(_tool_names(twice["tools"]).count("read"), 1)
        self.assertEqual(once["stream_options"], twice["stream_options"])

    def test_full_hermes_agent_turn_keeps_every_tool(self) -> None:
        hermes_names = ["read_file", "write_file", "bash", "grep", "glob", "web_search"]
        tools = [
            {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
            for name in hermes_names
        ]
        shaped = gate.shape_body(
            {
                "model": "big-pickle",
                "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
                "tools": tools,
                "stream": True,
            },
            session_id="ses_x",
            request_id="msg_x",
        )
        names = _tool_names(shaped["tools"])
        self.assertEqual(names[: len(hermes_names)], hermes_names)
        self.assertEqual(names[-1], "read")


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
