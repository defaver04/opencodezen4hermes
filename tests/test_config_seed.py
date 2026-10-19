"""Tests for ``install/seed_config.py`` — the step that makes the picker admit us.

This module is the least glamorous part of the repo and the most load-bearing:
``docs/BLUEPRINT.md`` §1 records that bundling the plugin WITHOUT the seed yields
``groups: []`` in ``get_available_models()``, because ``api/config.py`` only
admits a plugin provider into ``detected_providers`` when its config entry
carries a route. No seed, no dropdown.

So the properties tested here are the ones that decide whether a user's picker
works *and* whether we destroy their config trying:

* merge-only — other providers, other top-level keys, comments and layout survive;
* idempotent — a second run is a byte-identical no-op with no new backup;
* honest about drift — a stale port is reported, not silently rewritten without
  ``--force``;
* atomic — a failure leaves the original file untouched, never half-written;
* safe on garbage — invalid YAML or a non-mapping ``providers`` returns
  ``failed`` rather than clobbering anything.

``unittest`` only, and PyYAML is an optional dependency: the module under test
guards its import because a bare system ``python3`` usually lacks it, and CI
installs nothing. Every test that needs a real parse skips with a clear message
when ``yaml`` is unavailable, rather than silently asserting nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from install import seed_config as sc  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the interpreter
    yaml = None  # type: ignore[assignment]

_REQUIRES_YAML = unittest.skipUnless(
    sc.HAS_YAML, "PyYAML is not importable; the seed cannot write YAML here"
)

_EXPECTED_BASE_URL = "http://127.0.0.1:8799/v1"
_SLUG = "opencode-zen-free"


class _TempConfigCase(unittest.TestCase):
    """Base case giving each test a private directory and clean env vars."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config = self.tmp / "config.yaml"

        # The resolution chain reads the environment; a developer's own
        # HERMES_HOME must not leak into these tests.
        for name in ("HERMES_CONFIG_PATH", "HERMES_HOME"):
            previous = os.environ.pop(name, None)
            if previous is not None:
                self.addCleanup(os.environ.__setitem__, name, previous)

    def seed(self, **kwargs):
        kwargs.setdefault("path", self.config)
        return sc.seed_config(**kwargs)

    def load(self) -> dict:
        return yaml.safe_load(self.config.read_text(encoding="utf-8"))


# ── Contract constants ───────────────────────────────────────────────────────


class TestContractConstants(unittest.TestCase):
    """The identifiers BLUEPRINT §2 fixes, asserted where they are declared."""

    def test_slug_and_display_name_match_the_blueprint(self) -> None:
        self.assertEqual(sc.PROVIDER_SLUG, "opencode-zen-free")
        self.assertEqual(sc.PROVIDER_NAME, "OpenCode Zen (free)")

    def test_default_port_matches_the_sidecar(self) -> None:
        from sidecar.config import DEFAULT_PORT

        self.assertEqual(sc.DEFAULT_PORT, DEFAULT_PORT, "seed and sidecar disagree on the port")

    def test_managed_keys_are_the_four_from_the_blueprint(self) -> None:
        self.assertEqual(
            sc.MANAGED_KEYS, ("name", "base_url", "api_key", "models_discovered")
        )

    def test_api_key_is_the_documented_public_sentinel(self) -> None:
        # Not a secret: the lane is anonymous and ignores the header's value.
        self.assertEqual(sc.PUBLIC_KEY_SENTINEL, "public")

    def test_build_block_has_the_exact_shape(self) -> None:
        block = sc.build_block()
        self.assertEqual(
            block,
            {
                "name": "OpenCode Zen (free)",
                "base_url": _EXPECTED_BASE_URL,
                "api_key": "public",
                "models_discovered": False,
            },
        )

    def test_build_block_honours_a_custom_port(self) -> None:
        self.assertEqual(sc.build_block(9123)["base_url"], "http://127.0.0.1:9123/v1")

    def test_models_discovered_false_pins_the_roster(self) -> None:
        """BLUEPRINT §7: false keeps Hermes from replacing our nine with its probe.

        ``api/config.py::_provider_models_are_discovered_catalog`` only treats a
        provider as discovery-driven when this flag is true, so ``False`` is what
        makes the nine-model roster durable rather than best-effort.
        """
        self.assertIs(sc.build_block()["models_discovered"], False)


