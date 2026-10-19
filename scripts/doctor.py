#!/usr/bin/env python3
"""Diagnose a machine that is supposed to serve the Zen free roster.

``verify_live.py`` answers "does the lane work?". This script answers the more
common user question: **"why does my WebUI not show the free models?"**

It checks, in order:

1. the interpreter is Python 3.12 or newer;
2. the sidecar answers ``GET /healthz`` (the only liveness probe this repo
   allows — docs/BLUEPRINT.md §5);
3. ``GET /v1/models`` serves the nine free ids (docs/BLUEPRINT.md §3);
4. the provider plugin is importable and its ``ProviderProfile`` carries the
   right ``base_url`` and fallback roster — this is what the picker reads;
5. the config seed exists and carries ``base_url`` (docs/BLUEPRINT.md §7) —
   without it ``api/config.py`` never admits the slug into
   ``detected_providers`` and the model group renders empty;
6. a pack layout is present and complete, when ``--pack`` is given.

Network failures are reported as a check result, never as a traceback. Exit
status is ``0`` unless at least one check FAILed; ``WARN`` never fails the run,
because a dev checkout legitimately has no seeded ``$HERMES_HOME`` and the
launcher treats the seed as best-effort (docs/BLUEPRINT.md §6).

Usage:
    python3 scripts/doctor.py
    python3 scripts/doctor.py --pack /opt/hermes-webui-agent
    python3 scripts/doctor.py --port 8799 --json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

#: The nine free models, in picker order (docs/BLUEPRINT.md §3).
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

PROVIDER_SLUG = "opencode-zen-free"
PROVIDER_LABEL = "OpenCode Zen (free)"
MIN_PYTHON = (3, 12)

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass(slots=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"check": self.name, "status": self.status, "detail": self.detail, "fix": self.fix}


@dataclass(slots=True)
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, fix: str = "") -> Check:
        check = Check(name=name, status=status, detail=detail, fix=fix)
        self.checks.append(check)
        return check

    def count(self, status: str) -> int:
        return sum(1 for check in self.checks if check.status == status)

    @property
    def ok(self) -> bool:
        return self.count(FAIL) == 0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _get_json(url: str, timeout: float) -> tuple[int, Any]:
    """GET a JSON document. Returns ``(status, payload_or_None)``, never raises."""
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as error:
        return int(error.code), None
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise ConnectionError(str(getattr(error, "reason", error))) from None
    try:
        return status, json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return status, None


def _sidecar_defaults() -> tuple[str, int]:
    """Prefer the shipped defaults over a second copy of 8799."""
    try:
        from sidecar.config import DEFAULT_HOST, DEFAULT_PORT  # noqa: PLC0415 - optional

        return DEFAULT_HOST, int(DEFAULT_PORT)
    except Exception:  # noqa: BLE001 - a partial checkout must not break the doctor
        return "127.0.0.1", 8799


def config_path_from_env() -> str | None:
    """$HERMES_CONFIG_PATH, else $HERMES_HOME/config.yaml (docs/BLUEPRINT.md §7)."""
    explicit = os.environ.get("HERMES_CONFIG_PATH")
    if explicit:
        return os.path.expanduser(explicit)
    home = os.environ.get("HERMES_HOME")
    if home:
        return os.path.join(os.path.expanduser(home), "config.yaml")
    return os.path.expanduser("~/.hermes/config.yaml")


_SEED_KEY = re.compile(rf"^\s+{re.escape(PROVIDER_SLUG)}\s*:\s*$")
_BASE_URL = re.compile(r"^\s+base_url\s*:\s*(\S.*?)\s*$")


def parse_seed(text: str) -> dict[str, Any] | None:
    """Find the ``providers.opencode-zen-free`` block, with or without PyYAML.

    PyYAML is used when it is importable (the pack venv has it) because it is
    exact. The fallback is a deliberately narrow indentation scan: it only looks
    for the slug line followed by ``base_url``, which is all this check needs,
    and it says so in the result so nobody mistakes it for a YAML parser.
    """
    try:
        import yaml  # noqa: PLC0415 - optional dependency, present in the pack venv
    except ImportError:
        yaml = None  # type: ignore[assignment]

    if yaml is not None:
        try:
            document = yaml.safe_load(text) or {}
        except Exception as error:  # noqa: BLE001 - any parse failure is the finding
            return {"_error": f"not valid YAML: {error}"}
        if not isinstance(document, dict):
            return {"_error": "top level is not a mapping"}
        providers = document.get("providers")
        if not isinstance(providers, dict):
            return None
        entry = providers.get(PROVIDER_SLUG)
        if not isinstance(entry, dict):
            return None
        return {**entry, "_parser": "pyyaml"}

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not _SEED_KEY.match(line):
            continue
        block: dict[str, Any] = {"_parser": "scan"}
        indent = len(line) - len(line.lstrip())
        for follow in lines[index + 1 :]:
            if not follow.strip() or follow.lstrip().startswith("#"):
                continue
            if len(follow) - len(follow.lstrip()) <= indent:
                break
            match = _BASE_URL.match(follow)
            if match:
                block["base_url"] = match.group(1).strip("'\"")
        return block
    return None


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def check_python(report: Report) -> None:
    version = sys.version_info
    human = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) >= MIN_PYTHON:
        report.add("python", PASS, f"{human} at {sys.executable}")
    else:
        report.add(
            "python",
            FAIL,
            f"{human} is older than the required {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            f"run the sidecar with a Python >= 3.12, e.g. the pack venv: "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
        )


def check_healthz(report: Report, base: str, timeout: float) -> dict[str, Any] | None:
    url = f"{base}/healthz"
    try:
        status, payload = _get_json(url, timeout)
    except ConnectionError as error:
        report.add(
            "sidecar /healthz",
            FAIL,
            f"{url} did not answer ({error})",
            "start it: python3 -m sidecar (dev) or bin/opencodezen4hermes-sidecar "
            "(pack); check state/sidecar.log if it exits 2 (port taken)",
        )
        return None
    if status != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
        report.add(
            "sidecar /healthz",
            FAIL,
            f"{url} answered HTTP {status} without {{\"ok\": true}}",
            "something else may own the port; stop it or run the sidecar with --port N",
        )
        return None
    catalog = payload.get("catalog") if isinstance(payload.get("catalog"), dict) else {}
    exposed = catalog.get("exposed")
    total = catalog.get("total")
    report.add(
        "sidecar /healthz",
        PASS,
        f"ok: true (catalog {exposed} free of {total} live, status={catalog.get('status')})",
    )
    return payload


def check_models(report: Report, base: str, timeout: float) -> list[str]:
    url = f"{base}/v1/models"
    try:
        status, payload = _get_json(url, timeout)
    except ConnectionError as error:
        report.add("sidecar /v1/models", FAIL, f"{url} did not answer ({error})", "see the /healthz hint")
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(data, list):
        report.add(
            "sidecar /v1/models",
            FAIL,
            f"{url} answered HTTP {status} without a data list",
            "restart the sidecar; if it persists, run python3 -m sidecar --verbose",
        )
        return []

    served = [item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)]
    missing = [model for model in EXPECTED_MODELS if model not in served]
    extra = [model for model in served if model not in EXPECTED_MODELS]
    if not served:
        report.add(
            "sidecar /v1/models",
            FAIL,
            "the catalog is empty — the free filter rejected everything",
            "check GET /healthz catalog.last_error, then --catalog-ttl 0 to refetch",
        )
    elif missing:
        report.add(
            "sidecar /v1/models",
            WARN,
            f"{len(served)} model(s) served, missing from BLUEPRINT §3: {', '.join(missing)}",
            "the upstream roster moved; refresh with --catalog-ttl 0 and update docs/BLUEPRINT.md §3",
        )
    else:
        report.add("sidecar /v1/models", PASS, f"{len(served)} free model(s), all nine contract ids present")
    if extra:
        report.add(
            "sidecar /v1/models (extra)",
            WARN,
            f"unexpected id(s): {', '.join(extra[:6])}",
            "either the roster grew (update BLUEPRINT §3) or a paid id leaked into the free group",
        )
    return served


def _stub_providers() -> None:
    """Install the smallest ``providers`` package the plugin imports.

    Only used when the real hermes-agent tree is absent, so the *profile fields*
    can still be inspected. It is reported as a WARN, never as a PASS.
    """
    import types

    if "providers" in sys.modules:
        return

    package = types.ModuleType("providers")
    base = types.ModuleType("providers.base")

    class ProviderProfile:  # noqa: D401 - mirrors the upstream dataclass surface we use
        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    registered: list[Any] = []

    def register_provider(profile: Any) -> None:
        registered.append(profile)

    base.ProviderProfile = ProviderProfile  # type: ignore[attr-defined]
    package.register_provider = register_provider  # type: ignore[attr-defined]
    package.base = base  # type: ignore[attr-defined]
    package.__path__ = []  # type: ignore[attr-defined]
    sys.modules["providers"] = package
    sys.modules["providers.base"] = base


def check_plugin(report: Report, hermes_agent_dir: str | None, expect_base_url: str) -> None:
    plugin_dir = os.path.join(_REPO_ROOT, "plugin", PROVIDER_SLUG)
    init = os.path.join(plugin_dir, "__init__.py")
    if not os.path.isfile(init):
        report.add(
            "plugin profile",
            FAIL,
            f"no plugin at {init}",
            "run this from a checkout of opencodezen4hermes, or install the plugin into the pack",
        )
        return

    real_providers = bool(hermes_agent_dir) and os.path.isdir(os.path.join(hermes_agent_dir, "providers"))
    if real_providers:
        assert hermes_agent_dir is not None
        sys.path.insert(0, hermes_agent_dir)
    else:
        _stub_providers()

    # The directory name carries dashes, so it is not importable by name: load it
    # from its file location, exactly like Hermes' plugin loader does.
    module_name = PROVIDER_SLUG.replace("-", "_") + "_plugin"
    try:
        spec = importlib.util.spec_from_file_location(module_name, init)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot build an import spec for {init}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        profile = getattr(module, "profile", None)
    except Exception as error:  # noqa: BLE001 - report, do not traceback
        sys.modules.pop(module_name, None)
        report.add(
            "plugin profile",
            FAIL,
            f"importing {init} raised {type(error).__name__}: {error}",
            "run the sidecar's own interpreter against it to see the traceback: "
            f"python3 -c \"import importlib.util as u; s=u.spec_from_file_location('p', '{init}'); "
            "m=u.module_from_spec(s); s.loader.exec_module(m)\"",
        )
        return

    if profile is None:
        report.add(
            "plugin profile",
            FAIL,
            f"{init} defines no module-level `profile`",
            "the picker reads `profile`, not `register_provider()` alone",
        )
        return

    problems: list[str] = []
    if getattr(profile, "name", None) != PROVIDER_SLUG:
        problems.append(f"name={getattr(profile, 'name', None)!r} != {PROVIDER_SLUG!r}")
    base_url = getattr(profile, "base_url", None)
    if base_url != expect_base_url:
        problems.append(f"base_url={base_url!r} != {expect_base_url!r}")
    fallback = tuple(getattr(profile, "fallback_models", ()) or ())
    if fallback != EXPECTED_MODELS:
        problems.append(
            f"fallback_models has {len(fallback)} id(s) and does not match BLUEPRINT §3"
        )
    if getattr(profile, "auth_type", None) != "api_key":
        problems.append(f"auth_type={getattr(profile, 'auth_type', None)!r} != 'api_key'")
    if getattr(profile, "env_vars", "missing") not in ((), [], "missing"):
        # A keyless provider that advertises env vars makes Hermes ask for a credential.
        problems.append("env_vars is non-empty: the provider must look keyless")

    detail = f"{getattr(profile, 'name', '?')} -> {base_url} ({len(fallback)} fallback ids)"
    source = f"real hermes-agent at {hermes_agent_dir}" if real_providers else "stubbed providers.base"
    if problems:
        if base_url != expect_base_url and all("base_url=" in problem for problem in problems):
            # The profile itself is fine; it just points at a different port than
            # the one under test. Say which side to move.
            advisory = urlsplit(str(base_url))
            where = advisory.netloc or str(base_url)
            report.add(
                "plugin profile",
                WARN,
                f"plugin advertises {base_url}, this run tested {expect_base_url}",
                f"start the sidecar on {where} (the address the picker will use), or "
                "set OPENCODEZEN4HERMES_PORT before starting the sidecar and hermes-webui",
            )
        else:
            report.add("plugin profile", FAIL, "; ".join(problems), "fix plugin/opencode-zen-free/__init__.py")
    elif real_providers:
        report.add("plugin profile", PASS, f"{detail} [{source}]")
    else:
        report.add(
            "plugin profile",
            WARN,
            f"{detail} [{source}: fields checked, Hermes itself not exercised]",
            "point --pack at an installed pack (or set HERMES_HOME) to load the real providers.base",
        )


def check_seed(report: Report, expect_base_url: str) -> None:
    path = config_path_from_env()
    if not path:
        report.add("config seed", WARN, "could not resolve a config path", "set $HERMES_HOME or $HERMES_CONFIG_PATH")
        return
    if not os.path.isfile(path):
        report.add(
            "config seed",
            WARN,
            f"no config at {path}",
            "run install/seed_config.py (or bin/hermes-webui, which seeds on start); "
            "without this route the picker group renders empty",
        )
        return
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        report.add("config seed", FAIL, f"cannot read {path}: {error}", "check file permissions")
        return

    seed = parse_seed(text)
    if seed is None:
        report.add(
            "config seed",
            WARN,
            f"{path} has no providers.{PROVIDER_SLUG} entry",
            "run install/seed_config.py; api/config.py `_has_provider_route` needs the "
            "base_url route to admit the slug into detected_providers",
        )
        return
    if seed.get("_error"):
        report.add(
            "config seed",
            FAIL,
            f"{path}: {seed['_error']}",
            "fix or restore the YAML (a .bak.<timestamp> copy is kept on first seed)",
        )
        return

    base_url = seed.get("base_url")
    parser = seed.get("_parser", "scan")
    if not base_url:
        report.add(
            "config seed",
            WARN,
            f"providers.{PROVIDER_SLUG} exists in {path} but has no base_url [{parser}]",
            f"add base_url: {expect_base_url} — the route field is what the picker needs",
        )
    elif str(base_url) != expect_base_url:
        report.add(
            "config seed",
            WARN,
            f"base_url is {base_url!r}, sidecar serves {expect_base_url!r} [{parser}]",
            "align them: rerun install/seed_config.py --port N, or start the sidecar on "
            "the seeded port",
        )
    elif seed.get("models_discovered") is not False:
        report.add(
            "config seed",
            WARN,
            f"providers.{PROVIDER_SLUG} has base_url but models_discovered is not false",
            "set models_discovered: false — it pins the roster so a stale live probe "
            "cannot empty the picker (BLUEPRINT §7)",
        )
    else:
        report.add("config seed", PASS, f"{path}: providers.{PROVIDER_SLUG} with base_url [{parser}]")


_PACK_ARTEFACTS: tuple[tuple[str, str], ...] = (
    ("sidecar/server.py", "the sidecar package"),
    ("sidecar/gate/official_tools.json", "the captured official tool fixture"),
    ("install/seed_config.py", "the config seeder"),
    ("bin/opencodezen4hermes-sidecar", "the standalone sidecar launcher"),
    (f"hermes-agent/plugins/model-providers/{PROVIDER_SLUG}/__init__.py", "the provider plugin"),
    ("hermes-webui/server.py", "the upstream WebUI"),
    (".venv/bin/python", "the pack interpreter"),
)


def check_pack(report: Report, pack: str | None) -> None:
    if not pack:
        report.add(
            "pack layout",
            SKIP,
            "no --pack given (only meaningful for an installed pack)",
            "re-run with --pack /path/to/hermes-webui-agent to verify the pack contents",
        )
        return
    root = os.path.abspath(os.path.expanduser(pack))
    if not os.path.isdir(root):
        report.add("pack layout", FAIL, f"{root} is not a directory", "pass the unpacked pack root")
        return

    missing = [f"{rel} ({what})" for rel, what in _PACK_ARTEFACTS if not os.path.exists(os.path.join(root, rel))]
    if missing:
        report.add(
            "pack layout",
            FAIL,
            f"missing from {root}: {', '.join(missing)}",
            "reinstall: python3 install/pack_install.py --pack <dir>, or untar the release again",
        )
        return
    launcher = os.path.join(root, "bin", "hermes-webui")
    try:
        with open(launcher, encoding="utf-8", errors="replace") as handle:
            mentions_sidecar = "opencodezen4hermes-sidecar" in handle.read()
    except OSError as error:
        report.add("pack layout", FAIL, f"cannot read {launcher}: {error}", "check permissions")
        return
    if not mentions_sidecar:
        report.add(
            "pack layout",
            FAIL,
            f"{launcher} is the upstream launcher — it never starts the sidecar",
            "run install/pack_install.py --pack <dir> to write the extended launcher",
        )
    else:
        report.add("pack layout", PASS, f"{root}: all seam files present, launcher starts the sidecar")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run_checks(args: argparse.Namespace) -> Report:
    report = Report()
    host, default_port = _sidecar_defaults()
    port = args.port if args.port is not None else default_port
    base = f"http://{host}:{port}"
    expect_base_url = f"{base}/v1"

    check_python(report)
    health = check_healthz(report, base, args.timeout)
    served = check_models(report, base, args.timeout)
    if health is not None and served:
        catalog = health.get("catalog") or {}
        if catalog.get("status") == "stale" and catalog.get("last_error"):
            report.add(
                "sidecar catalog",
                WARN,
                f"serving the last good snapshot; last_error={catalog.get('last_error')}",
                "harmless while the upstream catalog is unreachable; the roster is pinned",
            )
    hermes_agent = None
    if args.pack:
        hermes_agent = os.path.join(os.path.abspath(os.path.expanduser(args.pack)), "hermes-agent")
    elif os.path.isdir(os.path.join(_REPO_ROOT, "hermes-agent")):
        hermes_agent = os.path.join(_REPO_ROOT, "hermes-agent")
    check_plugin(report, hermes_agent, expect_base_url)
    check_seed(report, expect_base_url)
    check_pack(report, args.pack)
    return report


def print_report(report: Report, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "schema": 1,
                    "ok": report.ok,
                    "checks": [check.as_dict() for check in report.checks],
                    "summary": {
                        "pass": report.count(PASS),
                        "warn": report.count(WARN),
                        "fail": report.count(FAIL),
                        "skip": report.count(SKIP),
                    },
                },
                indent=2,
            )
        )
        return

    for check in report.checks:
        print(f"{check.status:<4} {check.name:<22} {check.detail}")
        if check.fix and check.status in {WARN, FAIL}:
            print(f"     {'.' * 18} fix: {check.fix}")
    print()
    print(
        f"summary: {report.count(PASS)} pass, {report.count(WARN)} warn, "
        f"{report.count(FAIL)} fail, {report.count(SKIP)} skip "
        f"-> {'OK' if report.ok else 'PROBLEMS FOUND'}"
    )


def main(argv: list[str] | None = None) -> int:
    host_default, port_default = _sidecar_defaults()
    parser = argparse.ArgumentParser(
        prog="doctor.py",
        description=(
            "Check that a machine can serve the OpenCode Zen free roster: the "
            "sidecar, the model list, the provider plugin, the config seed and "
            "the pack layout."
        ),
        epilog=(
            "Every failure prints a one-line fix. Exit status is 0 unless a check "
            "FAILed; WARN means the feature is degraded but the run is usable."
        ),
    )
    parser.add_argument(
        "--pack",
        default=None,
        metavar="DIR",
        help="installed pack root to inspect (default: no pack check)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="N",
        help=f"sidecar port (default {port_default}, from sidecar/config.py)",
    )
    parser.add_argument("--host", default=host_default, metavar="HOST", help=f"sidecar host (default {host_default})")
    parser.add_argument(
        "--timeout",
        type=float,
        default=4.0,
        metavar="SECONDS",
        help="per-request timeout for the local probes (default 4)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    try:
        report = run_checks(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - a doctor that dies is useless
        print(f"FAIL doctor could not finish: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print_report(report, args.json)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
