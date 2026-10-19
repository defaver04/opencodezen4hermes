"""Tests for ``install/pack_install.py`` (BLUEPRINT §4, §6).

Standard library ``unittest`` only — the pack ships no test runner (BLUEPRINT §8).

Everything here runs against **synthetic** pack trees built in ``tempfile``
directories: no network, no sockets, no real ``hermes-webui-agent`` pack. The
one external process the suite starts is ``bash -n``, which only parses.

The module under test is loaded by path rather than imported as
``install.pack_install`` so the suite does not depend on ``install/`` being a
package (it is not) or on the repository root being importable.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "install" / "pack_install.py"

#: The upstream launcher body, copied verbatim from ``make_launcher()`` in
#: /tmp/hwa/lib.sh. The generated launcher must keep every one of these lines
#: working; the tests below assert the environment exports and the exec line
#: survive the rewrite.
UPSTREAM_LAUNCHER = """#!/usr/bin/env bash
# hermes-webui launcher (relatif terhadap pack root)
set -euo pipefail
SELF="$(realpath "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"
export HERMES_WEBUI_AGENT_DIR="$ROOT/hermes-agent"
export HERMES_WEBUI_PYTHON="$ROOT/.venv/bin/python"
export HERMES_WEBUI_PORT="${HERMES_WEBUI_PORT:-8789}"
mkdir -p "${HERMES_WEBUI_STATE_DIR:-$ROOT/state}"
exec "$HERMES_WEBUI_PYTHON" "$ROOT/hermes-webui/bootstrap.py" --no-browser --host 0.0.0.0 "$@"
"""


def load_module():
    """Import ``install/pack_install.py`` by file path."""
    spec = importlib.util.spec_from_file_location("_test_pack_install_module", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pack_install = load_module()


class PackInstallTestBase(unittest.TestCase):
    """Builds a disposable synthetic pack + repository checkout per test."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pack-install-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pack = self.tmp / "pack"
        self.repo = self.tmp / "repo"
        self._make_pack()
        self._make_repo()

    # -- fixtures ---------------------------------------------------------- #
    def _make_pack(self) -> None:
        """A directory shaped exactly like the tar.gz contents (BLUEPRINT §4)."""
        (self.pack / "hermes-webui").mkdir(parents=True)
        (self.pack / "hermes-webui" / "server.py").write_text("# upstream\n", encoding="utf-8")
        (self.pack / "hermes-webui" / "bootstrap.py").write_text("# upstream\n", encoding="utf-8")
        (self.pack / "hermes-agent").mkdir()
        venv_bin = self.pack / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        # The real pack venv python is a relative symlink into .pyroot; a symlink
        # to the running interpreter exercises the same is-a-file-following-links
        # check without shipping a 60 MB python.
        (venv_bin / "python").symlink_to(sys.executable)
        (self.pack / "bin").mkdir()
        (self.pack / "bin" / "hermes").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        self.launcher = self.pack / "bin" / "hermes-webui"
        self.launcher.write_text(UPSTREAM_LAUNCHER, encoding="utf-8")
        self.launcher.chmod(0o755)

    def _make_repo(self) -> None:
        sidecar = self.repo / "sidecar"
        (sidecar / "gate").mkdir(parents=True)
        (sidecar / "__init__.py").write_text('__version__ = "0.0.0-test"\n', encoding="utf-8")
        (sidecar / "server.py").write_text("# sidecar\n", encoding="utf-8")
        (sidecar / "gate" / "official_tools.json").write_text('{"tools": []}\n', encoding="utf-8")
        (sidecar / "gate" / "official_chat_system.txt").write_text("system\n", encoding="utf-8")
        (sidecar / "gate" / "official_resp_developer.txt").write_text("dev\n", encoding="utf-8")
        # Bytecode caches and stray artifacts must never reach the pack.
        (sidecar / "__pycache__").mkdir()
        (sidecar / "__pycache__" / "server.cpython-312.pyc").write_bytes(b"\x00\x01")
        (sidecar / "server.pyc").write_bytes(b"\x00\x01")

        plugin = self.repo / "plugin" / "opencode-zen-free"
        plugin.mkdir(parents=True)
        (plugin / "__init__.py").write_text("# plugin\n", encoding="utf-8")
        (plugin / "plugin.yaml").write_text("name: opencode-zen-free\n", encoding="utf-8")
        (plugin / "__pycache__").mkdir()
        (plugin / "__pycache__" / "x.pyc").write_bytes(b"\x00")

        (self.repo / "install").mkdir()
        (self.repo / "install" / "seed_config.py").write_text(
            "#!/usr/bin/env python3\nSEED = 1\n", encoding="utf-8"
        )

    # -- helpers ----------------------------------------------------------- #
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        """Invoke the module's CLI in-process, capturing stdout/stderr."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pack_install.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def install(self, *extra: str) -> tuple[int, str, str]:
        return self.run_cli(
            "--pack", str(self.pack), "--repo-root", str(self.repo), *extra
        )

    def read_launcher(self) -> str:
        return self.launcher.read_text(encoding="utf-8")

    def installed_paths(self) -> dict[str, Path]:
        return {
            "sidecar": self.pack / "sidecar" / "__init__.py",
            "gate_tools": self.pack / "sidecar" / "gate" / "official_tools.json",
            "plugin": self.pack
            / "hermes-agent"
            / "plugins"
            / "model-providers"
            / "opencode-zen-free"
            / "__init__.py",
            "plugin_yaml": self.pack
            / "hermes-agent"
            / "plugins"
            / "model-providers"
            / "opencode-zen-free"
            / "plugin.yaml",
            "seed": self.pack / "install" / "seed_config.py",
            "sidecar_script": self.pack / "bin" / "opencodezen4hermes-sidecar",
        }


class TestRefusal(PackInstallTestBase):
    def test_refuses_plain_directory(self):
        """A directory without hermes-webui/server.py or .venv/bin/python is refused."""
        target = self.tmp / "not-a-pack"
        target.mkdir()
        code, _out, err = self.run_cli("--pack", str(target), "--repo-root", str(self.repo))
        self.assertEqual(code, 1)
        self.assertIn("refusing to install", err)
        self.assertEqual(list(target.iterdir()), [], "refusal must not write anything")

    def test_refuses_pack_missing_venv(self):
        shutil.rmtree(self.pack / ".venv")
        code, _out, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn(".venv/bin/python", err)
        self.assertFalse((self.pack / "sidecar").exists())

    def test_refuses_pack_missing_webui_server(self):
        (self.pack / "hermes-webui" / "server.py").unlink()
        code, _out, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("hermes-webui/server.py", err)

    def test_refuses_nonexistent_pack(self):
        code, _out, err = self.run_cli(
            "--pack", str(self.tmp / "nope"), "--repo-root", str(self.repo)
        )
        self.assertEqual(code, 1)
        self.assertIn("not a directory", err)

    def test_refuses_when_repository_source_is_missing(self):
        shutil.rmtree(self.repo / "plugin")
        code, _out, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("repository source missing", err)
        self.assertFalse((self.pack / "sidecar").exists(), "no partial install")

    def test_rejects_out_of_range_port(self):
        code, _out, err = self.run_cli(
            "--pack", str(self.pack), "--repo-root", str(self.repo), "--port", "0"
        )
        self.assertEqual(code, 1)
        self.assertIn("--port must be 1..65535", err)


class TestFreshInstall(PackInstallTestBase):
    def test_lays_down_every_artifact(self):
        code, out, _err = self.install()
        self.assertEqual(code, 0, out)
        for name, path in self.installed_paths().items():
            self.assertTrue(path.is_file(), f"{name} missing at {path}")

    def test_gate_payload_files_are_copied(self):
        """gate/*.txt are read at request time by sidecar/gate.py — ship them."""
        self.assertEqual(self.install()[0], 0)
        gate = self.pack / "sidecar" / "gate"
        for name in ("official_tools.json", "official_chat_system.txt", "official_resp_developer.txt"):
            self.assertTrue((gate / name).is_file(), name)

    def test_pycache_excluded(self):
        self.assertEqual(self.install()[0], 0)
        self.assertFalse((self.pack / "sidecar" / "__pycache__").exists())
        self.assertFalse((self.pack / "sidecar" / "server.pyc").exists())
        self.assertFalse(
            (
                self.pack
                / "hermes-agent"
                / "plugins"
                / "model-providers"
                / "opencode-zen-free"
                / "__pycache__"
            ).exists()
        )
        found = [p for p in self.pack.rglob("*") if p.name == "__pycache__" or p.suffix == ".pyc"]
        self.assertEqual(found, [], f"bytecode leaked into the pack: {found}")

    def test_antipattern_survives(self):
        """Without a config seed, api/config.py shows an empty group (BLUEPRINT §1).

        The launcher must reference the seed at the path the installer writes.
        """
        self.assertEqual(self.install()[0], 0)
        launched = self.read_launcher()
        self.assertIn('SEED_SCRIPT="$ROOT/install/seed_config.py"', launched)
        self.assertIn('"$HERMES_WEBUI_PYTHON" "$SEED_SCRIPT"', launched)

    def test_scripts_are_executable(self):
        self.assertEqual(self.install()[0], 0)
        self.assertTrue(os.access(self.launcher, os.X_OK))
        self.assertTrue(os.access(self.pack / "bin" / "opencodezen4hermes-sidecar", os.X_OK))

    def test_reports_each_artifact(self):
        _code, out, _err = self.install()
        for fragment in (
            "[created] <pack>/sidecar",
            "[created] <pack>/hermes-agent/plugins/model-providers/opencode-zen-free",
            "[created] <pack>/install/seed_config.py",
            "[created] <pack>/bin/opencodezen4hermes-sidecar",
            "[updated] <pack>/bin/hermes-webui",
            "[created] <pack>/bin/hermes-webui.upstream",
        ):
            self.assertIn(fragment, out)


class TestGeneratedLauncher(PackInstallTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.install()[0], 0)
        self.text = self.read_launcher()

    def test_bash_syntax_check_passes(self):
        """`bash -n` must accept the generated launcher."""
        if shutil.which("bash") is None:  # pragma: no cover - bash is always present here
            self.skipTest("bash not available")
        for path in (self.launcher, self.pack / "bin" / "opencodezen4hermes-sidecar"):
            result = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, f"bash -n {path}: {result.stderr}")

    def test_starts_with_a_shebang_and_keeps_strict_mode(self):
        self.assertTrue(self.text.startswith("#!/usr/bin/env bash\n"))
        self.assertIn("set -euo pipefail", self.text)

    def test_preserves_upstream_environment_exports(self):
        """Every export of make_launcher() in /tmp/hwa/lib.sh must survive."""
        for line in (
            'SELF="$(realpath "${BASH_SOURCE[0]}")"',
            'ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"',
            'export HERMES_WEBUI_AGENT_DIR="$ROOT/hermes-agent"',
            'export HERMES_WEBUI_PYTHON="$ROOT/.venv/bin/python"',
            'export HERMES_WEBUI_PORT="${HERMES_WEBUI_PORT:-8789}"',
            'mkdir -p "$STATE"',
        ):
            self.assertIn(line, self.text, f"upstream line lost: {line}")

    def test_preserves_upstream_exec_line(self):
        self.assertIn(
            'exec "$HERMES_WEBUI_PYTHON" "$ROOT/hermes-webui/bootstrap.py" '
            '--no-browser --host 0.0.0.0 "$@"',
            self.text,
        )

    def test_ordering_is_sidecar_health_then_seed_then_exec(self):
        """BLUEPRINT §6 fixes the order; assert it positionally, not by presence."""
        health = self.text.index("probe_healthz")
        start = self.text.index("starting it detached")
        seed = self.text.index('"$HERMES_WEBUI_PYTHON" "$SEED_SCRIPT"')
        exec_line = self.text.index("exec \"$HERMES_WEBUI_PYTHON\"")
        self.assertLess(health, start)
        self.assertLess(start, seed)
        self.assertLess(seed, exec_line)

    def test_health_probe_targets_healthz_only(self):
        """`/healthz` is the only liveness probe anything may use (BLUEPRINT §5)."""
        self.assertIn('"$SIDECAR_URL/healthz"', self.text)
        self.assertNotIn("/v1/models", self.text)

    def test_state_dir_is_honoured_and_logged(self):
        self.assertIn('STATE="${HERMES_WEBUI_STATE_DIR:-$ROOT/state}"', self.text)
        self.assertIn('LOG="$STATE/launcher.log"', self.text)
        self.assertIn('SIDECAR_LOG="$STATE/sidecar.log"', self.text)

    def test_sidecar_port_is_not_the_webui_port(self):
        self.assertIn('SIDECAR_PORT="${OPENCODEZEN4HERMES_PORT:-8799}"', self.text)
        self.assertIn('export HERMES_WEBUI_PORT="${HERMES_WEBUI_PORT:-8789}"', self.text)

    def test_sidecar_start_uses_pack_pythonpath(self):
        """`-m sidecar` only resolves with PYTHONPATH=$ROOT inside a pack."""
        self.assertIn('PYTHONPATH="$ROOT" OPENCODEZEN4HERMES_PORT="$SIDECAR_PORT"', self.text)
        self.assertIn("disown", self.text)

    def test_failures_are_warnings_not_fatal(self):
        """A dead sidecar must never stop the WebUI from booting (BLUEPRINT §6)."""
        self.assertNotIn("set -e\n", self.text.replace("set -euo pipefail", ""))
        self.assertIn("continuing anyway", self.text)
        # No `exit` before the exec: that is the only way out of the script.
        body_before_exec = self.text[: self.text.index('exec "$HERMES_WEBUI_PYTHON"')]
        for line in body_before_exec.splitlines():
            stripped = line.strip()
            self.assertFalse(
                stripped.startswith("exit ") or stripped == "exit",
                f"launcher can exit before the WebUI starts: {line!r}",
            )
        # `seed_rc` is captured, never propagated.
        self.assertIn('|| seed_rc=$?', self.text)

    def test_custom_port_is_baked_in(self):
        self.assertEqual(self.install("--port", "9123")[0], 0)
        self.assertIn('SIDECAR_PORT="${OPENCODEZEN4HERMES_PORT:-9123}"', self.read_launcher())


class TestSidecarScript(PackInstallTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.install()[0], 0)
        self.script = self.pack / "bin" / "opencodezen4hermes-sidecar"
        self.text = self.script.read_text(encoding="utf-8")

    def test_sets_pythonpath_and_runs_module(self):
        self.assertIn('export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"', self.text)
        self.assertIn('exec "$PY" -m sidecar "$@"', self.text)

    def test_uses_the_pack_venv_python(self):
        self.assertIn('PY="$ROOT/.venv/bin/python"', self.text)
        self.assertNotIn("/usr/bin/python3", self.text)

    def test_honours_port_env_and_forwards_flags(self):
        self.assertIn('export OPENCODEZEN4HERMES_PORT="${OPENCODEZEN4HERMES_PORT:-8799}"', self.text)
        self.assertIn('"$@"', self.text)

    def test_guards_against_a_missing_venv(self):
        shutil.rmtree(self.pack / ".venv")
        result = subprocess.run([str(self.script)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(".venv/bin/python is missing", result.stderr)

    def test_explains_why_pythonpath_is_needed(self):
        """The comment is the specification: a reader must not 'fix' it away."""
        self.assertIn("No module named sidecar", self.text)
        self.assertIn("PYTHONPATH", self.text)


class TestIdempotence(PackInstallTestBase):
    def test_rerun_reports_unchanged_and_keeps_content(self):
        self.assertEqual(self.install()[0], 0)
        first = self.read_launcher()
        paths = self.installed_paths()

        code, out, _err = self.install()
        self.assertEqual(code, 0)
        self.assertIn("[unchanged] <pack>/bin/hermes-webui", out)
        self.assertIn("[unchanged] <pack>/bin/opencodezen4hermes-sidecar", out)
        self.assertEqual(self.read_launcher(), first)
        for name, path in paths.items():
            self.assertTrue(path.is_file(), f"{name} lost on re-run")

    def test_no_bytecode_creeps_in_on_rerun(self):
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.install()[0], 0)
        found = [p for p in self.pack.rglob("*.pyc")]
        self.assertEqual(found, [], f"bytecode leaked on re-run: {found}")

    def test_deleted_sidecar_module_does_not_survive_a_rerun(self):
        """The sidecar directory is owned wholesale: replacement, not merge."""
        self.assertEqual(self.install()[0], 0)
        stale = self.pack / "sidecar" / "removed_module.py"
        stale.write_text("# gone upstream\n", encoding="utf-8")
        self.assertEqual(self.install()[0], 0)
        self.assertFalse(stale.exists(), "stale module survived a re-run")

    def test_repairs_a_lost_exec_bit(self):
        self.assertEqual(self.install()[0], 0)
        os.chmod(self.launcher, 0o644)
        self.assertEqual(self.install()[0], 0)
        self.assertTrue(os.access(self.launcher, os.X_OK), "exec bit not repaired")


class TestUpstreamBackup(PackInstallTestBase):
    def test_backup_created_once_and_preserved(self):
        upstream = self.pack / "bin" / "hermes-webui.upstream"
        self.assertFalse(upstream.exists())

        self.assertEqual(self.install()[0], 0)
        self.assertEqual(upstream.read_text(encoding="utf-8"), UPSTREAM_LAUNCHER)
        self.assertNotEqual(self.read_launcher(), UPSTREAM_LAUNCHER, "launcher not extended")

        # Corrupt the backup; a re-run must not recreate it, proving "once".
        upstream.write_text("# tampered\n", encoding="utf-8")
        code, out, _err = self.install()
        self.assertEqual(code, 0)
        self.assertIn("upstream backup kept", out)
        self.assertEqual(upstream.read_text(encoding="utf-8"), "# tampered\n")

    def test_rerun_does_not_back_up_our_own_launcher(self):
        self.assertEqual(self.install()[0], 0)
        extended = self.read_launcher()
        (self.pack / "bin" / "hermes-webui.upstream").unlink()
        self.assertEqual(self.install()[0], 0)
        # The backup is gone and the launcher is already ours: nothing to back up,
        # and crucially the extended launcher must not become the "upstream".
        self.assertFalse((self.pack / "bin" / "hermes-webui.upstream").exists())
        self.assertEqual(self.read_launcher(), extended)
        code, out, _err = self.install()
        self.assertEqual(code, 0)
        self.assertIn("no upstream to back up", out)

    def test_missing_upstream_launcher_is_not_fatal(self):
        self.launcher.unlink()
        code, out, _err = self.install()
        self.assertEqual(code, 0)
        self.assertIn("nothing to back up", out)
        self.assertTrue(self.launcher.is_file(), "launcher still written")


class TestLauncherRuntime(PackInstallTestBase):
    """Run the generated launcher against a stub pack interpreter.

    Text assertions cannot show that the three §6 steps actually happen in
    order, that arguments survive, or that a broken sidecar does not abort the
    boot. These tests execute the real generated script with ``$ROOT/.venv/bin/python``
    replaced by a stub that answers the health probe and records the calls.

    No sockets: the stub intercepts the probe's ``-c`` invocation, so nothing
    ever dials 127.0.0.1. The wait loop is capped with
    ``OPENCODEZEN4HERMES_LAUNCHER_TICKS=1`` to keep the suite fast.
    """

    #: Stub interpreter. It must answer three call shapes:
    #:   -c <script> <url>       -> the /healthz probe: print $STUB_HEALTH
    #:   -m sidecar ...          -> the sidecar control script's exec line
    #:   <seed_config.py> --port -> the config seed (step 2)
    #:   <bootstrap.py> ...      -> the WebUI exec (step 3)
    STUB_PYTHON = """#!/usr/bin/env bash
case "$1" in
  -c) printf '%s\\n' "${STUB_HEALTH:-no}" ;;
  -m) printf 'CALL sidecar-module port=%s pythonpath=%s\\n' \\
        "${OPENCODEZEN4HERMES_PORT:-unset}" "${PYTHONPATH:-unset}" ;;
  *seed_config.py) printf 'CALL seed args=%s\\n' "$*" ; exit "${STUB_SEED_RC:-0}" ;;
  *bootstrap.py) printf 'CALL webui %s\\n' "$*" ;;
  *) printf 'CALL other %s\\n' "$*" ;;
esac
exit 0
"""

    def setUp(self) -> None:
        super().setUp()
        venv_python = self.pack / ".venv" / "bin" / "python"
        venv_python.unlink()
        venv_python.write_text(self.STUB_PYTHON, encoding="utf-8")
        venv_python.chmod(0o755)
        self.health_file = self.tmp / "health.txt"
        self.assertEqual(self.install()[0], 0)

    def run_launcher(self, *argv: str, health: str = "no", seed_rc: int = 0, port: str = "8799"):
        """Execute bin/hermes-webui, returning (returncode, stdout+stderr, log text)."""
        if shutil.which("bash") is None:  # pragma: no cover
            self.skipTest("bash not available")
        env = dict(os.environ)
        env.update(
            {
                "STUB_HEALTH": health,
                "STUB_SEED_RC": str(seed_rc),
                "OPENCODEZEN4HERMES_PORT": port,
                # 1 tick = at most 0.5 s of waiting instead of the real 10 s.
                "OPENCODEZEN4HERMES_LAUNCHER_TICKS": "1",
            }
        )
        result = subprocess.run(
            ["bash", str(self.launcher), *argv],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        log = self.pack / "state" / "launcher.log"
        return result.returncode, result.stdout + result.stderr, (
            log.read_text(encoding="utf-8") if log.exists() else ""
        )

    def sidecar_log(self) -> str:
        """Calls made by the detached sidecar land here, not on our stdout."""
        path = self.pack / "state" / "sidecar.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_boots_the_webui_and_forwards_arguments(self):
        code, output, _log = self.run_launcher("--extra", "value")
        self.assertEqual(code, 0, output)
        self.assertIn("CALL webui", output)
        self.assertIn("--no-browser --host 0.0.0.0 --extra value", output)

    def test_runs_sidecar_then_seed_then_webui(self):
        """The runtime order is the contract; assert it in the captured calls.

        The detached sidecar writes to state/sidecar.log rather than our stdout
        (it is disowned), so the sidecar call is read from there.
        """
        _code, output, log = self.run_launcher()
        start = log.index("step 1/3 sidecar silent")
        seed = log.index("step 2/3 seeding")
        webui = log.index("step 3/3 exec bootstrap.py")
        self.assertLess(start, seed, "sidecar must start before the seed")
        self.assertLess(seed, webui, "seed must run before the WebUI exec")
        self.assertIn("CALL sidecar-module", self.sidecar_log())
        # The seed's own output is redirected into launcher.log (§6: every step
        # is logged there), while the WebUI exec inherits our streams.
        self.assertIn("CALL seed", log)
        self.assertIn("CALL webui", output)

    def test_sidecar_gets_pack_pythonpath(self):
        """The stub is exec'd by bin/opencodezen4hermes-sidecar, not directly."""
        code, _output, _log = self.run_launcher()
        self.assertEqual(code, 0)
        called = self.sidecar_log()
        self.assertIn("CALL sidecar-module", called)
        # PATH_SEP: bin/opencodezen4hermes-sidecar injects PYTHONPATH=$ROOT so
        # that `-m sidecar` resolves inside a pack (the package is not installed).
        self.assertIn(f"pythonpath={self.pack}", called)

    def test_seed_receives_the_sidecar_port_not_the_webui_port(self):
        """Seeding the WebUI port would point base_url at the WebUI, not the sidecar."""
        _code, output, log = self.run_launcher(port="9123")
        seeded = " ".join(line for line in log.splitlines() if "CALL seed args=" in line)
        self.assertIn("--port 9123", seeded)
        self.assertNotIn("8789", seeded)

    def test_healthy_sidecar_is_not_restarted(self):
        code, output, log = self.run_launcher(health="ok")
        self.assertEqual(code, 0)
        self.assertEqual(self.sidecar_log(), "", "restarted an already-healthy sidecar")
        self.assertIn("CALL webui", output)
        self.assertIn("already answering", log)

    def test_dead_sidecar_does_not_stop_the_webui(self):
        """BLUEPRINT §6: a dead sidecar must never stop the WebUI from booting."""
        code, output, log = self.run_launcher(health="no")
        self.assertEqual(code, 0)
        self.assertIn("WARN step 1/3", log)
        self.assertIn("CALL webui", output, "WebUI was not exec'd")

    def test_failing_seed_does_not_stop_the_webui(self):
        code, output, log = self.run_launcher(health="ok", seed_rc=1)
        self.assertEqual(code, 0)
        self.assertIn("WARN step 2/3 config seed exited 1", log)
        self.assertIn("CALL webui", output, "WebUI was not exec'd")

    def test_missing_sidecar_script_does_not_stop_the_webui(self):
        (self.pack / "bin" / "opencodezen4hermes-sidecar").unlink()
        code, output, log = self.run_launcher()
        self.assertEqual(code, 0)
        self.assertIn("missing or not executable", log)
        self.assertIn("CALL webui", output)

    def test_logs_to_the_state_dir_not_the_pack_root(self):
        state = self.tmp / "custom-state"
        env = dict(os.environ)
        env.update(
            {
                "STUB_HEALTH": "ok",
                "HERMES_WEBUI_STATE_DIR": str(state),
                "OPENCODEZEN4HERMES_LAUNCHER_TICKS": "1",
            }
        )
        result = subprocess.run(
            ["bash", str(self.launcher)], capture_output=True, text=True, env=env,
            timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((state / "launcher.log").is_file(), "HERMES_WEBUI_STATE_DIR ignored")
        self.assertFalse((self.pack / "state" / "launcher.log").exists())

    def test_real_sidecar_script_refuses_a_pack_without_a_venv(self):
        """The control script exits non-zero rather than running a wrong python."""
        (self.pack / ".venv" / "bin" / "python").unlink()
        result = subprocess.run(
            [str(self.pack / "bin" / "opencodezen4hermes-sidecar"), "--print-config"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("is missing", result.stderr)


class TestDryRun(PackInstallTestBase):
    def test_dry_run_writes_nothing(self):
        before = sorted(p.relative_to(self.pack).as_posix() for p in self.pack.rglob("*"))
        launcher_before = self.read_launcher()

        code, out, _err = self.install("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("dry-run: nothing was written", out)

        after = sorted(p.relative_to(self.pack).as_posix() for p in self.pack.rglob("*"))
        self.assertEqual(before, after, "dry-run created or removed files")
        self.assertEqual(self.read_launcher(), launcher_before)
        self.assertFalse((self.pack / "bin" / "hermes-webui.upstream").exists())
        self.assertFalse((self.pack / "sidecar").exists())

    def test_dry_run_still_reports_the_plan(self):
        _code, out, _err = self.install("--dry-run")
        self.assertIn("installing opencodezen4hermes into", out)
        self.assertIn("[created] <pack>/bin/hermes-webui.upstream (would be written)", out)
        self.assertIn("[updated] <pack>/bin/hermes-webui", out)

    def test_dry_run_after_install_reports_unchanged(self):
        self.assertEqual(self.install()[0], 0)
        code, out, _err = self.install("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("[unchanged] <pack>/bin/hermes-webui", out)


class TestDefaultsAndValidation(PackInstallTestBase):
    def test_default_repo_root_is_the_parent_of_install(self):
        """`--repo-root` omitted must mean the checkout this script lives in."""
        self.assertEqual(pack_install.main.__module__, "_test_pack_install_module")
        code, out, _err = self.run_cli("--pack", str(self.pack))
        self.assertEqual(code, 0)
        self.assertIn(f"source: {REPO_ROOT}", out)
        # The real repository's own sidecar must have landed in the pack.
        self.assertTrue((self.pack / "sidecar" / "catalog.py").is_file())

    def test_validate_pack_returns_reasons(self):
        problems = pack_install.validate_pack(self.pack)
        self.assertEqual(problems, [])
        self.assertEqual(len(pack_install.validate_pack(self.tmp)), 2)

    def test_default_port_matches_the_sidecar_module(self):
        """One port, agreed by BLUEPRINT §2, sidecar/config.py and this installer."""
        self.assertEqual(pack_install.DEFAULT_SIDECAR_PORT, 8799)
        sys.path.insert(0, str(REPO_ROOT))
        try:
            import sidecar.config as sidecar_config
        finally:
            sys.path.remove(str(REPO_ROOT))
        self.assertEqual(sidecar_config.DEFAULT_PORT, pack_install.DEFAULT_SIDECAR_PORT)


class TestTarballName(unittest.TestCase):
    """The released artefact name is user-facing, so pin its shape.

    Upstream built ``VER_ID`` with ``echo "$WEBUI_REF-$AGENT_REF" | tr -c ...``.
    ``tr -c`` maps *every* non-matching byte, including ``echo``'s trailing
    newline, so ``VER_ID`` ended in ``-`` and the tarball was named
    ``...v2026.9.24--linux-arm64.tar.gz``. A stray dash is cosmetic until
    someone scripts against the published name, and then it is a broken glob.
    """

    def _ver_id(self, webui_ref: str, agent_ref: str) -> str:
        """Evaluate build.sh's VER_ID expression exactly as the shell does."""
        expr = self._extract_ver_id_expression()
        completed = subprocess.run(
            ["bash", "-c", f'WEBUI_REF={webui_ref!r}; AGENT_REF={agent_ref!r}; {expr}; printf %s "$VER_ID"'],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout

    @staticmethod
    def _extract_ver_id_expression() -> str:
        """Pull the real assignment out of build.sh rather than duplicating it."""
        for raw in (REPO_ROOT / "build.sh").read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("VER_ID="):
                return line
        raise AssertionError("build.sh has no VER_ID assignment to test")

    def test_the_ver_id_has_no_trailing_dash(self):
        ver_id = self._ver_id("v0.52.458", "v2026.9.24")
        self.assertEqual(ver_id, "v0.52.458-v2026.9.24")
        self.assertFalse(ver_id.endswith("-"), f"VER_ID ends with a dash: {ver_id!r}")

    def test_the_tarball_name_is_clean(self):
        ver_id = self._ver_id("v0.52.458", "v2026.9.24")
        name = f"hermes-webui-agent-{ver_id}-linux-arm64.tar.gz"
        self.assertEqual(name, "hermes-webui-agent-v0.52.458-v2026.9.24-linux-arm64.tar.gz")
        self.assertNotIn("--", name)

    def test_the_assignment_does_not_use_echo(self):
        """The bug was ``echo``'s newline; ``printf '%s'`` cannot emit one."""
        expr = self._extract_ver_id_expression()
        self.assertNotIn("echo", expr, "use printf '%s' — echo's newline becomes a dash")
        self.assertIn("printf", expr)

    def test_branch_refs_stay_single_dash_separated(self):
        """A ref may itself contain characters tr rewrites; the join stays one dash."""
        ver_id = self._ver_id("main", "main")
        self.assertEqual(ver_id, "main-main")


if __name__ == "__main__":
    unittest.main(verbosity=2)