# ── Path resolution ──────────────────────────────────────────────────────────


class TestResolveConfigPath(_TempConfigCase):
    """The chain mirrors ``api/config.py::_get_config_path``."""

    def test_explicit_path_wins(self) -> None:
        os.environ["HERMES_CONFIG_PATH"] = str(self.tmp / "env.yaml")
        os.environ["HERMES_HOME"] = str(self.tmp / "home")
        self.assertEqual(
            sc.resolve_config_path(self.tmp / "explicit.yaml"), self.tmp / "explicit.yaml"
        )

    def test_env_config_path_beats_hermes_home(self) -> None:
        os.environ["HERMES_CONFIG_PATH"] = str(self.tmp / "env.yaml")
        os.environ["HERMES_HOME"] = str(self.tmp / "home")
        self.assertEqual(sc.resolve_config_path(), self.tmp / "env.yaml")

    def test_hermes_home_is_used_when_config_path_is_absent(self) -> None:
        os.environ["HERMES_HOME"] = str(self.tmp / "home")
        self.assertEqual(sc.resolve_config_path(), self.tmp / "home" / "config.yaml")

    def test_fallback_is_dot_hermes_in_the_home_directory(self) -> None:
        self.assertEqual(sc.resolve_config_path(), Path.home() / ".hermes" / "config.yaml")

    def test_blank_explicit_path_is_ignored(self) -> None:
        os.environ["HERMES_HOME"] = str(self.tmp / "home")
        self.assertEqual(sc.resolve_config_path("   "), self.tmp / "home" / "config.yaml")

    def test_tilde_is_expanded(self) -> None:
        self.assertEqual(
            sc.resolve_config_path("~/x.yaml"), Path.home() / "x.yaml"
        )


# ── Fresh file ───────────────────────────────────────────────────────────────


@_REQUIRES_YAML
class TestFreshFile(_TempConfigCase):
    def test_creates_the_file_and_reports_created(self) -> None:
        result = self.seed()
        self.assertEqual(result.status, sc.STATUS_CREATED)
        self.assertTrue(result.changed)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(self.config.is_file())

    def test_written_document_has_exactly_the_seed(self) -> None:
        self.seed()
        self.assertEqual(
            self.load(),
            {
                "providers": {
                    _SLUG: {
                        "name": "OpenCode Zen (free)",
                        "base_url": _EXPECTED_BASE_URL,
                        "api_key": "public",
                        "models_discovered": False,
                    }
                }
            },
        )

    def test_creates_missing_parent_directories(self) -> None:
        nested = self.tmp / "a" / "b" / "config.yaml"
        result = sc.seed_config(nested)
        self.assertEqual(result.status, sc.STATUS_CREATED)
        self.assertTrue(nested.is_file())

    def test_no_backup_is_made_when_there_was_no_file(self) -> None:
        self.seed()
        self.assertEqual(list(self.tmp.glob("config.yaml.bak.*")), [])

    def test_an_empty_file_is_treated_as_a_fresh_file(self) -> None:
        self.config.write_text("", encoding="utf-8")
        result = self.seed()
        self.assertEqual(result.status, sc.STATUS_CREATED)
        self.assertIn(_SLUG, self.load()["providers"])


# ── Merge behaviour: the part that must not be rude ──────────────────────────


