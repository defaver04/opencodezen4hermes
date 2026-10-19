"""Tests for :mod:`sidecar.catalog` — the free-roster decision chain.

The subtle part of this module is the *order* of the verdicts, and it is the
reason the module exists at all (see its docstring): a ready metadata verdict
always wins, **including a negative one**, and the ``"free" in name`` heuristic
only fires when metadata cannot speak (pending, or the model is absent from the
feed). Getting that backwards is how a delisted ``*-free`` id stays in a picker
forever.

Everything here is offline: the network fetchers are replaced by attribute
assignment on the instance. No sockets are opened (BLUEPRINT §8).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error

from sidecar import catalog as catalog_module

#: BLUEPRINT §3, in picker order. The single source of truth for the roster.
BLUEPRINT_ROSTER = (
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

BLOCKED = ("exo-free", "jev-1.13-free", "ling-3.0-flash-fin-free")


def price(**kwargs) -> catalog_module.ModelPrice:
    return catalog_module.ModelPrice(**kwargs)


class BlocklistTest(unittest.TestCase):
    def test_blocklist_contains_the_three_documented_ids(self) -> None:
        self.assertIsInstance(catalog_module.FREE_LANE_BLOCKLIST, frozenset)
        for model in BLOCKED:
            with self.subTest(model=model):
                self.assertIn(model, catalog_module.FREE_LANE_BLOCKLIST)

    def test_blocklist_is_exactly_those_three(self) -> None:
        # Pinned deliberately: adding an id here removes it from every picker,
        # and removing one resurrects a model that returns 400/410 upstream.
        self.assertEqual(catalog_module.FREE_LANE_BLOCKLIST, frozenset(BLOCKED))

    def test_no_blocked_id_appears_in_the_static_roster(self) -> None:
        for model in BLOCKED:
            with self.subTest(model=model):
                self.assertNotIn(model, catalog_module.STATIC_FREE_MODELS)

    def test_blocklist_beats_a_positive_metadata_verdict(self) -> None:
        # The blocklist is checked before pricing, so a free-looking delisted
        # id can never be resurrected by a stale models.dev row.
        cat = catalog_module.Catalog("https://example.invalid/v1", None)
        cat._fetch_live = lambda: set(BLOCKED)
        cat._fetch_prices = lambda: {model: price(input=0, output=0) for model in BLOCKED}
        cat.refresh()
        for model in BLOCKED:
            with self.subTest(model=model):
                verdict = cat.decision(model)
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.source, "local_blocklist")
                self.assertTrue(verdict.known)

    def test_blocked_ids_never_surface_in_models(self) -> None:
        cat = catalog_module.Catalog("https://example.invalid/v1", None)
        cat._fetch_live = lambda: set(BLOCKED) | {"big-pickle"}
        cat._fetch_prices = lambda: {
            **{model: price(input=0, output=0) for model in BLOCKED},
            "big-pickle": price(input=0, output=0),
        }
        cat.refresh()
        self.assertEqual(cat.models(), ["big-pickle"])


class StaticRosterTest(unittest.TestCase):
    def test_static_roster_has_exactly_the_nine_blueprint_ids(self) -> None:
        # Membership, not declaration order: the tuple literal in
        # sidecar/catalog.py is written chronologically (newest additions last),
        # while ``Catalog.models()`` sorts before exposing. The exposed order is
        # what BLUEPRINT §3 pins, and it is asserted below.
        self.assertEqual(set(catalog_module.STATIC_FREE_MODELS), set(BLUEPRINT_ROSTER))
        self.assertEqual(len(catalog_module.STATIC_FREE_MODELS), len(BLUEPRINT_ROSTER))

    def test_exposed_bootstrap_roster_is_in_blueprint_picker_order(self) -> None:
        # BLUEPRINT §3: "Nine models, in this order everywhere (it is the
        # picker order)", and that order happens to be lexicographic.
        self.assertEqual(list(BLUEPRINT_ROSTER), sorted(BLUEPRINT_ROSTER))
        exposed = catalog_module.Catalog("https://example.invalid/v1", None).models()
        self.assertEqual(exposed, list(BLUEPRINT_ROSTER))

    def test_static_roster_has_nine_unique_ids(self) -> None:
        self.assertEqual(len(catalog_module.STATIC_FREE_MODELS), 9)
        self.assertEqual(len(set(catalog_module.STATIC_FREE_MODELS)), 9)

    def test_every_static_id_is_a_string(self) -> None:
        for model in catalog_module.STATIC_FREE_MODELS:
            with self.subTest(model=model):
                self.assertIsInstance(model, str)
                self.assertTrue(model)
                self.assertNotIn(" ", model)

    def test_static_roster_is_a_tuple_so_it_cannot_be_mutated(self) -> None:
        self.assertIsInstance(catalog_module.STATIC_FREE_MODELS, tuple)

    def test_bootstrap_exposure_is_sorted_and_complete(self) -> None:
        cat = catalog_module.Catalog("https://example.invalid/v1", None)
        exposed = cat.models()
        self.assertEqual(exposed, sorted(BLUEPRINT_ROSTER))
        self.assertEqual(len(exposed), 9)


class LocalOverridesTest(unittest.TestCase):
    def test_mimo_context_window_override_is_one_mebibyte(self) -> None:
        self.assertEqual(
            catalog_module.LOCAL_MODEL_OVERRIDES["mimo-v2.6-flash-free"]["context_window"],
            1048576,
        )

    def test_limits_applies_the_override_over_feed_data(self) -> None:
        # models.dev says 200k; the model card and the official CLI say 1 MiB.
        cat = catalog_module.Catalog("https://example.invalid/v1", None)
        cat._fetch_live = lambda: {"mimo-v2.6-flash-free"}
        cat._fetch_prices = lambda: {"mimo-v2.6-flash-free": price(input=0, output=0, context_window=200000)}
        cat.refresh()
        self.assertEqual(cat.limits("mimo-v2.6-flash-free")["context_window"], 1048576)

    def test_limits_returns_none_for_an_unknown_model(self) -> None:
        cat = catalog_module.Catalog("https://example.invalid/v1", None)
        self.assertIsNone(cat.limits("never-heard-of-it"))

    def test_limits_captures_context_and_output(self) -> None:
        cat = catalog_module.Catalog("https://example.invalid/v1", None)
        cat._fetch_live = lambda: {"m"}
        cat._fetch_prices = lambda: {"m": price(input=0, output=0, context_window=128000, max_output=8192)}
        cat.refresh()
        self.assertEqual(cat.limits("m"), {"context_window": 128000, "max_output": 8192})


class DecideTest(unittest.TestCase):
    """``decide(model, prices, ready)`` — metadata first, name fallback second."""

    def test_zero_input_and_zero_output_is_free(self) -> None:
        verdict = catalog_module.decide("anything", {"anything": price(input=0, output=0)}, True)
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.source, "metadata_free")
        self.assertTrue(verdict.known)

    def test_zero_pricing_on_a_free_named_model_reports_both_sources(self) -> None:
        verdict = catalog_module.decide("x-free", {"x-free": price(input=0, output=0)}, True)
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.source, "name_and_metadata_free")

    def test_deprecated_is_denied_even_when_free(self) -> None:
        verdict = catalog_module.decide("x-free", {"x-free": price(input=0, output=0, deprecated=True)}, True)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.source, "metadata_deprecated")
        self.assertTrue(verdict.known)

    def test_deprecation_beats_price_zero_because_it_is_checked_first(self) -> None:
        # The ordering inside decide(): deprecated -> free -> unknown -> paid.
        verdict = catalog_module.decide("delisted-free", {"delisted-free": price(input=0, output=0, deprecated=True)}, True)
        self.assertFalse(verdict.allowed)

    def test_paid_pricing_is_denied(self) -> None:
        verdict = catalog_module.decide("x", {"x": price(input=1.0, output=2.0)}, True)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.source, "metadata_paid")
        self.assertTrue(verdict.known)

    def test_paid_pricing_on_a_free_named_model_is_still_denied(self) -> None:
        # THE regression this module exists to prevent: a ready negative
        # verdict outranks the name heuristic.
        verdict = catalog_module.decide("x-free", {"x-free": price(input=0.5, output=0.5)}, True)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.source, "metadata_paid")

    def test_unknown_cost_is_denied_when_metadata_is_ready(self) -> None:
        verdict = catalog_module.decide("x", {"x": price(input=None, output=None)}, True)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.source, "metadata_cost_unknown")
        self.assertFalse(verdict.known)

    def test_only_one_sided_cost_is_unknown(self) -> None:
        for model_price in (price(input=0, output=None), price(input=None, output=0)):
            with self.subTest(price=model_price):
                verdict = catalog_module.decide("x", {"x": model_price}, True)
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.source, "metadata_cost_unknown")

    def test_present_but_unknown_cost_denies_even_a_free_named_model(self) -> None:
        # A row that exists but declares no price is *not* proof of any free
        # conclusion, so it is denied rather than granted by the name
        # heuristic. This is the ``known=False`` branch of decide(): the model
        # is absent from the free list until models.dev says something.
        verdict = catalog_module.decide("x-free", {"x-free": price(input=None, output=None)}, True)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.source, "metadata_cost_unknown")
        self.assertFalse(verdict.known)

    def test_metadata_pending_falls_back_to_the_name(self) -> None:
        verdict = catalog_module.decide("x-free", {}, False)
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.source, "name_free")

    def test_metadata_pending_denies_a_paid_sounding_name(self) -> None:
        verdict = catalog_module.decide("x", {}, False)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.source, "metadata_pending")

    def test_empty_metadata_map_is_treated_as_pending_even_when_ready(self) -> None:
        # ``not prices`` short-circuits: an empty feed is indistinguishable
        # from an unloaded one, so the name heuristic is the only signal.
        verdict = catalog_module.decide("x-free", {}, True)
        self.assertEqual(verdict.source, "name_free")
        verdict = catalog_module.decide("x", {}, True)
        self.assertEqual(verdict.source, "metadata_pending")

    def test_model_missing_from_the_feed_falls_back_to_the_name(self) -> None:
        prices = {"other": price(input=1, output=1)}
        verdict = catalog_module.decide("new-free", prices, True)
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.source, "name_free")
        self.assertFalse(verdict.known)

    def test_model_missing_from_the_feed_is_denied_when_not_free_named(self) -> None:
        verdict = catalog_module.decide("new-model", {"other": price()}, True)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.source, "metadata_model_missing")

    def test_decision_carries_source_and_known_fields(self) -> None:
        verdict = catalog_module.decide("x", {"x": price(input=0, output=0)}, True)
        self.assertIsInstance(verdict, catalog_module.Decision)
        self.assertIsInstance(verdict.allowed, bool)
        self.assertIsInstance(verdict.source, str)
        self.assertIsInstance(verdict.known, bool)


class FreeNameTest(unittest.TestCase):
    def test_name_heuristic_looks_for_free_anywhere_case_insensitively(self) -> None:
        self.assertTrue(catalog_module.is_free_name("x-free"))
        self.assertTrue(catalog_module.is_free_name("GLM-5.3-FLASH-FREE"))
        self.assertTrue(catalog_module.is_free_name("free-tier-model"))

    def test_name_heuristic_rejects_names_without_free(self) -> None:
        for name in ("big-pickle", "gpt-4", "", None):
            with self.subTest(name=name):
                self.assertFalse(catalog_module.is_free_name(name))


class DecodeModelsDevTest(unittest.TestCase):
    def _feed(self, **providers) -> dict:
        return dict(providers)

    def test_prefers_the_opencode_provider(self) -> None:
        payload = self._feed(
            openai={"models": {"gpt": {"cost": {"input": 1, "output": 2}}}},
            opencode={"models": {"oc-free": {"cost": {"input": 0, "output": 0}}}},
        )
        decoded = catalog_module.decode_models_dev(payload)
        self.assertEqual(list(decoded), ["oc-free"])

    def test_opencode_zen_key_is_also_accepted(self) -> None:
        payload = {"opencode-zen": {"models": {"z-free": {"cost": {"input": 0, "output": 0}}}}}
        self.assertEqual(list(catalog_module.decode_models_dev(payload)), ["z-free"])

    def test_exact_key_outranks_a_substring_key(self) -> None:
        payload = {
            "opencode-reseller": {
                "id": "acme",
                "name": "Acme OpenCode mirror",
                "models": {"mirror": {"cost": {"input": 9, "output": 9}}},
            },
            "opencode": {"models": {"real": {"cost": {"input": 0, "output": 0}}}},
        }
        self.assertEqual(list(catalog_module.decode_models_dev(payload)), ["real"])

    def test_substring_key_without_opencode_identity_is_ignored(self) -> None:
        payload = {
            "opencode-reseller": {
                "id": "acme",
                "name": "Acme Reseller",
                "models": {"q": {"cost": {"input": 1, "output": 1}}},
            }
        }
        self.assertEqual(catalog_module.decode_models_dev(payload), {})

    def test_substring_key_with_opencode_identity_is_accepted(self) -> None:
        payload = {
            "opencode-reseller": {
                "id": "acme",
                "name": "Acme OpenCode",
                "models": {"q": {"cost": {"input": 0, "output": 0}}},
            }
        }
        self.assertEqual(list(catalog_module.decode_models_dev(payload)), ["q"])

    def test_non_dict_payloads_return_empty(self) -> None:
        for payload in (None, [], "nope", 7):
            with self.subTest(payload=payload):
                self.assertEqual(catalog_module.decode_models_dev(payload), {})

    def test_model_id_field_overrides_the_dict_key(self) -> None:
        payload = {"opencode": {"models": {"key-a": {"id": "real-a", "cost": {"input": 0, "output": 0}}}}}
        self.assertEqual(list(catalog_module.decode_models_dev(payload)), ["real-a"])

    def test_prices_and_limits_are_decoded(self) -> None:
        payload = {
            "opencode": {
                "models": {
                    "m": {
                        "cost": {"input": 0, "output": 0},
                        "limit": {"context": 200000, "output": 8192},
                        "reasoning": True,
                        "modalities": {"input": ["text", "image"]},
                    }
                }
            }
        }
        decoded = catalog_module.decode_models_dev(payload)["m"]
        self.assertEqual(decoded.context_window, 200000)
        self.assertEqual(decoded.max_output, 8192)
        self.assertTrue(decoded.reasoning)
        self.assertEqual(decoded.modalities, ["text", "image"])

    def test_boolean_costs_are_not_treated_as_prices(self) -> None:
        # bool is an int subclass; treating True as 1.0 would make a free model
        # look paid (or vice versa) depending on the value.
        payload = {"opencode": {"models": {"m": {"cost": {"input": True, "output": True}}}}}
        decoded = catalog_module.decode_models_dev(payload)["m"]
        self.assertIsNone(decoded.input)
        self.assertIsNone(decoded.output)

    def test_deprecation_flags_are_decoded_from_every_documented_field(self) -> None:
        cases = (
            {"deprecated": True},
            {"status": "deprecated"},
            {"status": "retired"},
            {"status": "disabled"},
            {"lifecycle": "DEPRECATED"},
            {"deprecated_at": "2026-01-01"},
            {"retirement_date": "2026-01-01"},
        )
        for marker in cases:
            with self.subTest(marker=marker):
                payload = {"opencode": {"models": {"m": {"cost": {"input": 0, "output": 0}, **marker}}}}
                self.assertTrue(catalog_module.decode_models_dev(payload)["m"].deprecated)

    def test_an_unmarked_model_is_not_deprecated(self) -> None:
        payload = {"opencode": {"models": {"m": {"cost": {"input": 0, "output": 0}}}}}
        self.assertFalse(catalog_module.decode_models_dev(payload)["m"].deprecated)

    def test_effort_values_are_decoded_and_deduplicated(self) -> None:
        payload = {
            "opencode": {
                "models": {
                    "m": {
                        "cost": {"input": 0, "output": 0},
                        "reasoning_options": [
                            {"type": "effort", "values": ["low", "low", ""]},
                            {"type": "budget", "values": ["ignored"]},
                            "junk",
                            {"type": "effort", "values": ["high"]},
                        ],
                    }
                }
            }
        }
        self.assertEqual(catalog_module.decode_models_dev(payload)["m"].effort_values, ["low", "high"])

    def test_modalities_are_deduplicated_and_empty_becomes_none(self) -> None:
        payload = {"opencode": {"models": {"m": {"cost": {"input": 0, "output": 0}, "modalities": {"input": ["text", "text", ""]}}}}}
        self.assertEqual(catalog_module.decode_models_dev(payload)["m"].modalities, ["text"])
        empty = {"opencode": {"models": {"m": {"cost": {"input": 0, "output": 0}, "modalities": {"input": []}}}}}
        self.assertIsNone(catalog_module.decode_models_dev(empty)["m"].modalities)

    def test_malformed_provider_bodies_are_skipped(self) -> None:
        for payload in (
            {"opencode": "nope"},
            {"opencode": {"models": []}},
            {"opencode": {"models": {"m": "not-a-dict"}}},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(catalog_module.decode_models_dev(payload), {})


class CatalogBehaviourTest(unittest.TestCase):
    def _catalog(self, **kwargs) -> catalog_module.Catalog:
        return catalog_module.Catalog("https://example.invalid/v1", None, **kwargs)

    def test_bootstrap_roster_when_nothing_fetched(self) -> None:
        cat = self._catalog()
        self.assertEqual(cat.models(), sorted(BLUEPRINT_ROSTER))

    def test_snapshot_reports_pending_before_the_first_fetch(self) -> None:
        snapshot = self._catalog().snapshot()
        self.assertEqual(snapshot["status"], "pending")
        self.assertEqual(snapshot["total"], 0)
        self.assertEqual(snapshot["exposed"], 9)
        self.assertFalse(snapshot["prices_ready"])
        self.assertIsNone(snapshot["age_seconds"])

    def test_live_roster_filters_by_the_free_verdict(self) -> None:
        cat = self._catalog()
        cat._fetch_live = lambda: {"free-one", "paid-one", "big-pickle"}
        cat._fetch_prices = lambda: {
            "free-one": price(input=0, output=0),
            "paid-one": price(input=3, output=3),
            "big-pickle": price(input=0, output=0),
        }
        cat.refresh()
        self.assertEqual(cat.models(), ["big-pickle", "free-one"])

    def test_live_roster_is_sorted(self) -> None:
        cat = self._catalog()
        cat._fetch_live = lambda: {"z-free", "a-free", "m-free"}
        cat._fetch_prices = lambda: {model: price(input=0, output=0) for model in ("z-free", "a-free", "m-free")}
        cat.refresh()
        self.assertEqual(cat.models(), ["a-free", "m-free", "z-free"])

    def test_include_paid_exposes_paid_models(self) -> None:
        cat = self._catalog(include_paid=True)
        cat._fetch_live = lambda: {"paid-one", "big-pickle"}
        cat._fetch_prices = lambda: {"paid-one": price(input=3, output=3)}
        cat.refresh()
        self.assertEqual(cat.models(), ["big-pickle", "paid-one"])

    def test_include_paid_still_honours_the_blocklist(self) -> None:
        cat = self._catalog(include_paid=True)
        cat._fetch_live = lambda: {"exo-free", "paid-one"}
        cat._fetch_prices = lambda: {"paid-one": price(input=3, output=3)}
        cat.refresh()
        self.assertEqual(cat.models(), ["paid-one"])

    def test_static_id_is_vouched_when_metadata_lies_about_deprecation(self) -> None:
        # A compile-time verified id survives a stale deprecation claim; it only
        # dies by leaving the live catalog, which is the blocklist's job.
        cat = self._catalog()
        cat._fetch_live = lambda: set(BLUEPRINT_ROSTER)
        cat._fetch_prices = lambda: {"big-pickle": price(deprecated=True)}
        cat.refresh()
        verdict = cat.decision("big-pickle")
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.source, "static_verified")

    def test_static_id_is_vouched_when_absent_from_metadata(self) -> None:
        cat = self._catalog()
        cat._fetch_live = lambda: {"big-pickle"}
        cat._fetch_prices = lambda: {"something-else": price(input=1, output=1)}
        cat.refresh()
        self.assertEqual(cat.decision("big-pickle").source, "static_verified")

    def test_static_rescue_does_not_override_a_paid_verdict(self) -> None:
        # Only deprecated/missing/pending are rescued; a ready paid price is a
        # fact, not a metadata gap.
        cat = self._catalog()
        cat._fetch_live = lambda: {"big-pickle"}
        cat._fetch_prices = lambda: {"big-pickle": price(input=1, output=1)}
        cat.refresh()
        verdict = cat.decision("big-pickle")
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.source, "metadata_paid")

    def test_non_static_free_named_model_is_not_rescued(self) -> None:
        cat = self._catalog()
        cat._fetch_live = lambda: {"random-free"}
        cat._fetch_prices = lambda: {"random-free": price(deprecated=True)}
        cat.refresh()
        self.assertFalse(cat.decision("random-free").allowed)

    def test_failed_refresh_keeps_the_bootstrap_and_records_the_error(self) -> None:
        cat = self._catalog()

        def boom():
            raise ValueError("models endpoint returned an empty list")

        cat._fetch_live = boom
        cat._fetch_prices = boom
        cat.refresh()  # must never raise
        self.assertEqual(cat.models(), sorted(BLUEPRINT_ROSTER))
        snapshot = cat.snapshot()
        self.assertEqual(snapshot["status"], "pending")
        self.assertIsNotNone(snapshot["last_error"])

    def test_refresh_swallows_the_documented_failure_types(self) -> None:
        # ``refresh`` catches URLError/HTTPError/OSError/ValueError and nothing
        # else — a programming error such as RuntimeError must still escape, so
        # a genuine bug is not silently converted into "catalog pending".
        cat = self._catalog()
        for error in (OSError("down"), ValueError("bad json")):
            with self.subTest(error=error):
                def boom(error=error):
                    raise error

                cat._fetch_live = boom
                cat._fetch_prices = boom
                cat.refresh(force=True)  # must not raise

    def test_refresh_lets_an_unexpected_error_escape(self) -> None:
        cat = self._catalog()

        def boom():
            raise RuntimeError("a real bug")

        cat._fetch_live = boom
        cat._fetch_prices = boom
        with self.assertRaises(RuntimeError):
            cat.refresh(force=True)

    def test_ttl_suppresses_a_redundant_refresh(self) -> None:
        calls = {"n": 0}
        cat = self._catalog(ttl=300)

        def fetch_live():
            calls["n"] += 1
            return {"big-pickle"}

        cat._fetch_live = fetch_live
        cat._fetch_prices = lambda: {"big-pickle": price(input=0, output=0)}
        cat.refresh()
        cat.refresh()
        self.assertEqual(calls["n"], 1)

    def test_force_bypasses_the_ttl(self) -> None:
        calls = {"n": 0}
        cat = self._catalog(ttl=300)

        def fetch_live():
            calls["n"] += 1
            return {"big-pickle"}

        cat._fetch_live = fetch_live
        cat._fetch_prices = lambda: {"big-pickle": price(input=0, output=0)}
        cat.refresh()
        cat.refresh(force=True)
        self.assertEqual(calls["n"], 2)

    def test_snapshot_becomes_ready_after_a_successful_refresh(self) -> None:
        cat = self._catalog()
        cat._fetch_live = lambda: {"big-pickle"}
        cat._fetch_prices = lambda: {"big-pickle": price(input=0, output=0)}
        cat.refresh()
        snapshot = cat.snapshot()
        self.assertEqual(snapshot["status"], "ready")
        self.assertEqual(snapshot["total"], 1)
        self.assertEqual(snapshot["exposed"], 1)
        self.assertTrue(snapshot["prices_ready"])
        self.assertGreaterEqual(snapshot["age_seconds"], 0)

    def test_snapshot_goes_stale_after_ten_minutes(self) -> None:
        cat = self._catalog()
        cat._fetch_live = lambda: {"big-pickle"}
        cat._fetch_prices = lambda: {"big-pickle": price(input=0, output=0)}
        cat.refresh()
        cat._refreshed_at = time.time() - 601
        self.assertEqual(cat.snapshot()["status"], "stale")


class MaybeRefreshTest(unittest.TestCase):
    """``maybe_refresh`` gives the advertised TTL its meaning (Finding A).

    Without it ``refresh()`` ran exactly once, from ``warm()`` at boot, so a
    sidecar left running never noticed a model added upstream and
    ``OPENCODEZEN4HERMES_CATALOG_TTL`` was dead configuration.
    """

    def _catalog(self, *, ttl: float = 300.0) -> catalog_module.Catalog:
        return catalog_module.Catalog("https://lane.invalid/v1", None, ttl=ttl)

    def _settle(self, cat) -> None:
        """Wait for a scheduled refresh to finish (it runs on a daemon thread)."""
        deadline = time.time() + 5
        while time.time() < deadline:
            with cat._lock:
                if not cat._scheduled and not cat._refreshing:
                    return
            time.sleep(0.01)
        self.fail("scheduled refresh never settled")

    def test_a_fresh_snapshot_does_not_reschedule(self) -> None:
        cat = self._catalog(ttl=300)
        cat._fetch_live = lambda: {"big-pickle"}
        cat._fetch_prices = lambda: {"big-pickle": price(input=0, output=0)}
        cat.refresh()
        self.assertFalse(cat.maybe_refresh())

    def test_a_lapsed_ttl_schedules_a_refresh(self) -> None:
        calls = {"n": 0}
        cat = self._catalog(ttl=300)

        def fetch_live():
            calls["n"] += 1
            return {"big-pickle"}

        cat._fetch_live = fetch_live
        cat._fetch_prices = lambda: {"big-pickle": price(input=0, output=0)}
        cat.refresh()
        self.assertEqual(calls["n"], 1)

        cat._refreshed_at = time.time() - 301
        self.assertTrue(cat.maybe_refresh())
        self._settle(cat)
        self.assertEqual(calls["n"], 2)

    def test_it_is_non_blocking_and_answers_from_the_current_snapshot(self) -> None:
        """Stale-while-revalidate: the caller never waits on the network."""
        release = threading.Event()
        cat = self._catalog(ttl=0)
        # Roster-shaped ids: models() filters by the free verdict, so a made-up
        # name would be dropped and the test would pass for the wrong reason.
        cat._live = {"beam-free"}

        def fetch_live():
            release.wait(timeout=5)
            return {"glm-5.3-flash-free"}

        cat._fetch_live = fetch_live
        cat._fetch_prices = lambda: {}
        try:
            self.assertTrue(cat.maybe_refresh())
            # Returned while the fetch is still blocked...
            self.assertFalse(release.is_set())
            # ...and the current snapshot is what a caller sees meanwhile.
            self.assertEqual(cat.models(), ["beam-free"])
        finally:
            release.set()
        self._settle(cat)
        self.assertEqual(cat.models(), ["glm-5.3-flash-free"])

    def test_a_burst_schedules_only_one_refresh(self) -> None:
        calls = {"n": 0}
        release = threading.Event()
        cat = self._catalog(ttl=0)
        cat._live = {"beam-free"}

        def fetch_live():
            calls["n"] += 1
            release.wait(timeout=5)
            return {"beam-free"}

        cat._fetch_live = fetch_live
        cat._fetch_prices = lambda: {}
        try:
            results = [cat.maybe_refresh() for _ in range(10)]
            self.assertEqual(results.count(True), 1)
        finally:
            release.set()
        self._settle(cat)
        self.assertEqual(calls["n"], 1)

    def test_a_failed_refresh_is_not_an_error_and_is_reported(self) -> None:
        """The lane being down must never make the sidecar stop answering."""
        cat = self._catalog(ttl=0)
        cat._live = {"beam-free"}

        def boom():
            raise urllib.error.URLError("lane down")

        cat._fetch_live = boom
        cat._fetch_prices = lambda: {}
        self.assertTrue(cat.maybe_refresh())
        self._settle(cat)
        self.assertIn("lane down", cat.snapshot()["last_error"] or "")
        # The roster survives the failure.
        self.assertEqual(cat.models(), ["beam-free"])

    def test_never_fetched_counts_as_lapsed(self) -> None:
        """A cold catalog refreshes on the first read, not only at boot."""
        calls = {"n": 0}
        cat = self._catalog(ttl=300)

        def fetch_live():
            calls["n"] += 1
            return {"m"}

        cat._fetch_live = fetch_live
        cat._fetch_prices = lambda: {}
        self.assertTrue(cat.maybe_refresh())
        self._settle(cat)
        self.assertEqual(calls["n"], 1)

    def test_modalities_returns_none_for_an_unknown_model(self) -> None:
        cat = self._catalog()
        self.assertIsNone(cat.modalities("nope"))

    def test_modalities_returns_a_copy_not_the_internal_list(self) -> None:
        cat = self._catalog()
        cat._fetch_live = lambda: {"m"}
        cat._fetch_prices = lambda: {"m": price(input=0, output=0, modalities=["text"])}
        cat.refresh()
        captured = cat.modalities("m")
        self.assertEqual(captured, ["text"])
        captured.append("mutated")
        self.assertEqual(cat.modalities("m"), ["text"])


class DiskCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _catalog(self, **kwargs) -> catalog_module.Catalog:
        return catalog_module.Catalog("https://example.invalid/v1", self.cache_dir, **kwargs)

    @property
    def cache_path(self) -> str:
        return os.path.join(self.cache_dir, "models-dev-cache.json")

    def _populate(self, cat: catalog_module.Catalog) -> None:
        cat._fetch_live = lambda: {"big-pickle"}
        cat._fetch_prices = lambda: {"big-pickle": price(input=0, output=0, context_window=128000)}
        cat.refresh()

    def test_cache_file_is_written_with_prices(self) -> None:
        self._populate(self._catalog())
        self.assertTrue(os.path.exists(self.cache_path))
        with open(self.cache_path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertIn("updated_at", raw)
        self.assertEqual(raw["prices"]["big-pickle"]["context_window"], 128000)

    def test_cache_round_trips_into_a_fresh_catalog(self) -> None:
        self._populate(self._catalog())
        reloaded = self._catalog()._load_cached_prices()
        self.assertEqual(reloaded["big-pickle"].context_window, 128000)

    def test_cache_is_ignored_after_seven_ttls(self) -> None:
        self._populate(self._catalog())
        with open(self.cache_path, encoding="utf-8") as handle:
            raw = json.load(handle)
        raw["updated_at"] = time.time() - 8 * catalog_module.METADATA_TTL
        with open(self.cache_path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        self.assertEqual(self._catalog()._load_cached_prices(), {})

    def test_corrupt_cache_is_ignored_not_raised(self) -> None:
        with open(self.cache_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(self._catalog()._load_cached_prices(), {})

    def test_missing_cache_dir_yields_no_prices(self) -> None:
        cat = catalog_module.Catalog("https://example.invalid/v1", os.path.join(self.cache_dir, "absent"))
        self.assertEqual(cat._load_cached_prices(), {})

    def test_no_cache_dir_means_no_file_io(self) -> None:
        cat = catalog_module.Catalog("https://example.invalid/v1", None)
        cat._save_cached_prices({"m": price(input=0, output=0)})  # must be a no-op
        self.assertIsNone(cat._metadata_cache_path)

    def test_warm_does_not_hang_when_the_fetch_blocks(self) -> None:
        import threading

        release = threading.Event()
        # No cache_dir on purpose: ``warm`` runs ``refresh`` on a daemon
        # thread, so a cache write could still be in flight when
        # ``TemporaryDirectory`` starts deleting and the cleanup would fail
        # with "Directory not empty". With no cache dir the save is a no-op.
        cat = catalog_module.Catalog("https://example.invalid/v1", None)
        cat._fetch_live = lambda: (release.wait(5), {"big-pickle"})[1]
        cat._fetch_prices = lambda: {"big-pickle": price(input=0, output=0)}
        started = time.monotonic()
        cat.warm(timeout=0.05)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        # Release and drain the worker so it cannot outlive this test.
        release.set()
        deadline = time.monotonic() + 2.0
        while not cat._live and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(cat._live, "background refresh did not finish after release")


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
