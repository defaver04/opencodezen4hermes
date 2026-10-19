#!/usr/bin/env python3
"""Prove that the config seed is what admits ``opencode-zen-free`` into the picker.

What this proves
----------------
``hermes-webui``'s model picker is fed by ``api.config.get_available_models()``.
The provider plugin alone does **not** guarantee a group in that catalogue. This
script builds a throwaway ``HERMES_HOME``, installs the plugin into it, writes a
``config.yaml`` with or without the BLUEPRINT §7 seed block, and asks the real
upstream tree which providers surfaced.

The configuration it probes by default is deliberately the *discriminating* one:
``model.provider`` points at some **other** provider, so the slug can only enter
the catalogue through its own ``providers.opencode-zen-free`` seed entry
(``api/config.py``: ``_has_provider_route`` / ``has_local_signal`` over
``api`` / ``base_url`` / ``api_key`` / ``key_env``). With ``--active-provider
opencode-zen-free`` the slug is admitted by the active-provider branch instead
and the seed stops being load-bearing — the probe reports that honestly rather
than pretending the seed was responsible. See ``--help``.

Skip / override conditions
--------------------------
This is an *integration* probe. It needs things a bare checkout does not have:

* the upstream tree, i.e. ``<upstream>/hermes-webui/api/config.py`` and
  ``<upstream>/hermes-agent/providers/``. Override the search with
  ``--upstream DIR`` or the ``OPENCODEZEN4HERMES_UPSTREAM_DIR`` env var; the
  search order is that env var, ``./vendor/upstream``, ``/tmp/hwa/vendor/upstream``.
  Not found -> exit 2, and ``tests/test_picker_integration.py`` skips.
* an interpreter that can import ``yaml`` (PyYAML) **and** ``httpx``, because
  ``api/config.py`` and the agent tree import both at module import time. A bare
  system ``python3`` has neither. Override with ``--python PATH`` or the
  ``OPENCODEZEN4HERMES_TEST_PYTHON`` env var; otherwise ``/tmp/venv/bin/python``
  and then the current interpreter are tried. None usable -> exit 2, and the
  unittest skips with the interpreter's own ImportError in the message.
* the plugin tree ``plugin/opencode-zen-free/`` in this repository (always here).

Exit codes
----------
``0`` the expected outcome was observed; ``1`` the probe ran but the expectation
failed (this is the interesting failure); ``2`` the probe could not run at all
(missing upstream / interpreter), i.e. the same condition the unittest skips on.

Usage
-----
    python3 scripts/picker_probe.py
    python3 scripts/picker_probe.py --no-seed          # the negative control
    python3 scripts/picker_probe.py --json
    python3 scripts/picker_probe.py --upstream /tmp/hwa/vendor/upstream \\
        --python /tmp/venv/bin/python
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ── Fixed contract (BLUEPRINT §2, §3, §7) ────────────────────────────────────

#: Provider slug. BLUEPRINT §2.
PROVIDER_SLUG = "opencode-zen-free"
#: Display name as the plugin declares it (``profile.display_name``). BLUEPRINT §2.
PROVIDER_LABEL = "OpenCode Zen (free)"
#: The label the static catalogue renders when it serves before the live rebuild
#: has warmed the cache: ``_PROVIDER_DISPLAY.get(pid, pid.replace("-", " ").title())``.
#: Measured, not guessed — see the module docstring of the unittest.
PROVIDER_LABEL_STATIC = "Opencode Zen Free"
#: Both spellings are accepted; neither is asserted to be *the* one.
ACCEPTED_LABELS: tuple[str, ...] = (PROVIDER_LABEL, PROVIDER_LABEL_STATIC)
#: The nine free ids, in picker order. BLUEPRINT §3.
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
#: Sidecar base URL the seed pins. BLUEPRINT §2 / §7.
SIDECAR_BASE_URL = "http://127.0.0.1:8799/v1"
#: Sentinel, not a secret. BLUEPRINT §7.
PUBLIC_KEY_SENTINEL = "public"

#: Env vars this probe honours.
ENV_UPSTREAM = "OPENCODEZEN4HERMES_UPSTREAM_DIR"
ENV_PYTHON = "OPENCODEZEN4HERMES_TEST_PYTHON"

#: Upstream search order, after the env var.
DEFAULT_UPSTREAM_CANDIDATES: tuple[str, ...] = ("./vendor/upstream", "/tmp/hwa/vendor/upstream")
#: Interpreters tried, in order, after the env override.
DEFAULT_PYTHON_CANDIDATES: tuple[str, ...] = ("/tmp/venv/bin/python",)

#: Third-party modules the upstream tree needs at import time.
REQUIRED_MODULES: tuple[str, ...] = ("yaml", "httpx")

#: Sentinel wrapped around the subprocess's JSON so upstream log noise
#: ("Failed to load plugin ...", "live provider-catalog rebuild exceeded ...")
#: cannot corrupt parsing.
_SENTINEL = "__PICKER_PROBE_JSON__"

#: Default subprocess timeout. ``get_providers()`` alone measured 13-15s on the
#: reference host and the cold path probes provider endpoints, so be generous.
DEFAULT_TIMEOUT = 240.0


# ── The code that runs inside the upstream tree ──────────────────────────────
#
# It runs in a *subprocess*, never in-process: importing ``api.config`` mutates
# sys.path and drags the whole agent tree in, which would poison every other
# test in the same interpreter.

_SANDBOX = '''
import json
from api import config

out = {"ok": True}
try:
    m = config.get_available_models()
except Exception as exc:  # pragma: no cover - reported, not raised
    out = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
else:
    out["groups"] = [
        {
            "provider_id": g.get("provider_id"),
            "provider": g.get("provider"),
            "models": [
                (x.get("id") if isinstance(x, dict) else str(x))
                for x in (g.get("models") or [])
            ],
        }
        for g in (m.get("groups") or [])
    ]
    out["active_provider"] = m.get("active_provider")
    out["default_model"] = m.get("default_model")

    try:
        out["is_plugin_model_provider"] = bool(
            config._is_plugin_model_provider("opencode-zen-free")
        )
    except Exception as exc:
        out["is_plugin_model_provider"] = None
        out["is_plugin_error"] = "%s: %s" % (type(exc).__name__, exc)

    # Secondary evidence: api.providers.get_providers() is slower (it probes
    # every configured provider) so it is opt-outable via --no-providers.
    if __PROVIDERS__:
        try:
            from api.providers import get_providers
        except Exception as exc:
            out["providers_error"] = "import: %s: %s" % (type(exc).__name__, exc)
        else:
            try:
                payload = get_providers()
            except Exception as exc:
                out["providers_error"] = "%s: %s" % (type(exc).__name__, exc)
            else:
                rows = payload.get("providers") if isinstance(payload, dict) else None
                row = None
                if isinstance(rows, list):
                    for candidate in rows:
                        if isinstance(candidate, dict) and str(candidate.get("id") or "") == "opencode-zen-free":
                            row = candidate
                            break
                elif isinstance(rows, dict):
                    row = rows.get("opencode-zen-free")
                out["provider_row"] = (
                    {k: v for k, v in row.items() if k != "models"} if isinstance(row, dict) else None
                )
                out["provider_row_found"] = isinstance(row, dict)

print("__PICKER_PROBE_JSON__" + json.dumps(out, default=str) + "__PICKER_PROBE_JSON__")
'''


# ── Results ──────────────────────────────────────────────────────────────────


@dataclass
class ProbeOutcome:
    """Everything one subprocess run told us.

    ``upstream`` and ``python`` record *which* tree and *which* interpreter
    produced this arm. They are not diagnostics: the negative control is only
    evidence when both arms ran against the same tree with the same interpreter,
    so the caller needs to be able to compare them (see
    ``tests/test_picker_integration.py``'s ``test_the_two_arms_differ_only_by_the_seed``).
    They default to ``None``/``""`` because the vacuous-control placeholder in
    :func:`main` is never run against anything.
    """

    seeded: bool
    active_provider: str
    home: Path
    upstream: Path | None = None
    python: str = ""
    ok: bool = False
    error: str | None = None
    groups: list[dict] = field(default_factory=list)
    active_provider_reported: str | None = None
    default_model: str | None = None
    is_plugin_model_provider: bool | None = None
    provider_row: dict | None = None
    provider_row_found: bool | None = None
    providers_error: str | None = None

    @property
    def seeded_group(self) -> dict | None:
        """The ``opencode-zen-free`` group, or ``None`` when absent."""
        for group in self.groups:
            if group.get("provider_id") == PROVIDER_SLUG:
                return group
        return None

    @property
    def seeded_model_ids_raw(self) -> list[str]:
        group = self.seeded_group
        return list(group.get("models") or []) if group else []

    @property
    def seeded_model_ids(self) -> list[str]:
        """The nine ids with upstream's ``@<provider>:`` routing hint stripped.

        ``_apply_provider_prefix`` qualifies every id with ``@opencode-zen-free:``
        when the slug is *not* the active provider — it is how Hermes keeps a
        model routable back to the plugin that surfaced it (``api/config.py``
        around line 9895). The prefix is upstream routing, not part of the
        roster, so the assertion is made against the bare BLUEPRINT §3 ids and
        :attr:`seeded_model_ids_raw` keeps the wire form for the report.
        """
        prefix = f"@{PROVIDER_SLUG}:"
        return [mid[len(prefix):] if mid.startswith(prefix) else mid for mid in self.seeded_model_ids_raw]

    @property
    def seeded_label(self) -> str | None:
        group = self.seeded_group
        return str(group.get("provider")) if group else None

    @property
    def group_ids(self) -> list[str]:
        return [str(g.get("provider_id")) for g in self.groups]


# ── Discovery ────────────────────────────────────────────────────────────────


def repo_root() -> Path:
    """This repository's root (the parent of ``scripts/``)."""
    return Path(__file__).resolve().parent.parent


def find_upstream(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Return a usable upstream tree, or ``None``.

    An *upstream tree* is a directory holding ``hermes-webui/api/config.py`` and
    ``hermes-agent/providers/``. Search order: *explicit*, then
    ``$OPENCODEZEN4HERMES_UPSTREAM_DIR``, then ``./vendor/upstream``, then
    ``/tmp/hwa/vendor/upstream``.
    """
    candidates: list[Path] = []
    if explicit is not None and str(explicit).strip():
        candidates.append(Path(str(explicit)).expanduser())
    env_dir = os.environ.get(ENV_UPSTREAM, "").strip()
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    for raw in DEFAULT_UPSTREAM_CANDIDATES:
        candidates.append(Path(raw).expanduser())

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if (resolved / "hermes-webui" / "api" / "config.py").is_file() and (
            resolved / "hermes-agent" / "providers"
        ).is_dir():
            return resolved
    return None


def interpreter_error(python: str) -> str | None:
    """Return ``None`` when *python* can import every required module, else why not."""
    probe = "import " + ", ".join(REQUIRED_MODULES)
    try:
        completed = subprocess.run(
            [python, "-c", probe],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError as exc:
        return f"cannot execute {python}: {exc}"
    except subprocess.TimeoutExpired:
        return f"{python} timed out importing {', '.join(REQUIRED_MODULES)}"
    if completed.returncode == 0:
        return None
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return f"{python} cannot import {', '.join(REQUIRED_MODULES)}: {detail[-1] if detail else 'unknown error'}"


def find_interpreter(explicit: str | os.PathLike[str] | None = None) -> tuple[str, list[str]]:
    """Return ``(python, notes)`` for the first interpreter that has the deps.

    ``python`` is ``""`` when nothing worked; ``notes`` records why each
    candidate was rejected, so the skip message can be specific.
    """
    notes: list[str] = []
    candidates: list[str] = []
    if explicit is not None and str(explicit).strip():
        candidates.append(str(Path(str(explicit)).expanduser()))
    env_python = os.environ.get(ENV_PYTHON, "").strip()
    if env_python:
        candidates.append(env_python)
    candidates.extend(DEFAULT_PYTHON_CANDIDATES)
    candidates.append(sys.executable)

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        direct = Path(candidate)
        if os.sep in candidate and not direct.exists():
            notes.append(f"{candidate}: not present")
            continue
        problem = interpreter_error(candidate)
        if problem is None:
            return candidate, notes
        notes.append(problem)
    return "", notes


# ── The probe itself ─────────────────────────────────────────────────────────


def _control_config() -> str:
    """The ``config.yaml`` used when the slug must be admitted by its own seed.

    ``model.provider`` points at *another* provider on purpose. That is what
    makes the seed load-bearing: the slug can then only reach the catalogue via
    its own ``providers.opencode-zen-free`` entry.
    """
    return (
        "model:\n"
        "  provider: anthropic\n"
        "  name: claude-sonnet-4\n"
        "providers:\n"
        "  anthropic:\n"
        "    api_key: sk-not-real\n"
    )


def _seeded_config() -> str:
    """The control config plus the BLUEPRINT §7 seed block."""
    return _control_config() + (
        f"  {PROVIDER_SLUG}:\n"
        f"    name: {PROVIDER_LABEL}\n"
        f"    base_url: {SIDECAR_BASE_URL}\n"
        f"    api_key: {PUBLIC_KEY_SENTINEL}\n"
        "    models_discovered: false\n"
    )


def _active_config() -> str:
    """``model.provider`` is the slug itself — the Lead's hand-run recipe."""
    return (
        "model:\n"
        f"  provider: {PROVIDER_SLUG}\n"
        "  name: deepseek-v4.1-flash-free\n"
    )


def probe_config_text(*, seeded: bool, active_provider: str) -> str:
    """Render the ``config.yaml`` for one probe arm."""
    if active_provider == PROVIDER_SLUG:
        text = _active_config()
        if seeded:
            text += (
                "providers:\n"
                f"  {PROVIDER_SLUG}:\n"
                f"    name: {PROVIDER_LABEL}\n"
                f"    base_url: {SIDECAR_BASE_URL}\n"
                f"    api_key: {PUBLIC_KEY_SENTINEL}\n"
                "    models_discovered: false\n"
            )
        return text
    return _seeded_config() if seeded else _control_config()


def _parse_sandbox_output(stdout: str) -> dict | None:
    """Extract the JSON payload from a noisy subprocess stdout."""
    start = stdout.find(_SENTINEL)
    if start < 0:
        return None
    start += len(_SENTINEL)
    end = stdout.find(_SENTINEL, start)
    if end < 0:
        return None
    try:
        payload = json.loads(stdout[start:end])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def run_probe(
    *,
    upstream: Path,
    python: str,
    seeded: bool,
    active_provider: str = "anthropic",
    with_providers: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    home: Path | None = None,
    plugin_source: Path | None = None,
) -> ProbeOutcome:
    """Build a throwaway ``HERMES_HOME`` and ask the upstream picker about it.

    The upstream tree is never modified: it is only put on ``PYTHONPATH``. The
    ``HERMES_HOME`` is a fresh temp directory (or *home* when given, in which
    case the caller owns its lifetime).
    """
    owns_home = home is None
    home_dir = Path(tempfile.mkdtemp(prefix="picker-probe-")) if home is None else Path(home)
    plugin_src = (
        Path(plugin_source)
        if plugin_source is not None
        else repo_root() / "plugin" / PROVIDER_SLUG
    )

    outcome = ProbeOutcome(
        seeded=seeded,
        active_provider=active_provider,
        home=home_dir,
        upstream=Path(upstream),
        python=python,
    )
    try:
        plugins_dir = home_dir / "plugins" / "model-providers"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(plugin_src, plugins_dir / PROVIDER_SLUG, dirs_exist_ok=True)

        config_path = home_dir / "config.yaml"
        config_path.write_text(
            probe_config_text(seeded=seeded, active_provider=active_provider),
            encoding="utf-8",
        )

        # Hermes memoises the parsed config on (mtime_ns, size) and the picker
        # has its own cache; a fresh HERMES_HOME makes cache state irrelevant,
        # but an isolated HOME also keeps ~/.hermes/.env out of the picture.
        env = dict(os.environ)
        env["HERMES_HOME"] = str(home_dir)
        env["HERMES_CONFIG_PATH"] = str(config_path)
        env["HOME"] = str(home_dir)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(upstream / "hermes-webui"), str(upstream / "hermes-agent")]
        )
        # 0 selects the legacy synchronous catalogue build (api/config.py, the
        # HERMES_WEBUI_MODELS_REBUILD_BUDGET comment). Without it the rebuild is
        # abandoned at its 4s budget and the *static* catalogue answer is served,
        # whose label is the title-cased slug rather than the plugin display name.
        env["HERMES_WEBUI_MODELS_REBUILD_BUDGET"] = "0"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for noisy in ("PYTHONWARNINGS",):
            env.pop(noisy, None)

        source = _SANDBOX.replace("__PROVIDERS__", "True" if with_providers else "False")

        try:
            completed = subprocess.run(
                [python, "-c", source],
                cwd=str(upstream / "hermes-webui"),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            outcome.error = f"the upstream tree did not answer within {timeout:g}s"
            return outcome
        except OSError as exc:
            outcome.error = f"cannot execute {python}: {exc}"
            return outcome

        payload = _parse_sandbox_output(completed.stdout or "")
        if payload is None:
            tail = "\n".join((completed.stderr or completed.stdout or "").strip().splitlines()[-6:])
            outcome.error = (
                "no JSON payload from the upstream tree "
                f"(exit {completed.returncode}); last output:\n{tail}"
            )
            return outcome

        outcome.ok = bool(payload.get("ok"))
        if not outcome.ok:
            outcome.error = str(payload.get("error") or "unknown failure")
            return outcome

        raw_groups = payload.get("groups") or []
        outcome.groups = [g for g in raw_groups if isinstance(g, dict)]
        outcome.active_provider_reported = payload.get("active_provider")
        outcome.default_model = payload.get("default_model")
        outcome.is_plugin_model_provider = payload.get("is_plugin_model_provider")
        outcome.provider_row = payload.get("provider_row")
        outcome.provider_row_found = payload.get("provider_row_found")
        outcome.providers_error = payload.get("providers_error")
        return outcome
    finally:
        if owns_home:
            shutil.rmtree(home_dir, ignore_errors=True)


# ── Verdict ──────────────────────────────────────────────────────────────────

EXPECTED_WITH_SEED = "group present"
EXPECTED_WITHOUT_SEED = "group absent"


def check(
    outcome: ProbeOutcome,
    *,
    expected_models: tuple[str, ...] = EXPECTED_MODELS,
    accepted_labels: tuple[str, ...] = ACCEPTED_LABELS,
    expect_row: bool = True,
) -> list[str]:
    """Return the list of problems; empty means the arm behaved as expected.

    With the seed: the group must exist, carry every expected id in order, and
    be labelled with one of the accepted spellings. Without the seed: the group
    must NOT exist (that is the whole point).

    The label check is deliberately a set membership, not an equality: the
    catalogue renders ``plugin.display_name`` once the live rebuild has warmed
    the cache and ``slug.replace("-", " ").title()`` before that. Both were
    measured; asserting one spelling would make this probe flaky by design.
    """
    problems: list[str] = []
    if not outcome.ok:
        return [f"probe did not produce a catalogue: {outcome.error}"]

    group = outcome.seeded_group

    if not outcome.seeded:
        if group is not None:
            problems.append(
                f"negative control failed: '{PROVIDER_SLUG}' surfaced WITHOUT the config "
                f"seed (active provider was {outcome.active_provider!r}); groups="
                f"{outcome.group_ids}. The seed would not be load-bearing here."
            )
        return problems

    if group is None:
        problems.append(
            f"'{PROVIDER_SLUG}' is missing from get_available_models() even though the "
            f"BLUEPRINT §7 seed is in place; groups={outcome.group_ids}"
        )
        return problems

    ids = outcome.seeded_model_ids
    if ids != list(expected_models):
        problems.append(
            f"model ids differ: expected {list(expected_models)}, got {ids} "
            f"(raw from upstream: {outcome.seeded_model_ids_raw})"
        )

    label = outcome.seeded_label
    if label not in accepted_labels:
        problems.append(
            f"label is {label!r}, expected one of {list(accepted_labels)}"
        )

    if expect_row:
        if outcome.providers_error:
            problems.append(f"get_providers() failed: {outcome.providers_error}")
        elif outcome.provider_row_found is None:
            problems.append("get_providers() was not exercised")
        elif not outcome.provider_row_found:
            problems.append("get_providers() has no row for the slug")
        else:
            row = outcome.provider_row or {}
            if row.get("has_key") is not True:
                problems.append(f"provider row has_key is {row.get('has_key')!r}, expected True")
            if row.get("key_source") != "config_yaml":
                problems.append(
                    f"provider row key_source is {row.get('key_source')!r}, expected 'config_yaml'"
                )
            if row.get("models_total") != len(expected_models):
                problems.append(
                    f"provider row models_total is {row.get('models_total')!r}, "
                    f"expected {len(expected_models)}"
                )
    return problems


# ── Reporting ────────────────────────────────────────────────────────────────


def format_outcome(outcome: ProbeOutcome, problems: list[str]) -> str:
    """Human-readable block for one arm."""
    arm = "seed present" if outcome.seeded else "negative control (no seed)"
    lines = [f"── {arm} ── (active provider: {outcome.active_provider})"]

    if not outcome.ok:
        lines.append(f"  could not read the catalogue: {outcome.error}")
        return "\n".join(lines)

    if not outcome.groups:
        lines.append("  groups: (none)")
    for group in outcome.groups:
        ids = group.get("models") or []
        marker = "  <-- expected" if group.get("provider_id") == PROVIDER_SLUG else ""
        lines.append(
            f"  {group.get('provider_id')}: {len(ids)} model(s), "
            f"label {group.get('provider')!r}{marker}"
        )
        if group.get("provider_id") == PROVIDER_SLUG:
            lines.append(f"    ids: {' '.join(str(i) for i in ids)}")

    if outcome.is_plugin_model_provider is not None:
        lines.append(f"  config._is_plugin_model_provider({PROVIDER_SLUG!r}) = {outcome.is_plugin_model_provider}")
    if outcome.provider_row_found:
        row = outcome.provider_row or {}
        lines.append(
            "  providers.get_providers() row: "
            f"has_key={row.get('has_key')} key_source={row.get('key_source')!r} "
            f"models_total={row.get('models_total')} display_name={row.get('display_name')!r}"
        )
    elif outcome.providers_error:
        lines.append(f"  providers.get_providers() unavailable: {outcome.providers_error}")

    for problem in problems:
        lines.append(f"  PROBLEM: {problem}")
    if not problems:
        lines.append("  verdict: as expected")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="picker_probe.py",
        description=(
            "Ask a real hermes-webui tree which providers get_available_models() surfaces, "
            "with and without the BLUEPRINT §7 config seed. This is the evidence that the "
            "seed — not the plugin alone — is what admits opencode-zen-free into the picker."
        ),
        epilog=(
            "Discovery and skip conditions:\n"
            f"  upstream tree   --upstream DIR, else ${ENV_UPSTREAM}, else "
            f"{', '.join(DEFAULT_UPSTREAM_CANDIDATES)}. Must contain "
            "hermes-webui/api/config.py and hermes-agent/providers/.\n"
            f"  interpreter     --python PATH, else ${ENV_PYTHON}, else "
            f"{', '.join(DEFAULT_PYTHON_CANDIDATES)}, else the current interpreter. "
            f"Must import {' + '.join(REQUIRED_MODULES)}; a bare system python3 has neither.\n"
            "  exit 2          the probe could not run (missing upstream or interpreter) — "
            "the same condition tests/test_picker_integration.py skips on.\n"
            "\n"
            "Which configuration is probed:\n"
            "  By default model.provider is set to another provider (the control shape), so "
            "the slug can only enter the catalogue through its own seed entry. "
            "With --active-provider opencode-zen-free the slug is admitted by the "
            "active-provider branch instead and the seed stops being load-bearing; the "
            "probe says so rather than claiming credit for the seed.\n"
            "\n"
            "The probe never touches the upstream tree (it goes on PYTHONPATH only) and "
            "never needs the sidecar: it reads the config seed, not localhost:8799."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--upstream", metavar="DIR", default=None, help="upstream tree to probe")
    parser.add_argument("--python", metavar="PATH", default=None, help="interpreter for the probe subprocess")
    parser.add_argument(
        "--active-provider",
        metavar="SLUG",
        default="anthropic",
        help=(
            "value of model.provider in the generated config.yaml (default: anthropic, the "
            "discriminating shape). Use opencode-zen-free to reproduce the hand-run recipe, "
            "where the seed is NOT what admits the group"
        ),
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="run only the negative control arm, with the seed omitted from config.yaml",
    )
    parser.add_argument(
        "--no-providers",
        action="store_true",
        help="skip the api.providers.get_providers() cross-check (it probes every provider and is slow)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"per-arm subprocess timeout (default {DEFAULT_TIMEOUT:g}s)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    upstream = find_upstream(args.upstream)
    if upstream is None:
        message = (
            "no upstream hermes-webui + hermes-agent tree found; pass --upstream DIR or set "
            f"{ENV_UPSTREAM}. Looked at: --upstream, ${ENV_UPSTREAM}, "
            f"{', '.join(DEFAULT_UPSTREAM_CANDIDATES)}"
        )
        if args.json:
            print(json.dumps({"ok": False, "skipped": True, "reason": message}, indent=2))
        else:
            print(f"picker_probe: SKIP — {message}", file=sys.stderr)
        return 2

    python, notes = find_interpreter(args.python)
    if not python:
        message = (
            f"no interpreter can import {' and '.join(REQUIRED_MODULES)}; pass --python PATH or "
            f"set {ENV_PYTHON}. Tried: " + "; ".join(notes)
        )
        if args.json:
            print(json.dumps({"ok": False, "skipped": True, "reason": message}, indent=2))
        else:
            print(f"picker_probe: SKIP — {message}", file=sys.stderr)
        return 2

    if not args.json:
        print(f"picker_probe: upstream {upstream}")
        print(f"picker_probe: interpreter {python}")
        print(
            "picker_probe: note — 0 means the picker reads the config seed; the live sidecar "
            "on 127.0.0.1:8799 is irrelevant here and is never contacted."
        )
        print()

    arms: list[ProbeOutcome] = []
    all_problems: list[str] = []
    # Both arms by default: the *pair* is the finding. ``--no-seed`` narrows the
    # run to the negative control alone — the quick "did anything leak in?" check.
    plan: tuple[bool, ...] = (False,) if args.no_seed else (True, False)
    for seeded in plan:
        if not seeded and not _negative_control_is_meaningful(args.active_provider):
            outcome = ProbeOutcome(seeded=False, active_provider=args.active_provider, home=Path("."))
            outcome.ok = True
            outcome.error = (
                "not run: with --active-provider opencode-zen-free the slug is admitted by the "
                "active-provider branch (api/config.py:7434) regardless of the seed, so this "
                "control cannot discriminate. Use the default --active-provider to run it."
            )
            arms.append(outcome)
            if not args.json:
                print(f"── negative control (no seed) ── ({outcome.error})\n")
            continue

        outcome = run_probe(
            upstream=upstream,
            python=python,
            seeded=seeded,
            active_provider=args.active_provider,
            with_providers=not args.no_providers,
            timeout=args.timeout,
        )
        problems = check(outcome, expect_row=not args.no_providers)
        arms.append(outcome)
        all_problems.extend(f"[{'seed' if seeded else 'no-seed'}] {p}" for p in problems)
        if not args.json:
            print(format_outcome(outcome, problems))
            print()

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not all_problems,
                    "upstream": str(upstream),
                    "python": python,
                    "active_provider": args.active_provider,
                    "expected_models": list(EXPECTED_MODELS),
                    "arms": [
                        {
                            "seeded": outcome.seeded,
                            "note": outcome.error if not outcome.ok else None,
                            "groups": outcome.groups,
                            "group_ids": outcome.group_ids,
                            "seeded_label": outcome.seeded_label,
                            "is_plugin_model_provider": outcome.is_plugin_model_provider,
                            "provider_row": outcome.provider_row,
                            "provider_row_found": outcome.provider_row_found,
                        }
                        for outcome in arms
                    ],
                    "problems": all_problems,
                },
                indent=2,
                default=str,
            )
        )
        return 0 if not all_problems else 1

    if all_problems:
        print("picker_probe: FAIL")
        for problem in all_problems:
            print(f"  - {problem}")
        return 1

    if args.no_seed:
        # Only the control ran, so do not claim the pair was proven.
        print(
            f"picker_probe: PASS — without the BLUEPRINT §7 seed {PROVIDER_SLUG!r} does not "
            "reach the picker. Run again without --no-seed to prove the seed is what admits it."
        )
        return 0

    print(
        "picker_probe: PASS — the BLUEPRINT §7 seed is what admits "
        f"{PROVIDER_SLUG!r} into the picker with all {len(EXPECTED_MODELS)} models, and "
        "without it the group does not appear."
    )
    if args.active_provider == PROVIDER_SLUG:
        print(
            "  caveat: --active-provider was the slug itself, so the group would also appear "
            "without the seed via the active-provider branch; re-run without it to test the seed."
        )
    return 0


def _negative_control_is_meaningful(active_provider: str) -> bool:
    """False when the slug is the active provider, which admits it seedlessly."""
    return active_provider.strip().lower() != PROVIDER_SLUG


if __name__ == "__main__":
    sys.exit(main())
