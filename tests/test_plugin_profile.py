"""Tests for ``plugin/opencode-zen-free/__init__.py`` — the Hermes provider profile.

The plugin lives outside the importable tree and imports ``providers`` from
``hermes-agent``, which is not installed in this repository (BLUEPRINT §4 puts
it in the pack). So ``providers`` and ``providers.base`` are stubbed in
``sys.modules`` before the module is loaded by path — the test must not need
the real upstream tree, and it must not leave the stub behind for other test
modules.

The profile fields asserted here are the ones in BLUEPRINT §2 and §7: the slug,
the display name, the sidecar ``base_url`` (which is what makes every Hermes
completion go through the gate rewrite) and the empty ``env_vars`` (which is
what stops the picker from demanding a credential for an anonymous lane).

Stdlib only, no network (BLUEPRINT §8).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import unittest
from unittest import mock

#: BLUEPRINT §3, in picker order. ``FALLBACK_FREE_MODELS`` must match exactly.
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

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_PATH = os.path.join(_REPO_ROOT, "plugin", "opencode-zen-free", "__init__.py")

PROVIDER_SLUG = "opencode-zen-free"
PROVIDER_DISPLAY_NAME = "OpenCode Zen (free)"
SIDECAR_BASE_URL = "http://127.0.0.1:8799/v1"


class _StubProviderProfile:
    """Minimal stand-in for ``providers.base.ProviderProfile``.

    Real upstream profiles are dataclasses; the plugin instantiates one with
    keyword arguments, so accepting ``**kwargs`` and recording them as
    attributes is enough to observe what the plugin declares.
    """

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)

    def fetch_models(self, **kwargs):  # pragma: no cover - not called here
        raise NotImplementedError


def _load_plugin():
    """Import the plugin by path with ``providers`` stubbed in ``sys.modules``.

    Returns ``(module, registered)`` where ``registered`` is the list of
    profiles the plugin passed to ``register_provider``.
    """
    registered: list = []

    providers_pkg = types.ModuleType("providers")
    base_mod = types.ModuleType("providers.base")
    base_mod.ProviderProfile = _StubProviderProfile
    providers_pkg.base = base_mod
    providers_pkg.ProviderProfile = _StubProviderProfile
    providers_pkg.register_provider = registered.append

    stubs = {"providers": providers_pkg, "providers.base": base_mod}
    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("opencode_zen_free_plugin_under_test", PLUGIN_PATH)
        module = importlib.util.module_from_spec(spec)
        # Executing inside the patched sys.modules means the module's own
        # ``from providers import register_provider`` binds our stub.
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
    return module, registered


class PluginLoadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module, cls.registered = _load_plugin()
        cls.profile = cls.module.profile

    def test_plugin_file_exists(self) -> None:
        self.assertTrue(os.path.isfile(PLUGIN_PATH), PLUGIN_PATH)

    def test_module_exposes_a_profile_object(self) -> None:
        self.assertTrue(hasattr(self.module, "profile"))
        self.assertIsNotNone(self.profile)

    def test_profile_class_subclasses_the_provider_base(self) -> None:
        self.assertTrue(issubclass(self.module.OpenCodeZenFreeProfile, _StubProviderProfile))

    def test_profile_is_registered_exactly_once(self) -> None:
        self.assertEqual(len(self.registered), 1)
        self.assertIs(self.registered[0], self.profile)

    def test_registration_happens_at_import_time(self) -> None:
        # A profile that is defined but never registered never reaches the
        # picker, so registration is part of the contract.
        _module, registered = _load_plugin()
        self.assertEqual(len(registered), 1)

    def test_import_does_not_leave_the_stub_in_sys_modules(self) -> None:
        # Other test modules import the real thing if it is present; leaking a
        # stub would silently change their behaviour.
        self.assertNotIn("providers", sys.modules)

    def test_module_defines_the_fallback_roster(self) -> None:
        self.assertTrue(hasattr(self.module, "FALLBACK_FREE_MODELS"))
        self.assertIsInstance(self.module.FALLBACK_FREE_MODELS, tuple)


class ProfileIdentityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module, _registered = _load_plugin()
        cls.profile = cls.module.profile

    def test_name_is_the_blueprint_slug(self) -> None:
        self.assertEqual(self.profile.name, PROVIDER_SLUG)

    def test_display_name_is_the_blueprint_value(self) -> None:
        self.assertEqual(self.profile.display_name, PROVIDER_DISPLAY_NAME)

    def test_description_is_present_and_mentions_no_api_key(self) -> None:
        self.assertTrue(self.profile.description)
        self.assertIn("no api key", self.profile.description.lower())

    def test_signup_url_points_at_opencode(self) -> None:
        self.assertEqual(self.profile.signup_url, "https://opencode.ai")


class ProfileRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module, _registered = _load_plugin()
        cls.profile = cls.module.profile

    def test_base_url_is_the_local_sidecar_v1(self) -> None:
        # BLUEPRINT §2: sidecar base URL. This single string is what routes
        # every Hermes completion through the gate rewrite.
        self.assertEqual(self.profile.base_url, SIDECAR_BASE_URL)

    def test_base_url_host_is_loopback(self) -> None:
        self.assertTrue(self.profile.base_url.startswith("http://127.0.0.1:"))
        self.assertNotIn("opencode.ai", self.profile.base_url)

    def test_base_url_port_matches_the_sidecar_default(self) -> None:
        self.assertEqual(self.module.SIDECAR_PORT, 8799)
        self.assertIn(f":{self.module.SIDECAR_PORT}/", self.profile.base_url)

    def test_sidecar_host_and_port_constants_are_exported(self) -> None:
        self.assertEqual(self.module.SIDECAR_HOST, "127.0.0.1")

    def test_auth_type_is_api_key(self) -> None:
        # Deliberately api_key even though no credential is used: it is what
        # makes get_auth_status() report configured from the config seed.
        self.assertEqual(self.profile.auth_type, "api_key")

    def test_env_vars_is_empty_so_no_credential_is_demanded(self) -> None:
        self.assertEqual(tuple(self.profile.env_vars), ())

    def test_supports_model_listing_is_true(self) -> None:
        self.assertIs(self.profile.supports_model_listing, True)

    def test_health_check_is_disabled(self) -> None:
        # The sidecar is local; a doctor /models probe against the lane would
        # be a network call the user did not ask for.
        self.assertIs(self.profile.supports_health_check, False)


class FallbackRosterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module, _registered = _load_plugin()
        cls.profile = cls.module.profile

    def test_fallback_models_match_the_blueprint_roster_in_order(self) -> None:
        # BLUEPRINT §3: "A test asserts the two agree."
        self.assertEqual(tuple(self.module.FALLBACK_FREE_MODELS), BLUEPRINT_ROSTER)

    def test_profile_fallback_models_is_the_module_constant(self) -> None:
        self.assertIs(self.profile.fallback_models, self.module.FALLBACK_FREE_MODELS)

    def test_fallback_models_has_nine_unique_ids(self) -> None:
        self.assertEqual(len(self.module.FALLBACK_FREE_MODELS), 9)
        self.assertEqual(len(set(self.module.FALLBACK_FREE_MODELS)), 9)

    def test_fallback_models_contains_no_blocked_id(self) -> None:
        # sidecar/catalog.py FREE_LANE_BLOCKLIST: these 400/410 upstream.
        for blocked in ("exo-free", "jev-1.13-free", "ling-3.0-flash-fin-free"):
            with self.subTest(blocked=blocked):
                self.assertNotIn(blocked, self.module.FALLBACK_FREE_MODELS)

    def test_every_fallback_id_contains_free_except_big_pickle(self) -> None:
        # Documents the roster's shape: eight ids literally say "free" and
        # ``big-pickle`` is the curated exception that only pricing proves.
        for model in self.module.FALLBACK_FREE_MODELS:
            with self.subTest(model=model):
                if model == "big-pickle":
                    self.assertNotIn("free", model)
                else:
                    self.assertIn("free", model)

    def test_fallback_models_agrees_with_the_sidecar_static_roster(self) -> None:
        # The cross-module parity check BLUEPRINT §3 asks for. Both are
        # tuples that happen to be declared in different orders, so compare as
        # sets and then verify the exposed (sorted) order separately.
        from sidecar import catalog as catalog_module

        self.assertEqual(
            set(self.module.FALLBACK_FREE_MODELS),
            set(catalog_module.STATIC_FREE_MODELS),
        )
        self.assertEqual(
            list(self.module.FALLBACK_FREE_MODELS),
            catalog_module.Catalog("https://example.invalid/v1", None).models(),
        )

    def test_default_aux_model_is_the_blueprint_default(self) -> None:
        self.assertEqual(self.profile.default_aux_model, "deepseek-v4.1-flash-free")

    def test_default_aux_model_is_in_the_roster(self) -> None:
        self.assertIn(self.profile.default_aux_model, self.module.FALLBACK_FREE_MODELS)

    def test_default_max_tokens_is_a_positive_int(self) -> None:
        # 8192, not a small budget: reasoning models return empty text when
        # starved (docs/GATE.md, "empty at 32 tokens").
        self.assertIsInstance(self.profile.default_max_tokens, int)
        self.assertGreaterEqual(self.profile.default_max_tokens, 1024)

    def test_default_max_tokens_value(self) -> None:
        self.assertEqual(self.profile.default_max_tokens, 8192)


class _FakeResponse:
    """Stand-in for the ``urlopen`` context manager the plugin wraps.

    ``fetch_models`` uses ``with urlopen(...) as response`` and calls
    ``response.read()``, so the fake needs both a context protocol and a body.
    """

    def __init__(self, payload):
        if isinstance(payload, bytes):
            self._body = payload
        elif isinstance(payload, str):
            self._body = payload.encode("utf-8")
        else:
            self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


class FetchModelsTest(unittest.TestCase):
    """``fetch_models`` must degrade to ``None``, never raise, never dial out.

    ``urllib.request.urlopen`` is replaced for every test in this class: the
    brief forbids sockets, and a loopback connection to port 1 is still a
    socket. Patching the attribute also documents the call the plugin makes.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.module, _registered = _load_plugin()
        cls.profile = cls.module.profile

    def setUp(self) -> None:
        patcher = mock.patch.object(self.module.urllib.request, "urlopen", side_effect=self._refused)
        self.urlopen = patcher.start()
        # Stop this patcher only. ``mock.patch.stopall()`` would also tear down
        # patches started by other test modules in the same process.
        self.addCleanup(patcher.stop)

    @staticmethod
    def _refused(*args, **kwargs):
        raise OSError("connection refused (simulated)")

    def test_returns_none_for_a_non_http_base_url_without_opening_a_socket(self) -> None:
        # The ``url.startswith("http")`` guard fires before urllib is reached.
        for bad in ("not-a-url", "ftp://127.0.0.1:8799/v1", "//127.0.0.1:8799/v1"):
            with self.subTest(base_url=bad):
                self.assertIsNone(self.profile.fetch_models(base_url=bad))
        self.assertEqual(self.urlopen.call_count, 0)

    def test_base_url_falls_back_to_the_profile_value_when_blank(self) -> None:
        # ``(base_url or self.base_url or "")``: a blank override uses the
        # profile URL rather than producing a relative path.
        for blank in ("", None):
            with self.subTest(base_url=blank):
                self.urlopen.reset_mock()
                self.assertIsNone(self.profile.fetch_models(base_url=blank, timeout=0.2))
                request = self.urlopen.call_args.args[0]
                self.assertEqual(request.full_url, SIDECAR_BASE_URL + "/models")

    def test_returns_none_when_the_sidecar_is_unreachable(self) -> None:
        # A cold sidecar is the normal first-launch case and must degrade to
        # the curated roster instead of raising into the picker.
        result = self.profile.fetch_models(base_url="http://127.0.0.1:1/v1", timeout=0.2)
        self.assertIsNone(result)

    def test_unreachable_sidecar_does_not_raise(self) -> None:
        try:
            self.profile.fetch_models(base_url="http://127.0.0.1:1/v1", timeout=0.2)
        except Exception as error:  # pragma: no cover - the assertion is the point
            self.fail(f"fetch_models raised {type(error).__name__}: {error}")

    def test_request_url_is_the_sidecar_models_endpoint(self) -> None:
        self.urlopen.side_effect = self._refused
        self.profile.fetch_models()
        self.assertEqual(self.urlopen.call_args.args[0].full_url, SIDECAR_BASE_URL + "/models")

    def test_request_sends_the_public_bearer_sentinel(self) -> None:
        self.profile.fetch_models()
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer public")

    def test_explicit_api_key_replaces_the_sentinel(self) -> None:
        self.profile.fetch_models(api_key="tok_123")
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer tok_123")

    def test_request_accepts_json(self) -> None:
        self.profile.fetch_models()
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "application/json")

    def test_returns_the_live_roster_from_the_sidecar(self) -> None:
        payload = {"object": "list", "data": [{"id": "beam-free"}, {"id": "big-pickle"}]}
        self.urlopen.side_effect = None
        self.urlopen.return_value = _FakeResponse(payload)
        self.assertEqual(self.profile.fetch_models(), ["beam-free", "big-pickle"])

    def test_ignores_entries_without_a_string_id(self) -> None:
        payload = {"data": [{"id": "beam-free"}, {"id": 7}, {}, "junk", {"id": ""}]}
        self.urlopen.side_effect = None
        self.urlopen.return_value = _FakeResponse(payload)
        self.assertEqual(self.profile.fetch_models(), ["beam-free"])

    def test_returns_none_when_the_payload_is_not_an_object(self) -> None:
        self.urlopen.side_effect = None
        self.urlopen.return_value = _FakeResponse(["not", "a", "dict"])
        self.assertIsNone(self.profile.fetch_models())

    def test_returns_none_on_invalid_json(self) -> None:
        self.urlopen.side_effect = None
        self.urlopen.return_value = _FakeResponse(b"{not json")
        self.assertIsNone(self.profile.fetch_models())

    def test_fetch_models_accepts_the_documented_keyword_arguments(self) -> None:
        import inspect

        signature = inspect.signature(self.module.OpenCodeZenFreeProfile.fetch_models)
        self.assertEqual(
            list(signature.parameters),
            ["self", "api_key", "base_url", "timeout"],
        )
        for name in ("api_key", "base_url", "timeout"):
            with self.subTest(name=name):
                self.assertIsNot(signature.parameters[name].default, inspect.Parameter.empty)


class PluginManifestTest(unittest.TestCase):
    """``plugin.yaml`` sits next to the module in the pack (BLUEPRINT §4)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.path = os.path.join(os.path.dirname(PLUGIN_PATH), "plugin.yaml")
        with open(cls.path, encoding="utf-8") as handle:
            cls.text = handle.read()

    def test_manifest_exists(self) -> None:
        self.assertTrue(os.path.isfile(self.path), self.path)

    def test_manifest_names_the_provider_slug(self) -> None:
        self.assertIn(f"name: {PROVIDER_SLUG}", self.text)

    def test_manifest_kind_is_model_provider(self) -> None:
        self.assertIn("kind: model-provider", self.text)

    def test_manifest_version_matches_the_sidecar_version(self) -> None:
        from sidecar import __version__ as sidecar_version

        self.assertIn(f"version: {sidecar_version}", self.text)


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
