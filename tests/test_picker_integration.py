"""Is the config seed what admits ``opencode-zen-free`` into the WebUI picker?

This is the integration test for the claim the whole repository rests on
(``docs/BLUEPRINT.md`` §1 and §7):

    Bundling the plugin is not enough. ``api/config.py`` only admits a plugin
    provider into ``detected_providers`` when it is already credentialed **or**
    when its ``config.yaml`` entry carries a route (``_has_provider_route``).
    The anonymous lane has no credential, so the seed supplies that route.

It is a *real* test against a *real* upstream tree: it builds a throwaway
``HERMES_HOME``, installs this repository's plugin into it, writes a
``config.yaml`` with and without the §7 seed, and asks upstream
``api.config.get_available_models()`` which providers surfaced.

It SKIPS — it does not fail — when the upstream tree or a suitable interpreter
is unavailable, because a fresh clone has neither:

* upstream tree, from ``$OPENCODEZEN4HERMES_UPSTREAM_DIR``,
  ``./vendor/upstream``, then ``/tmp/hwa/vendor/upstream``;
* an interpreter that can import PyYAML **and** httpx, from
  ``$OPENCODEZEN4HERMES_TEST_PYTHON``, then ``/tmp/venv/bin/python``, then the
  running interpreter. A bare system ``python3`` has neither module.

Both knobs are the same ones ``scripts/picker_probe.py`` documents.
``make test`` stays green on a laptop without any of this; run
``python3 scripts/picker_probe.py`` for the standalone, human-reportable form.

Design notes worth keeping
--------------------------
**Subprocess isolation is mandatory, not stylistic.** Importing ``api.config``
rewrites ``sys.path`` (it prepends the upstream ``hermes-webui`` directory) and
pulls in the entire agent tree, including a plugin loader that logs
``Failed to load plugin ...`` for plugins whose deps are missing. Doing that
in-process would poison every other test in the same interpreter.

**Both arms run in the same test method.** The pair is the finding: "the group
appears" alone is satisfied by the active-provider branch, whereas "the group
appears *and* disappears without the seed" is the causal claim. Splitting them
into two methods would let one be skipped or sharded away from the other.

**Which ``model.provider`` the control uses.** The control config points
``model.provider`` at *another* provider (``anthropic``). That is deliberately
the discriminating shape: the slug can then only enter the catalogue through its
own ``providers.opencode-zen-free`` entry. The Lead's hand-run recipe pointed
``model.provider`` at the slug itself, which admits the group through
``api/config.py``'s active-provider branch (``detected_providers.add(active_provider)``)
*without* the seed — so it does not discriminate, and
``PickerProbeActiveProviderTests`` records that measured contrast. See also
``docs/REVIEW.md`` for the correction this test encodes.

**Label spelling is a set, not an equality.** Upstream renders the group's
``provider`` field two ways depending on whether the live provider-catalog
rebuild has warmed its cache:

* ``'OpenCode Zen (free)'`` — ``plugin_model_provider_display_name()``, i.e. the
  plugin's ``display_name``. Reached when the rebuild completes.
* ``'Opencode Zen Free'`` — ``_PROVIDER_DISPLAY.get(pid, pid.replace("-", " ").title())``,
  the static fallback.

Both were measured on the reference host. Asserting one spelling would make this
test fail depending on machine speed, which would be a lie about what is being
verified. The test pins the *set* and additionally pins which mechanism the
group came through (``get_providers()``'s ``key_source``), which is the part
that actually matters.

**The ids carry a routing prefix.** When the slug is not the active provider,
``_apply_provider_prefix`` emits ``@opencode-zen-free:<id>`` so Hermes can route
the model back to the plugin that surfaced it. The prefix is upstream routing,
not part of the BLUEPRINT §3 roster; the assertion is made against the bare ids
and both forms are reported on failure.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

#: Repository root (the parent of ``tests/``).
REPO_ROOT = Path(__file__).resolve().parent.parent
#: This test's skip-guard knobs, shared with ``scripts/picker_probe.py``.
ENV_UPSTREAM = "OPENCODEZEN4HERMES_UPSTREAM_DIR"
ENV_PYTHON = "OPENCODEZEN4HERMES_TEST_PYTHON"
#: Set to a truthy value to run the slow ``api.providers.get_providers()`` cross-check.
#: On the reference host that call alone took 13-15s per arm because it probes
#: every configured provider for keys.
ENV_PROVIDERS_CHECK = "OPENCODEZEN4HERMES_PICKER_PROVIDERS_CHECK"

#: The slug under test. BLUEPRINT §2.
PROVIDER_SLUG = "opencode-zen-free"
#: The plugin's ``display_name``. BLUEPRINT §2.
PROVIDER_LABEL = "OpenCode Zen (free)"
#: The static-catalogue spelling (``_PROVIDER_DISPLAY.get(pid, slug.title())``).
PROVIDER_LABEL_STATIC = "Opencode Zen Free"
#: Accepted spellings for the group label; see the module docstring.
ACCEPTED_LABELS: tuple[str, ...] = (PROVIDER_LABEL, PROVIDER_LABEL_STATIC)
#: The nine free ids in picker order. BLUEPRINT §3.
EXPECTED_MODELS: tuple[str, ...] = (
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
#: The provider the control config points ``model.provider`` at, so the slug
#: cannot be admitted by the active-provider branch.
CONTROL_PROVIDER = "anthropic"


def _load_probe_module():
    """Import ``scripts/picker_probe.py`` by path.

    The test and the standalone probe must agree on discovery, config shapes and
    the verdict, so there is exactly one implementation and it lives in
    ``scripts/``. Importing it here is safe: the module touches ``sys.path`` only
    inside the source it hands to a subprocess. It is loaded by path rather than
    as a package module because ``scripts/`` is deliberately not a package.
    """
    path = REPO_ROOT / "scripts" / "picker_probe.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_picker_probe", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_picker_probe", module)
    spec.loader.exec_module(module)
    return module


PROBE = _load_probe_module()


def _probe_missing() -> str | None:
    """Return why the probe module itself is unusable, or ``None``."""
    if PROBE is None:
        return f"scripts/picker_probe.py is missing from {REPO_ROOT}; the integration test needs it"
    return None


def _skip_reason() -> str | None:
    """Return why the *integration* arm cannot run, or ``None`` when it can.

    Deliberately separate from :func:`_probe_missing`: the contract tests below
    only need the probe module and must keep running on a bare checkout, where
    there is no upstream tree and no interpreter with the upstream deps.
    """
    if _probe_missing() is not None:
        return _probe_missing()
    if PROBE.find_upstream(None) is None:
        return (
            "no upstream hermes-webui + hermes-agent tree available, so the picker cannot be "
            f"queried. Set {ENV_UPSTREAM}=/path/to/vendor/upstream (it must contain "
            "hermes-webui/api/config.py and hermes-agent/providers/), or place the tree at "
            "./vendor/upstream, or run this test where /tmp/hwa/vendor/upstream exists. "
            "Run `python3 scripts/picker_probe.py` for the standalone probe."
        )
    python, notes = PROBE.find_interpreter(None)
    if not python:
        return (
            "no interpreter available that can import the modules the upstream tree needs "
            f"({' + '.join(PROBE.REQUIRED_MODULES)}). Set {ENV_PYTHON}=/path/to/python, or install "
            "PyYAML and httpx into the interpreter running this test. Tried: " + "; ".join(notes)
        )
    return None


SKIP_REASON = _skip_reason()

#: How long one probe arm may take. Upstream's cold provider-catalog rebuild
#: probes provider endpoints; on the reference host one arm with the
#: ``get_providers()`` cross-check enabled took ~15s.
ARM_TIMEOUT = float(os.environ.get("OPENCODEZEN4HERMES_PICKER_TIMEOUT") or 300.0)

#: The ``get_providers()`` cross-check is off by default: it re-probes every
#: configured provider and costs 13-15s per arm for evidence that duplicates what
#: the catalogue already proves. Opt in with the env var.
RUN_PROVIDERS_CHECK = (
    os.environ.get(ENV_PROVIDERS_CHECK, "").strip().lower() in {"1", "true", "yes", "on"}
)


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class PickerSeedIntegrationTests(unittest.TestCase):
    """The seed is load-bearing: with it the group appears, without it does not."""

    @classmethod
    def setUpClass(cls) -> None:
        assert PROBE is not None  # guaranteed by the class decorator
        cls.upstream = PROBE.find_upstream(None)
        cls.python, _ = PROBE.find_interpreter(None)
        cls._arms: dict[bool, object] = {}

    def _arm(self, seeded: bool):
        """Run (and memoise) one probe arm, skipping on an unrunnable probe."""
        if seeded in self._arms:
            return self._arms[seeded]
        outcome = PROBE.run_probe(
            upstream=self.upstream,
            python=self.python,
            seeded=seeded,
            active_provider=CONTROL_PROVIDER,
            with_providers=RUN_PROVIDERS_CHECK,
            timeout=ARM_TIMEOUT,
        )
        if not outcome.ok:
            self.skipTest(
                "the upstream tree could not be queried, so this integration test cannot run: "
                f"{outcome.error}"
            )
        self._arms[seeded] = outcome
        return outcome

    # ── the load-bearing positive ────────────────────────────────────────────

    def test_seed_admits_the_provider_into_the_picker(self) -> None:
        """With the BLUEPRINT §7 seed, the group exists with all nine ids in order."""
        outcome = self._arm(seeded=True)

        self.assertIsNotNone(
            outcome.seeded_group,
            msg=(
                f"'{PROVIDER_SLUG}' did not reach get_available_models() even though the "
                f"BLUEPRINT §7 seed was written; groups seen: {outcome.group_ids}"
            ),
        )
        self.assertEqual(
            outcome.seeded_model_ids,
            list(EXPECTED_MODELS),
            msg=(
                "the admitted group does not carry the BLUEPRINT §3 roster in order. "
                f"raw ids from upstream: {outcome.seeded_model_ids_raw}"
            ),
        )
        self.assertIn(
            outcome.seeded_label,
            ACCEPTED_LABELS,
            msg=(
                f"the group label {outcome.seeded_label!r} is neither the plugin display name "
                f"{PROVIDER_LABEL!r} nor the static-catalogue spelling {PROVIDER_LABEL_STATIC!r}"
            ),
        )
        self.assertTrue(
            outcome.is_plugin_model_provider,
            msg=(
                "upstream does not recognise the slug as a plugin model provider, so the group "
                "cannot have come from the installed plugin"
            ),
        )

    # ── the negative control: equally load-bearing ────────────────────────────

    def test_without_the_seed_the_group_does_not_appear(self) -> None:
        """The same setup without the seed yields no group — the seed is the cause.

        This is the arm that makes the positive arm mean anything. It runs with
        ``model.provider`` pointing at another provider, so the slug has no other
        route into the catalogue.
        """
        outcome = self._arm(seeded=False)

        self.assertIsNone(
            outcome.seeded_group,
            msg=(
                f"negative control failed: '{PROVIDER_SLUG}' surfaced WITHOUT the config seed, "
                "so this test would pass even if the seed did nothing. "
                f"groups seen: {outcome.group_ids}; "
                f"label: {outcome.seeded_label!r}. Check that the control config keeps "
                f"model.provider={CONTROL_PROVIDER!r} (pointing it at the slug admits the group "
                "through api/config.py's active-provider branch, seed or no seed)."
            ),
        )
        self.assertNotIn(
            PROVIDER_SLUG,
            outcome.group_ids,
            msg=f"'{PROVIDER_SLUG}' appeared in group_ids: {outcome.group_ids}",
        )

    def test_the_two_arms_differ_only_by_the_seed(self) -> None:
        """Both arms ran against the same tree with the same config except the seed.

        A guard against a future edit that makes the arms incomparable (different
        upstream, different interpreter, different active provider) and thereby
        turns the negative control into no evidence at all.
        """
        with_seed = self._arm(seeded=True)
        without_seed = self._arm(seeded=False)

        self.assertEqual(with_seed.upstream, without_seed.upstream)
        self.assertEqual(with_seed.python, without_seed.python)
        self.assertEqual(with_seed.active_provider, without_seed.active_provider)
        self.assertEqual(
            with_seed.active_provider_reported,
            without_seed.active_provider_reported,
            msg=(
                "the two arms resolved different active providers "
                f"({with_seed.active_provider_reported!r} vs "
                f"{without_seed.active_provider_reported!r}), so the negative control does not "
                "isolate the seed"
            ),
        )
        # The seed is the only difference in the generated config text: the
        # seeded arm is the control arm plus the appended block, nothing else.
        seeded_text = PROBE.probe_config_text(seeded=True, active_provider=CONTROL_PROVIDER)
        control_text = PROBE.probe_config_text(seeded=False, active_provider=CONTROL_PROVIDER)
        self.assertNotEqual(seeded_text, control_text)
        self.assertTrue(
            seeded_text.startswith(control_text),
            msg="the seeded arm is not the control arm with the seed block appended",
        )
        appended = seeded_text[len(control_text):]
        self.assertNotIn(
            CONTROL_PROVIDER,
            appended,
            msg=f"the appended block touches another provider's settings: {appended!r}",
        )

    @unittest.skipUnless(
        RUN_PROVIDERS_CHECK,
        f"secondary evidence; set {ENV_PROVIDERS_CHECK}=1 to enable (it probes every "
        "configured provider and costs ~15s per arm)",
    )
    def test_provider_row_reports_the_seed_as_its_key_source(self) -> None:
        """``api.providers.get_providers()`` attributes the route to config.yaml.

        The catalogue proves the slug is *listed*; this proves upstream believes
        it is listed **because** a route was seeded — ``has_key: True`` with
        ``key_source: 'config_yaml'`` comes from
        ``api/config.py``'s ``providers.<slug>`` branch, and ``models_total``
        matches the nine. It is guarded behind an env var because
        ``get_providers()`` probes every configured provider and is slow.
        """
        outcome = self._arm(seeded=True)

        if outcome.providers_error:
            self.skipTest(f"api.providers.get_providers() is unavailable here: {outcome.providers_error}")

        self.assertTrue(
            outcome.provider_row_found,
            msg=f"get_providers() has no row for '{PROVIDER_SLUG}'",
        )
        row = outcome.provider_row or {}
        self.assertIs(
            row.get("has_key"),
            True,
            msg=f"has_key was {row.get('has_key')!r}; the seed should make it True",
        )
        self.assertEqual(
            row.get("key_source"),
            "config_yaml",
            msg=(
                f"key_source was {row.get('key_source')!r}, not 'config_yaml'; the route is not "
                "being attributed to the seeded providers.<slug> entry"
            ),
        )
        self.assertEqual(row.get("models_total"), len(EXPECTED_MODELS))


@unittest.skipIf(
    _probe_missing() is not None,
    _probe_missing() or "",
)
class PickerProbeContractTests(unittest.TestCase):
    """The probe's own contract, checked without touching the upstream tree.

    Skips only when the probe module itself is missing. It deliberately does NOT
    inherit the upstream/interpreter skip guard: these checks are pure text and
    must keep running on a bare checkout, where they are the only thing standing
    between a stale roster here and a silently vacuous integration test.
    """

    def test_expected_models_match_the_blueprint_roster(self) -> None:
        """The ids this test asserts are BLUEPRINT §3, read back from the document."""
        blueprint = (REPO_ROOT / "docs" / "BLUEPRINT.md").read_text(encoding="utf-8")
        section = blueprint.split("## 3. The free roster", 1)
        self.assertEqual(len(section), 2, msg="BLUEPRINT.md has no '## 3. The free roster' section")
        body = section[1].split("```")[1]
        documented = tuple(body.split())
        self.assertEqual(
            documented,
            EXPECTED_MODELS,
            msg=(
                "the roster in BLUEPRINT §3 and EXPECTED_MODELS in this test disagree; one of "
                "them is stale. BLUEPRINT is the contract (BLUEPRINT.md §0: change it first)."
            ),
        )
        self.assertEqual(tuple(PROBE.EXPECTED_MODELS), EXPECTED_MODELS)

    def test_probe_and_test_agree_on_the_seed_shape(self) -> None:
        """The probe writes exactly the §7 block, and nothing else changes."""
        seeded = PROBE.probe_config_text(seeded=True, active_provider=CONTROL_PROVIDER)
        control = PROBE.probe_config_text(seeded=False, active_provider=CONTROL_PROVIDER)

        for key in ("name", "base_url", "api_key", "models_discovered"):
            self.assertIn(f"{key}:", seeded, msg=f"the seeded config omits {key!r}")
        self.assertIn(f"name: {PROVIDER_LABEL}", seeded)
        self.assertIn(f"base_url: {PROBE.SIDECAR_BASE_URL}", seeded)
        self.assertIn(f"api_key: {PROBE.PUBLIC_KEY_SENTINEL}", seeded)
        self.assertIn("models_discovered: false", seeded)

        self.assertNotIn(PROVIDER_SLUG, control)
        self.assertIn(PROVIDER_SLUG, seeded)
        self.assertIn(f"provider: {CONTROL_PROVIDER}", control)
        self.assertIn(
            "api_key: sk-not-real",
            control,
            msg="the control must keep another provider credentialed so the catalogue is not empty",
        )

    def test_control_config_does_not_make_the_slug_the_active_provider(self) -> None:
        """Guards the property that makes the negative control discriminating."""
        text = PROBE.probe_config_text(seeded=False, active_provider=CONTROL_PROVIDER)
        active_lines = [
            line.strip() for line in text.splitlines() if line.strip().startswith("provider:")
        ]
        self.assertEqual(active_lines, [f"provider: {CONTROL_PROVIDER}"])
        self.assertTrue(PROBE._negative_control_is_meaningful(CONTROL_PROVIDER))
        self.assertFalse(PROBE._negative_control_is_meaningful(PROVIDER_SLUG))

    def test_the_probe_never_reads_the_live_sidecar(self) -> None:
        """The picker decision comes from the seed, so the probe must not need 8799.

        The config seed's ``base_url`` appears in the generated YAML, but nothing
        in this path may open a socket to it: the catalogue is built from local
        config only. Asserted structurally — the probe's subprocess source must
        not import an HTTP client to reach the sidecar.
        """
        source = PROBE._SANDBOX
        for forbidden in ("8799", "sidecar", "urllib", "httpx", "requests"):
            self.assertNotIn(
                forbidden,
                source,
                msg=(
                    f"the probe's upstream-side source mentions {forbidden!r}; the picker "
                    "assertion must read the config seed, not the live sidecar"
                ),
            )

    def test_find_upstream_rejects_a_directory_without_the_tree(self) -> None:
        """Discovery validates the tree's shape, not just that a directory exists.

        A directory that merely *exists* must not be accepted — otherwise the
        integration test would run against nothing and report a confident, empty
        result instead of skipping.

        The default candidate list is neutralised for the duration so the result
        depends only on the tree handed in, not on whether this host happens to
        have the reference tree at ``/tmp/hwa/vendor/upstream``.
        """
        import tempfile
        from unittest import mock

        with mock.patch.object(PROBE, "DEFAULT_UPSTREAM_CANDIDATES", ()), mock.patch.dict(
            os.environ, {ENV_UPSTREAM: ""}, clear=False
        ):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertIsNone(PROBE.find_upstream(tmp))
                self.assertIsNone(
                    PROBE.find_upstream(None),
                    msg="with no candidates and no env override there is nothing to find",
                )

                # Only ``config.py``: still not a usable tree.
                (Path(tmp) / "hermes-webui" / "api").mkdir(parents=True)
                (Path(tmp) / "hermes-webui" / "api" / "config.py").write_text("", encoding="utf-8")
                self.assertIsNone(
                    PROBE.find_upstream(tmp),
                    msg="a tree with config.py but no hermes-agent/providers/ must be rejected",
                )

                # The second half of the contract: the agent tree.
                (Path(tmp) / "hermes-agent" / "providers").mkdir(parents=True)
                self.assertEqual(PROBE.find_upstream(tmp), Path(tmp).resolve())

    def test_find_upstream_falls_back_past_an_unusable_candidate(self) -> None:
        """An explicit directory that is not a tree falls through to the defaults."""
        import tempfile
        from unittest import mock

        found = PROBE.find_upstream(None)
        if found is None:
            self.skipTest("no upstream tree on this host, so the fallback has nowhere to land")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(PROBE, "DEFAULT_UPSTREAM_CANDIDATES", (str(found),)):
                self.assertEqual(PROBE.find_upstream(tmp), found.resolve())

    def test_interpreter_rejects_a_path_that_is_not_executable(self) -> None:
        """A bad interpreter path yields a reason string, never an exception."""
        problem = PROBE.interpreter_error("/nonexistent/python-does-not-exist")
        self.assertIsNotNone(problem)
        self.assertIn("cannot execute", problem or "")

    def test_interpreter_error_matches_the_running_interpreter(self) -> None:
        """``interpreter_error`` agrees with whether this interpreter has the deps."""
        problem = PROBE.interpreter_error(sys.executable)
        has_deps = all(
            importlib.util.find_spec(name) is not None for name in PROBE.REQUIRED_MODULES
        )
        if has_deps:
            self.assertIsNone(problem)
        else:
            self.assertIsNotNone(problem)
            self.assertIn("cannot import", problem or "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
