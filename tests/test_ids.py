"""Tests for :mod:`sidecar.ids` — the session/request id wire shapes.

What is actually load-bearing here (docs/GATE.md, "x-opencode-session must be
canonical"):

* the string matches ``^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$`` — 30 characters,
  nothing else checked by the lane;
* the 12 hex characters decode to a *plausible timestamp*, which no longer
  gates the lane (a 1970 timestamp passes) but would survive a re-tightening;
* ids are distinct per call, while :func:`sidecar.ids.canonical_session_id`
  is stable per conversation so upstream prompt-cache affinity survives.

Stdlib only, no network (BLUEPRINT §8).
"""

from __future__ import annotations

import re
import unittest

from sidecar import ids

#: From BLUEPRINT §2 / docs/GATE.md — the full wire shape the lane accepts.
SESSION_RE = re.compile(ids.CANONICAL_SESSION_PATTERN)
BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

#: A fixed, realistic epoch millisecond (2023-11-14T22:13:20Z) so assertions
#: about the time half are deterministic.
FIXED_MS = 1_700_000_000_000


class SessionShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        ids.reset_state()

    def test_generated_session_matches_canonical_pattern(self) -> None:
        session_id = ids.generate_session_id()
        self.assertRegex(session_id, SESSION_RE)
        self.assertTrue(SESSION_RE.match(session_id))

    def test_generated_session_is_exactly_30_characters(self) -> None:
        session_id = ids.generate_session_id()
        self.assertEqual(len(session_id), 30)
        self.assertEqual(len("ses_"), 4)
        self.assertEqual(len(session_id) - len("ses_"), 26)

    def test_time_half_is_lowercase_hex_and_suffix_is_base62_only(self) -> None:
        session_id = ids.generate_session_id(FIXED_MS)
        time_part = session_id[4:16]
        random_part = session_id[16:]
        self.assertTrue(all(char in "0123456789abcdef" for char in time_part), time_part)
        self.assertEqual(len(time_part), 12)
        self.assertTrue(all(char in BASE62 for char in random_part), random_part)
        self.assertEqual(len(random_part), 14)

    def test_uppercase_hex_would_be_rejected_by_the_pattern(self) -> None:
        # Guard against someone "simplifying" the encoder to hexdigest() on a
        # string that is later upper()ed: the pattern demands lowercase hex.
        self.assertIsNone(SESSION_RE.match("ses_ABCDEF012345" + "b" * 14))
        self.assertIsNotNone(SESSION_RE.match("ses_abcdef012345" + "b" * 14))

    def test_rejects_non_canonical_shapes_from_the_probe_matrix(self) -> None:
        # docs/GATE.md: ses_zzz -> 403, absent -> 403.
        for bad in (
            "ses_zzz",
            "",
            "ses_" + "g" * 26,             # 'g' is not hex
            "ses_" + "a" * 12 + "b" * 13,  # one char short
            "ses_" + "a" * 12 + "b" * 15,  # one char long
            "ses" + "a" * 12 + "b" * 14,   # missing underscore
            "msg_" + "a" * 12 + "b" * 14,  # wrong prefix
            "ses_" + "a" * 12 + "b" * 13 + "+",  # '+' is not Base62
            "ses_" + "a" * 12 + "b" * 13 + "_",  # '_' is not Base62
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(SESSION_RE.match(bad))


class Base62AlphabetTest(unittest.TestCase):
    def test_alphabet_is_exactly_the_documented_base62(self) -> None:
        self.assertEqual(ids.BASE62_ALPHABET, BASE62)
        self.assertEqual(len(ids.BASE62_ALPHABET), 62)
        self.assertEqual(len(set(ids.BASE62_ALPHABET)), 62)

    def test_alphabet_is_digits_upper_lower_in_that_order(self) -> None:
        # The order matters: it is the official CLI's table (ids.ts), and a
        # "sorted" alphabet would silently change generated ids.
        self.assertEqual(ids.BASE62_ALPHABET[:10], "0123456789")
        self.assertEqual(ids.BASE62_ALPHABET[10:36], "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        self.assertEqual(ids.BASE62_ALPHABET[36:], "abcdefghijklmnopqrstuvwxyz")


class TimestampHalfTest(unittest.TestCase):
    """The 12 hex characters must decode back to a plausible timestamp."""

    def setUp(self) -> None:
        ids.reset_state()

    def _decode_base(self, encoded_id: str) -> int:
        """Read the 12 hex characters of an id as a 48-bit integer."""
        return int(encoded_id[4:16], 16)

    def test_session_time_half_is_the_bitwise_complement_of_the_packed_base(self) -> None:
        # _tu_suffix(descending=True) wires ~base. Getting the complement
        # backwards still matches the regex, which is exactly why the packing
        # is asserted rather than assumed.
        for now_ms in (0, 1000, FIXED_MS):
            with self.subTest(now_ms=now_ms):
                ids.reset_state()
                encoded = ids.generate_session_id(now_ms)
                base = ~self._decode_base(encoded) & 0xFFFFFFFFFFFF
                self.assertEqual(base, (now_ms * 0x1000 + 1) & 0xFFFFFFFFFFFF)

    def test_request_time_half_is_the_raw_packed_base(self) -> None:
        for now_ms in (0, 1000, FIXED_MS):
            with self.subTest(now_ms=now_ms):
                ids.reset_state()
                base = self._decode_base(ids.generate_request_id(now_ms))
                self.assertEqual(base, (now_ms * 0x1000 + 1) & 0xFFFFFFFFFFFF)

    def test_short_timestamps_round_trip_exactly(self) -> None:
        # Below 2**36 ms (~795 days after the epoch) the packing fits in the
        # 48 bits available, so the time half decodes back to the input.
        for now_ms in (0, 1, 1000, 2**35):
            with self.subTest(now_ms=now_ms):
                ids.reset_state()
                base = ~self._decode_base(ids.generate_session_id(now_ms)) & 0xFFFFFFFFFFFF
                self.assertEqual(base >> 12, now_ms)
                self.assertEqual(base & 0xFFF, 1)

    def test_current_epoch_values_wrap_within_the_48_bit_field(self) -> None:
        # The wire field is 12 hex characters = 48 bits, but
        # ``now_ms * 0x1000 + counter`` needs ~53 bits for a 2020s timestamp,
        # so the high bits wrap every 2**36 ms (~795 days). The lane does not
        # validate the timestamp (docs/GATE.md: a 1970 value and ``a``*12 both
        # pass), so this is pinned as the encoder's real contract, not asserted
        # as a plausible wall clock.
        ids.reset_state()
        base = ~self._decode_base(ids.generate_session_id(FIXED_MS)) & 0xFFFFFFFFFFFF
        self.assertEqual(base >> 12, FIXED_MS % 2**36)
        self.assertNotEqual(base >> 12, FIXED_MS)

    def test_zero_timestamp_encodes_to_the_complement_of_the_counter(self) -> None:
        # docs/GATE.md: "ses_ + 0x12 + b*14 (1970 timestamp)" passes the lane.
        # With now_ms=0 the packed base is just the 1-based counter, so the
        # descending session half is its complement.
        ids.reset_state()
        self.assertEqual(ids.generate_session_id(0)[4:16], "f" * 11 + "e")
        ids.reset_state()
        self.assertEqual(ids.generate_request_id(0)[4:16], "0" * 11 + "1")

    def test_counter_increments_inside_one_millisecond(self) -> None:
        first = ids.generate_session_id(FIXED_MS)
        second = ids.generate_session_id(FIXED_MS)
        self.assertEqual(int(first[4:16], 16) - 1, int(second[4:16], 16))
        self.assertEqual(len(first), len(second))

    def test_reset_state_restarts_the_counter(self) -> None:
        first = ids.generate_session_id(FIXED_MS)
        ids.reset_state()
        again = ids.generate_session_id(FIXED_MS)
        self.assertEqual(first[4:16], again[4:16])


class RequestIdTest(unittest.TestCase):
    def setUp(self) -> None:
        ids.reset_state()

    def test_request_id_starts_with_msg_underscore(self) -> None:
        request_id = ids.generate_request_id()
        self.assertTrue(request_id.startswith("msg_"))
        # Not "req_": on the wire x-opencode-request carries the CLI's user
        # message id (msg_<26>), per sidecar/ids.py.
        self.assertFalse(request_id.startswith("req_"))

    def test_request_id_is_msg_plus_26_characters(self) -> None:
        request_id = ids.generate_request_id()
        self.assertEqual(len(request_id), 30)
        self.assertRegex(request_id, r"^msg_[0-9a-f]{12}[0-9A-Za-z]{14}$")

    def test_request_id_is_not_a_session_id(self) -> None:
        self.assertIsNone(SESSION_RE.match(ids.generate_request_id()))


class UniquenessTest(unittest.TestCase):
    def setUp(self) -> None:
        ids.reset_state()

    def test_two_consecutive_sessions_differ(self) -> None:
        first = ids.generate_session_id()
        second = ids.generate_session_id()
        self.assertNotEqual(first, second)
        # Same-millisecond calls only differ in the counter half here, but the
        # random suffix makes collisions practically impossible either way.
        self.assertEqual(len(first), len(second))

    def test_two_consecutive_requests_differ(self) -> None:
        self.assertNotEqual(ids.generate_request_id(), ids.generate_request_id())

    def test_bulk_sessions_are_all_distinct_and_canonical(self) -> None:
        produced = {ids.generate_session_id() for _ in range(200)}
        self.assertEqual(len(produced), 200)
        for session_id in produced:
            self.assertRegex(session_id, SESSION_RE)


class CanonicalSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        ids.reset_state()

    def test_canonical_id_passes_through_untouched(self) -> None:
        # A signal already shaped like a real session id must survive, because
        # re-minting it would throw away upstream prompt-cache affinity.
        already = "ses_" + "0123456789ab" + "b" * 14
        self.assertIs(ids.canonical_session_id(already), already)
        self.assertEqual(ids.canonical_session_id(already), already)

    def test_canonical_id_is_stable_for_the_same_signal(self) -> None:
        signal = '["hello there"]'
        first = ids.canonical_session_id(signal)
        for _ in range(5):
            self.assertEqual(ids.canonical_session_id(signal), first)
        self.assertRegex(first, SESSION_RE)

    def test_canonical_id_differs_for_different_signals(self) -> None:
        self.assertNotEqual(
            ids.canonical_session_id('"conversation-a"'),
            ids.canonical_session_id('"conversation-b"'),
        )

    def test_generate_is_not_stable_where_canonical_is(self) -> None:
        # The whole point of the split: generate mints, canonical caches.
        self.assertNotEqual(ids.generate_session_id(), ids.generate_session_id())
        ids.reset_state()
        signal = '"same"'
        self.assertEqual(ids.canonical_session_id(signal), ids.canonical_session_id(signal))

    def test_empty_signal_still_yields_a_valid_cached_id(self) -> None:
        first = ids.canonical_session_id("")
        self.assertRegex(first, SESSION_RE)
        self.assertEqual(ids.canonical_session_id(""), first)

    def test_cache_is_bounded_and_evicts_oldest(self) -> None:
        limit = ids._MAX_CACHED_SESSIONS
        self.assertGreater(limit, 0)
        for index in range(limit + 5):
            ids.canonical_session_id(f"conversation-{index}")
        self.assertEqual(len(ids._session_cache), limit)
        # Oldest insertions are the ones dropped; the newest are retained.
        self.assertNotIn("conversation-0", ids._session_cache)
        self.assertIn(f"conversation-{limit + 4}", ids._session_cache)

    def test_cached_value_is_reused_not_re_minted(self) -> None:
        signal = '"cache-me"'
        first = ids.canonical_session_id(signal)
        # Advance the clock far enough that a fresh mint would change the
        # timestamp half; the cache must still win.
        second = ids.canonical_session_id(signal, now_ms=FIXED_MS + 10_000)
        self.assertEqual(first, second)

    def test_reset_state_clears_the_session_cache(self) -> None:
        signal = '"forget-me"'
        ids.canonical_session_id(signal)
        self.assertTrue(ids._session_cache)
        ids.reset_state()
        self.assertFalse(ids._session_cache)


class ConversationSignalTest(unittest.TestCase):
    def setUp(self) -> None:
        ids.reset_state()

    def test_signal_is_deterministic_for_identical_messages(self) -> None:
        messages = [{"role": "user", "content": "what is 2+2?"}]
        first = ids.conversation_signal(messages)
        second = ids.conversation_signal([{"role": "user", "content": "what is 2+2?"}])
        self.assertEqual(first, second)
        self.assertEqual(first, '"what is 2+2?"')

    def test_signal_uses_the_first_user_turn_only(self) -> None:
        # Multi-turn history must not change the key, or affinity would be
        # lost on every follow-up message (ids.ts conversationSeed).
        one = [{"role": "user", "content": "opening"}]
        many = [
            {"role": "user", "content": "opening"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "follow-up"},
        ]
        self.assertEqual(ids.conversation_signal(one), ids.conversation_signal(many))

    def test_signal_skips_leading_system_messages(self) -> None:
        with_system = [
            {"role": "system", "content": "you are a helpful agent"},
            {"role": "user", "content": "question"},
        ]
        self.assertEqual(ids.conversation_signal(with_system), '"question"')

    def test_signal_differs_when_the_first_user_message_differs(self) -> None:
        self.assertNotEqual(
            ids.conversation_signal([{"role": "user", "content": "alpha"}]),
            ids.conversation_signal([{"role": "user", "content": "beta"}]),
        )

    def test_signal_is_stable_under_key_reordering(self) -> None:
        first = [{"role": "user", "content": "same"}]
        second = [{"content": "same", "role": "user"}]
        self.assertEqual(ids.conversation_signal(first), ids.conversation_signal(second))

    def test_signal_of_structured_content_is_json_encoded(self) -> None:
        signal = ids.conversation_signal(
            [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        )
        self.assertIn("hi", signal)
        self.assertTrue(signal.startswith("[") or signal.startswith("{"))

    def test_no_user_turn_yields_empty_signal(self) -> None:
        for messages in (
            [],
            None,
            [{"role": "system", "content": "system only"}],
            [{"role": "assistant", "content": "no user yet"}],
        ):
            with self.subTest(messages=messages):
                self.assertEqual(ids.conversation_signal(messages), "")

    def test_null_content_is_skipped_rather_than_encoded(self) -> None:
        # json.dumps(None) == "null"; a null-content user turn (the shape an
        # assistant tool-call reply uses) must not become the conversation key.
        self.assertEqual(
            ids.conversation_signal(
                [{"role": "user", "content": None}, {"role": "user", "content": "real"}]
            ),
            '"real"',
        )

    def test_non_dict_messages_are_tolerated(self) -> None:
        self.assertEqual(
            ids.conversation_signal(["junk", None, {"role": "user", "content": "kept"}]),
            '"kept"',
        )

    def test_signal_feeds_canonical_session_id_stably(self) -> None:
        messages = [{"role": "user", "content": "stable conversation"}]
        first = ids.canonical_session_id(ids.conversation_signal(messages))
        second = ids.canonical_session_id(ids.conversation_signal(list(messages)))
        self.assertEqual(first, second)
        self.assertRegex(first, SESSION_RE)


class WireAgreementTest(unittest.TestCase):
    """The pattern here is the same one docs/GATE.md documents."""

    def test_module_pattern_matches_the_documented_regex(self) -> None:
        self.assertEqual(ids.CANONICAL_SESSION_PATTERN, r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")

    def test_module_pattern_is_anchored_at_both_ends(self) -> None:
        self.assertTrue(ids.CANONICAL_SESSION_PATTERN.startswith("^"))
        self.assertTrue(ids.CANONICAL_SESSION_PATTERN.endswith("$"))

    def test_pattern_accepts_the_probe_matrix_accepted_values(self) -> None:
        # Every 200 row from the x-opencode-session table in docs/GATE.md.
        for accepted in (
            "ses_" + "0" * 12 + "b" * 14,
            "ses_" + "a" * 12 + "b" * 14,
            "ses_" + "0123456789ab" + "b" * 14,
        ):
            with self.subTest(accepted=accepted):
                self.assertRegex(accepted, SESSION_RE)


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