@_REQUIRES_YAML
class TestMergePreservesUserContent(_TempConfigCase):
    """Adding a provider must never cost the user anything they wrote."""

    def write_config(self, text: str) -> None:
        self.config.write_text(text, encoding="utf-8")

    def test_other_providers_survive_verbatim(self) -> None:
        self.write_config(
            "providers:\n"
            "  openai:\n"
            "    api_key: sk-real\n"
            "  anthropic:\n"
            "    api_key: sk-ant\n"
        )
        result = self.seed()
        self.assertEqual(result.status, sc.STATUS_CREATED)
        providers = self.load()["providers"]
        self.assertEqual(providers["openai"], {"api_key": "sk-real"})
        self.assertEqual(providers["anthropic"], {"api_key": "sk-ant"})
        self.assertEqual(providers[_SLUG]["base_url"], _EXPECTED_BASE_URL)

    def test_top_level_keys_survive(self) -> None:
        self.write_config(
            "model:\n"
            "  provider: openai\n"
            "  name: gpt-5\n"
            "providers:\n"
            "  openai:\n"
            "    api_key: sk-real\n"
        )
        self.seed()
        doc = self.load()
        self.assertEqual(doc["model"], {"provider": "openai", "name": "gpt-5"})
        self.assertIn(_SLUG, doc["providers"])

    def test_comments_are_preserved(self) -> None:
        """The whole reason a surgical edit exists instead of a ``safe_dump``."""
        self.write_config(
            "# my hand-written config\n"
            "providers:\n"
            "  # the real one\n"
            "  openai:\n"
            "    api_key: sk-real\n"
        )
        self.seed()
        text = self.config.read_text(encoding="utf-8")
        self.assertIn("# my hand-written config", text)
        self.assertIn("# the real one", text)

    def test_a_backup_is_taken_on_the_first_modification(self) -> None:
        original = "providers:\n  openai:\n    api_key: sk-real\n"
        self.write_config(original)
        result = self.seed()
        self.assertIsNotNone(result.backup_path)
        backups = list(self.tmp.glob("config.yaml.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)

    def test_an_empty_providers_flow_mapping_is_filled_in(self) -> None:
        self.write_config("providers: {}\n")
        result = self.seed()
        self.assertEqual(result.status, sc.STATUS_CREATED)
        self.assertIn(_SLUG, self.load()["providers"])

    def test_a_bare_providers_key_is_filled_in(self) -> None:
        self.write_config("providers:\n")
        result = self.seed()
        self.assertEqual(result.status, sc.STATUS_CREATED)
        self.assertIn(_SLUG, self.load()["providers"])

    def test_extra_keys_inside_our_entry_survive_creation(self) -> None:
        """A user who half-wrote the entry keeps whatever they added."""
        self.write_config(
            "providers:\n"
            f"  {_SLUG}:\n"
            "    note: mine\n"
        )
        self.seed()
        entry = self.load()["providers"][_SLUG]
        self.assertEqual(entry["note"], "mine")
        self.assertEqual(entry["base_url"], _EXPECTED_BASE_URL)


# ── Idempotence ──────────────────────────────────────────────────────────────


@_REQUIRES_YAML
class TestIdempotence(_TempConfigCase):
    def test_second_run_is_unchanged_and_byte_identical(self) -> None:
        self.seed()
        before = self.config.read_bytes()
        mtime = self.config.stat().st_mtime_ns

        result = self.seed()

        self.assertEqual(result.status, sc.STATUS_UNCHANGED)
        self.assertFalse(result.changed)
        self.assertEqual(self.config.read_bytes(), before)
        # Not rewritten at all: the WebUI memoises on (mtime_ns, size), so a
        # churning rewrite would invalidate its cache for nothing.
        self.assertEqual(self.config.stat().st_mtime_ns, mtime)

    def test_second_run_does_not_add_a_second_backup(self) -> None:
        self.config.write_text("providers:\n  openai:\n    api_key: sk\n", encoding="utf-8")
        self.seed()
        self.seed()
        self.assertEqual(len(list(self.tmp.glob("config.yaml.bak.*"))), 1)

    def test_unchanged_message_names_the_seeded_base_url(self) -> None:
        self.seed()
        self.assertIn(_EXPECTED_BASE_URL, self.seed().message)


# ── Drift and --force ────────────────────────────────────────────────────────


@_REQUIRES_YAML
class TestDrift(_TempConfigCase):
    """A stale port must be reported, not silently rewritten."""

    def write_drifted(self, port: int = 1111) -> None:
        self.config.write_text(
            "providers:\n"
            f"  {_SLUG}:\n"
            "    name: OpenCode Zen (free)\n"
            f"    base_url: http://127.0.0.1:{port}/v1\n"
            "    api_key: public\n"
            "    models_discovered: false\n",
            encoding="utf-8",
        )

    def test_drift_without_force_is_reported_and_left_alone(self) -> None:
        self.write_drifted()
        before = self.config.read_bytes()

        result = self.seed()

        self.assertEqual(result.status, sc.STATUS_UNCHANGED)
        self.assertFalse(result.changed)
        self.assertEqual(result.exit_code, 0, "drift is a warning, not a failure")
        self.assertIn("1111", result.message)
        self.assertIn("--force", result.message)
        self.assertEqual(self.config.read_bytes(), before)

    def test_force_rewrites_the_managed_base_url(self) -> None:
        self.write_drifted()
        result = self.seed(force=True)
        self.assertEqual(result.status, sc.STATUS_UPDATED)
        self.assertTrue(result.changed)
        self.assertEqual(self.load()["providers"][_SLUG]["base_url"], _EXPECTED_BASE_URL)

    def test_force_keeps_comments(self) -> None:
        """--force resets managed keys; it must not cost the user their comments."""
        self.config.write_text(
            "# keep me\n"
            "providers:\n"
            f"  {_SLUG}:\n"
            "    # also keep me\n"
            "    base_url: http://127.0.0.1:1111/v1\n",
            encoding="utf-8",
        )
        self.seed(force=True)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn("# keep me", text)
        self.assertIn("# also keep me", text)
        self.assertEqual(self.load()["providers"][_SLUG]["base_url"], _EXPECTED_BASE_URL)

    def test_force_keeps_other_providers(self) -> None:
        self.config.write_text(
            "providers:\n"
            "  openai:\n"
            "    api_key: sk-real\n"
            f"  {_SLUG}:\n"
            "    base_url: http://127.0.0.1:1111/v1\n",
            encoding="utf-8",
        )
        self.seed(force=True)
        self.assertEqual(self.load()["providers"]["openai"], {"api_key": "sk-real"})

    def test_a_different_port_is_seeded_without_force_on_a_fresh_file(self) -> None:
        result = self.seed(port=9123)
        self.assertEqual(result.status, sc.STATUS_CREATED)
        self.assertEqual(
            self.load()["providers"][_SLUG]["base_url"], "http://127.0.0.1:9123/v1"
        )


# ── Dry run ──────────────────────────────────────────────────────────────────


@_REQUIRES_YAML
class TestDryRun(_TempConfigCase):
    def test_writes_nothing_when_the_file_is_absent(self) -> None:
        result = self.seed(dry_run=True)
        self.assertEqual(result.status, sc.STATUS_CREATED)
        self.assertTrue(result.changed)
        self.assertTrue(result.dry_run)
        self.assertFalse(self.config.exists(), "--dry-run created a file")
        self.assertEqual(list(self.tmp.glob("config.yaml.bak.*")), [])

    def test_carries_the_dry_run_marker_in_its_message(self) -> None:
        self.assertIn("dry run", self.seed(dry_run=True).message)

    def test_describe_says_would_rather_than_did(self) -> None:
        self.assertIn("would", self.seed(dry_run=True).describe())

    def test_leaves_an_existing_file_byte_identical(self) -> None:
        self.config.write_text("providers:\n  openai:\n    api_key: sk\n", encoding="utf-8")
        before = self.config.read_bytes()
        self.seed(dry_run=True)
        self.assertEqual(self.config.read_bytes(), before)

    def test_does_not_create_the_parent_directory(self) -> None:
        nested = self.tmp / "nope" / "config.yaml"
        sc.seed_config(nested, dry_run=True)
        self.assertFalse((self.tmp / "nope").exists())


# ── Failure modes: refuse, never clobber ─────────────────────────────────────


@_REQUIRES_YAML
class TestRefusesBadInput(_TempConfigCase):
    def test_invalid_yaml_fails_and_leaves_the_file_untouched(self) -> None:
        broken = "providers: [unclosed\n  - :\n"
        self.config.write_text(broken, encoding="utf-8")

        result = self.seed()

        self.assertEqual(result.status, sc.STATUS_FAILED)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.config.read_text(encoding="utf-8"), broken)

    def test_a_non_mapping_document_fails(self) -> None:
        self.config.write_text("- one\n- two\n", encoding="utf-8")
        result = self.seed()
        self.assertEqual(result.status, sc.STATUS_FAILED)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "- one\n- two\n")

    def test_a_non_mapping_providers_key_fails(self) -> None:
        self.config.write_text("providers:\n  - a\n  - b\n", encoding="utf-8")
        result = self.seed()
        self.assertEqual(result.status, sc.STATUS_FAILED)
        self.assertIn("providers", result.message)

    def test_a_scalar_entry_under_our_slug_fails_rather_than_being_overwritten(self) -> None:
        self.config.write_text(f"providers:\n  {_SLUG}: just-a-string\n", encoding="utf-8")
        result = self.seed()
        self.assertEqual(result.status, sc.STATUS_FAILED)
        self.assertIn("just-a-string", self.config.read_text(encoding="utf-8"))

    def test_no_backup_is_left_behind_on_failure(self) -> None:
        self.config.write_text("providers: [unclosed\n", encoding="utf-8")
        self.seed()
        self.assertEqual(list(self.tmp.glob("config.yaml.bak.*")), [])

    def test_a_failed_result_exits_non_zero(self) -> None:
        self.config.write_text("providers: [unclosed\n", encoding="utf-8")
        self.assertEqual(self.seed().exit_code, 1)


# ── Atomicity ────────────────────────────────────────────────────────────────


@_REQUIRES_YAML
class TestAtomicWrite(_TempConfigCase):
    def test_no_temp_file_survives_a_successful_write(self) -> None:
        self.seed()
        self.assertEqual(list(self.tmp.glob(".seed-config-*")), [])

    def test_an_existing_file_mode_is_preserved(self) -> None:
        self.config.write_text("providers:\n  openai:\n    api_key: sk\n", encoding="utf-8")
        os.chmod(self.config, 0o600)
        self.seed()
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_a_write_error_is_reported_not_raised(self) -> None:
        """A read-only directory must produce ``failed``, not a traceback."""
        if os.geteuid() == 0:
            self.skipTest("running as root: directory permissions are not enforced")
        target = self.tmp / "ro" / "config.yaml"
        (self.tmp / "ro").mkdir()
        os.chmod(self.tmp / "ro", 0o500)
        self.addCleanup(os.chmod, self.tmp / "ro", 0o700)
        result = sc.seed_config(target)
        self.assertEqual(result.status, sc.STATUS_FAILED)
        self.assertFalse(target.exists())


# ── CLI ──────────────────────────────────────────────────────────────────────


class TestCli(_TempConfigCase):
    """Exit codes matter: the launcher treats non-zero as a warning only."""

    def run_cli(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_REPO_ROOT / "install" / "seed_config.py"), *argv],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_help_documents_the_resolution_chain_and_exit_codes(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("HERMES_CONFIG_PATH", result.stdout)
        self.assertIn("--force", result.stdout)
        self.assertIn("Exit 0", result.stdout)

    def test_port_out_of_range_is_rejected(self) -> None:
        result = self.run_cli("--config", str(self.config), "--port", "70000")
        self.assertEqual(result.returncode, 1)
        self.assertIn("1..65535", result.stderr)

    @_REQUIRES_YAML
    def test_dry_run_exits_zero_and_writes_nothing(self) -> None:
        result = self.run_cli("--config", str(self.config), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry run", result.stdout)
        self.assertFalse(self.config.exists())

    @_REQUIRES_YAML
    def test_a_real_cli_run_creates_the_seed(self) -> None:
        result = self.run_cli("--config", str(self.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            yaml.safe_load(self.config.read_text(encoding="utf-8"))["providers"][_SLUG][
                "base_url"
            ],
            _EXPECTED_BASE_URL,
        )

    @_REQUIRES_YAML
    def test_the_cli_is_idempotent(self) -> None:
        self.run_cli("--config", str(self.config))
        before = self.config.read_bytes()
        result = self.run_cli("--config", str(self.config))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)


class TestWithoutYaml(unittest.TestCase):
    """A bare interpreter must degrade to a clear skip, never a traceback."""

    def test_the_module_imports_without_yaml(self) -> None:
        # Importing must not explode; HAS_YAML is what callers branch on.
        self.assertIsInstance(sc.HAS_YAML, bool)

    @unittest.skipIf(sc.HAS_YAML, "PyYAML IS importable here; nothing to test")
    def test_seeding_reports_skipped_and_touches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.yaml"
            result = sc.seed_config(target)
            self.assertEqual(result.status, sc.STATUS_SKIPPED)
            self.assertFalse(target.exists())
            self.assertIn("venv", result.message)


if __name__ == "__main__":
    unittest.main()
