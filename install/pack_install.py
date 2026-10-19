#!/usr/bin/env python3
"""Install opencodezen4hermes into a built ``hermes-webui-agent`` pack stage.

Why this file exists
--------------------
``build.sh`` produces the upstream arm64 pack (``hermes-webui/``,
``hermes-agent/``, ``.venv/``, ``bin/``). Everything this repository adds is
bolted on afterwards by this single idempotent step, so ``build.sh`` only needs
one new line and the existing eight stages keep their behaviour:

    python3 "$REPO_ROOT/install/pack_install.py" --pack "$STAGE"

What it lays down (``docs/BLUEPRINT.md`` §4)
-------------------------------------------
1. ``sidecar/``         -> ``<pack>/sidecar/`` (package + ``gate/`` payloads)
2. ``plugin/opencode-zen-free/`` -> ``<pack>/hermes-agent/plugins/model-providers/opencode-zen-free/``
3. ``install/seed_config.py``    -> ``<pack>/install/seed_config.py``
4. ``<pack>/bin/opencodezen4hermes-sidecar`` — standalone sidecar control (§5)
5. ``<pack>/bin/hermes-webui`` — the upstream launcher **extended** to start the
   sidecar, seed the config, and only then exec the WebUI (§6)

The original launcher is preserved once as ``bin/hermes-webui.upstream``. It is
never overwritten afterwards, not even by a re-run: the upstream file is
evidence, and silently replacing it with our own output would make it useless.

Contract
--------
* **Idempotent.** A second run is a no-op on content. Directories this tool owns
  (``<pack>/sidecar``, ``<pack>/.../opencode-zen-free``) are replaced wholesale
  rather than merged, so a module deleted upstream does not linger as a stale
  ``.pyc``-adjacent file in the pack.
* **Fails loudly.** A directory that is not a pack (no ``hermes-webui/server.py``
  or no ``.venv/bin/python``) is refused with exit 1 before anything is written.
* **Atomic files.** Generated scripts are written to a temp file in the target
  directory, fsynced, then ``os.replace``d, so a crash cannot leave a truncated
  launcher behind.
* **``--dry-run`` writes nothing**, not even ``bin/`` or a backup.

Usage
-----
    python3 install/pack_install.py --pack dist/stage
    python3 install/pack_install.py --pack ./stage --port 9123 --dry-run

Exit codes: ``0`` on success (including a no-op re-run), ``1`` on refusal or any
source that is missing from the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

#: Marker lines that identify *our* output. They let a re-run tell the upstream
#: launcher apart from the extended one we generated, so we never back up our
#: own artifact as if it were the original.
LAUNCHER_MARKER = "# opencodezen4hermes: extended launcher (BLUEPRINT §6)"
SIDECAR_MARKER = "# opencodezen4hermes: pack sidecar control script (BLUEPRINT §5)"

#: Must agree with ``sidecar/config.py`` DEFAULT_PORT and the config seed.
DEFAULT_SIDECAR_PORT = 8799

#: Never copy bytecode or caches into the pack.
IGNORE_PATTERNS = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")

#: ``@@PORT@@`` is substituted with the install-time sidecar port. A plain token
#: is used instead of ``str.format`` because the templates are full of bash
#: ``${...}`` expansions.
PORT_TOKEN = "@@PORT@@"


LAUNCHER_TEMPLATE = r"""#!/usr/bin/env bash
# opencodezen4hermes: extended launcher (BLUEPRINT §6)
#
# Upstream behaviour, plus three best-effort steps that run BEFORE the WebUI is
# exec'd. The order below is mandatory (BLUEPRINT §6):
#
#   1. ensure the sidecar  — probe /healthz, start bin/opencodezen4hermes-sidecar
#                            detached when it is silent, wait bounded, continue.
#   2. ensure the config seed — run install/seed_config.py idempotently so the
#                            WebUI picker admits the `opencode-zen-free` slug.
#   3. exec the WebUI exactly as the upstream launcher does.
#
# Every step logs to $STATE/launcher.log and every failure is a WARNING, never
# fatal: a user whose WebUI refuses to start because a free-tier convenience
# failed is a worse outcome than a picker with no free group.
set -euo pipefail

SELF="$(realpath "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"

# --- upstream environment (unchanged from the original launcher) -------------
export HERMES_WEBUI_AGENT_DIR="$ROOT/hermes-agent"
export HERMES_WEBUI_PYTHON="$ROOT/.venv/bin/python"
export HERMES_WEBUI_PORT="${HERMES_WEBUI_PORT:-8789}"
STATE="${HERMES_WEBUI_STATE_DIR:-$ROOT/state}"
mkdir -p "$STATE"
LOG="$STATE/launcher.log"

# The sidecar listens on its OWN port (default @@PORT@@), never on
# HERMES_WEBUI_PORT — conflating the two would make step 1 probe the WebUI.
SIDECAR_PORT="${OPENCODEZEN4HERMES_PORT:-@@PORT@@}"
SIDECAR_HOST="${OPENCODEZEN4HERMES_HOST:-127.0.0.1}"
SIDECAR_URL="http://$SIDECAR_HOST:$SIDECAR_PORT"
SIDECAR_BIN="$ROOT/bin/opencodezen4hermes-sidecar"
SIDECAR_LOG="$STATE/sidecar.log"
SEED_SCRIPT="$ROOT/install/seed_config.py"

# Bounded wait for the sidecar, in half-second ticks (default 20 ticks = 10 s).
# The sidecar warms its catalog for up to 5 s before it binds, so a shorter
# window would report "timed out" on a perfectly healthy first boot.
HEALTH_TRIES="${OPENCODEZEN4HERMES_LAUNCHER_TICKS:-20}"

log() {
  printf '%s [launcher] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG" 2>/dev/null || true
}

warn() {
  log "WARN $*"
  printf '%s\n' "hermes-webui: WARN $*" >&2 || true
}

# Liveness probe. /healthz is the only liveness contract (BLUEPRINT §5) and it
# must never depend on the upstream lane being reachable, so this only asks the
# local sidecar process. Stdlib urllib only: curl may not exist on a pack host.
probe_healthz() {
  "$HERMES_WEBUI_PYTHON" -c '
import json
import sys
import urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
        print("ok" if json.loads(response.read()).get("ok") is True else "no")
except Exception:
    print("no")
' "$SIDECAR_URL/healthz" 2>/dev/null
}

# --- 1/3 ensure the sidecar --------------------------------------------------
if [ "$(probe_healthz)" = "ok" ]; then
  log "step 1/3 sidecar already answering at $SIDECAR_URL"
elif [ ! -x "$SIDECAR_BIN" ]; then
  warn "step 1/3 $SIDECAR_BIN is missing or not executable — not starting a sidecar"
else
  log "step 1/3 sidecar silent at $SIDECAR_URL — starting it detached"
  # PYTHONPATH="$ROOT" is what makes `-m sidecar` work: inside a pack the
  # sidecar package sits at $ROOT/sidecar and is never pip-installed, so a bare
  # `-m sidecar` would raise ModuleNotFoundError.
  if command -v setsid >/dev/null 2>&1; then
    PYTHONPATH="$ROOT" OPENCODEZEN4HERMES_PORT="$SIDECAR_PORT" \
      setsid "$SIDECAR_BIN" >>"$SIDECAR_LOG" 2>&1 &
  else
    PYTHONPATH="$ROOT" OPENCODEZEN4HERMES_PORT="$SIDECAR_PORT" \
      nohup "$SIDECAR_BIN" >>"$SIDECAR_LOG" 2>&1 &
  fi
  disown 2>/dev/null || true
  ticks=0
  while [ "$ticks" -lt "$HEALTH_TRIES" ]; do
    if [ "$(probe_healthz)" = "ok" ]; then
      break
    fi
    sleep 0.5
    ticks=$((ticks + 1))
  done
  if [ "$(probe_healthz)" = "ok" ]; then
    log "step 1/3 sidecar up at $SIDECAR_URL"
  else
    warn "step 1/3 sidecar did not answer $SIDECAR_URL/healthz within $HEALTH_TRIES probe(s) — see $SIDECAR_LOG; continuing anyway"
  fi
fi

# --- 2/3 ensure the config seed ---------------------------------------------
seed_rc=0
if [ ! -f "$SEED_SCRIPT" ]; then
  warn "step 2/3 $SEED_SCRIPT is missing — the picker will not list the free group"
elif [ ! -x "$HERMES_WEBUI_PYTHON" ]; then
  warn "step 2/3 $HERMES_WEBUI_PYTHON is missing — cannot run the config seed"
else
  log "step 2/3 seeding providers.opencode-zen-free on port $SIDECAR_PORT"
  "$HERMES_WEBUI_PYTHON" "$SEED_SCRIPT" --port "$SIDECAR_PORT" >>"$LOG" 2>&1 || seed_rc=$?
  if [ "$seed_rc" -eq 0 ]; then
    log "step 2/3 config seed ok"
  else
    warn "step 2/3 config seed exited $seed_rc — continuing anyway"
  fi
fi

# --- 3/3 exec the WebUI (upstream behaviour, verbatim) -----------------------
log "step 3/3 exec bootstrap.py with HERMES_WEBUI_PORT=$HERMES_WEBUI_PORT"
exec "$HERMES_WEBUI_PYTHON" "$ROOT/hermes-webui/bootstrap.py" --no-browser --host 0.0.0.0 "$@"
"""


SIDECAR_SCRIPT_TEMPLATE = r"""#!/usr/bin/env bash
# opencodezen4hermes: pack sidecar control script (BLUEPRINT §5)
#
#   <pack>/bin/opencodezen4hermes-sidecar [--port N] [--verbose] ...
#
# Why PYTHONPATH="$ROOT" is required: inside a pack the sidecar package lives at
# <pack>/sidecar and is never installed into site-packages, so plain
# `$ROOT/.venv/bin/python -m sidecar` exits with "No module named sidecar".
# Prepending the pack root to PYTHONPATH makes the package importable without an
# install step. (In the development checkout the same thing happens for free by
# running `python3 -m sidecar` from the repository root, which is why the
# dev entry point needs no PYTHONPATH.)
#
# The interpreter is the pack venv rather than the system python3: the venv is
# the one bundled in the pack and the only guaranteed-present interpreter.
set -euo pipefail

SELF="$(realpath "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"
PY="$ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  printf '%s\n' "opencodezen4hermes-sidecar: $PY is missing — is this a pack root?" >&2
  exit 1
fi

# Default to the port chosen at install time unless the caller overrides it.
# An explicit --port on the command line still wins: sidecar/config.py applies
# CLI flags after the environment.
export OPENCODEZEN4HERMES_PORT="${OPENCODEZEN4HERMES_PORT:-@@PORT@@}"

# Prepend $ROOT only when it is not already there: bin/hermes-webui also exports
# PYTHONPATH when it starts this script, and an unconditional prepend would
# leave the pack root in the path twice.
case ":${PYTHONPATH:-}:" in
  *":$ROOT:"*) ;;
  *) export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" ;;
esac

exec "$PY" -m sidecar "$@"
"""


class InstallError(RuntimeError):
    """Raised when the pack or the repository source is unusable."""


class Result(NamedTuple):
    """One installed artifact, for reporting and for tests."""

    #: short identifier: sidecar, plugin, seed, sidecar-script, launcher, ...
    action: str
    #: where it landed, as an absolute path
    target: Path
    #: created / updated / unchanged
    status: str
    #: human note for the run log (e.g. what happened to the upstream backup)
    note: str = ""

    def display(self, pack: Path) -> str:
        """``<pack>/bin/hermes-webui`` — pack-relative, because pack roots vary."""
        try:
            shown = self.target.relative_to(pack).as_posix()
        except ValueError:
            shown = str(self.target)
        suffix = f" ({self.note})" if self.note else ""
        return f"[{self.status}] <pack>/{shown}{suffix}"


# --------------------------------------------------------------------------- #
# filesystem helpers
# --------------------------------------------------------------------------- #
def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync so a rename survives a power cut."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_text_atomic(
    path: Path,
    text: str,
    *,
    executable: bool = False,
    dry_run: bool = False,
) -> str:
    """Write ``text`` atomically, returning ``created``/``updated``/``unchanged``.

    The mode is repaired when the content already matches: a pack that lost its
    exec bit is a real failure mode (``chmod`` after an rsync without ``-p``),
    and rewriting the same bytes is cheaper than debugging it.
    """
    mode = 0o755 if executable else 0o644
    data = text.encode("utf-8")
    exists = path.is_file()
    if exists and path.read_bytes() == data:
        if not dry_run and (path.stat().st_mode & 0o777) != mode:
            os.chmod(path, mode)
        return "unchanged"
    action = "updated" if exists else "created"
    if dry_run:
        return action
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return action


def _tree_digest(root: Path) -> str:
    """Content digest of a directory tree, ignoring bytecode caches."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            digest.update(b"L" + relative.as_posix().encode() + b"\0" + os.readlink(path).encode())
        elif path.is_file():
            digest.update(b"F" + relative.as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
        elif path.is_dir():
            digest.update(b"D" + relative.as_posix().encode() + b"\0")
    return digest.hexdigest()


def copy_tree(src: Path, dest: Path, *, dry_run: bool = False) -> str:
    """Replace ``dest`` with a pristine copy of ``src``, minus bytecode caches.

    Replacement rather than merge is deliberate: these directories are wholly
    owned by this repository (BLUEPRINT §4), and a merge would leave modules
    that no longer exist upstream sitting in the pack.

    An identical tree is left untouched and reported as ``unchanged``, so a
    re-run does not churn mtimes (Hermes caches parsed config on
    ``(mtime_ns, size)``, and a builder re-running pack_install would otherwise
    invalidate every one of those caches).
    """
    if not src.is_dir():
        raise InstallError(f"missing source directory: {src}")
    fresh = _tree_digest(src)
    if dest.is_dir() and _tree_digest(dest) == fresh:
        return "unchanged"
    action = "updated" if dest.exists() else "created"
    if dry_run:
        return action
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=IGNORE_PATTERNS)
    # Verify what landed instead of trusting the copy.
    landed = _tree_digest(dest)
    if landed != fresh:
        raise InstallError(f"copy mismatch into {dest}: {landed} != {fresh}")
    return action


def validate_pack(pack: Path) -> list[str]:
    """Return the reasons ``pack`` is not a pack. Empty list means usable."""
    if not pack.is_dir():
        return [f"not a directory: {pack}"]
    problems = []
    server = pack / "hermes-webui" / "server.py"
    if not server.is_file():
        problems.append(f"missing {server} (is this really a pack root?)")
    python = pack / ".venv" / "bin" / "python"
    # exists() follows symlinks on purpose: the pack venv python is a relative
    # symlink into .pyroot, and a dangling one is as broken as a missing file.
    if not python.exists():
        problems.append(f"missing {python} (pack venv not present?)")
    return problems


# --------------------------------------------------------------------------- #
# the install itself
# --------------------------------------------------------------------------- #
def install(
    pack: Path,
    repo_root: Path,
    *,
    port: int = DEFAULT_SIDECAR_PORT,
    dry_run: bool = False,
) -> list[tuple[str, str, str]]:
    """Install every artifact into ``pack``.

    Returns one :class:`Result` per artifact for reporting and for tests.
    Raises :class:`InstallError` when the pack or a repository source is unusable.
    """
    for required in ("sidecar", "plugin/opencode-zen-free", "install/seed_config.py"):
        if not (repo_root / required).exists():
            raise InstallError(
                f"repository source missing: {repo_root / required} "
                "(run with --repo-root pointing at the opencodezen4hermes checkout)"
            )

    results: list[Result] = []

    # 1. the sidecar package, including gate/official_tools.json and the two
    #    official_*.txt payloads that sidecar/gate.py reads at request time.
    results.append(
        Result("sidecar", pack / "sidecar", copy_tree(repo_root / "sidecar", pack / "sidecar", dry_run=dry_run))
    )

    # 2. the Hermes provider plugin.
    plugin_dest = pack / "hermes-agent" / "plugins" / "model-providers" / "opencode-zen-free"
    results.append(
        Result(
            "plugin",
            plugin_dest,
            copy_tree(repo_root / "plugin" / "opencode-zen-free", plugin_dest, dry_run=dry_run),
        )
    )

    # 3. the config seed, invoked by the extended launcher (BLUEPRINT §6 step 2).
    #    Only seed_config.py ships; pack_install.py itself is not needed in a pack.
    seed_src = repo_root / "install" / "seed_config.py"
    results.append(
        Result(
            "seed",
            pack / "install" / "seed_config.py",
            write_text_atomic(
                pack / "install" / "seed_config.py",
                seed_src.read_text(encoding="utf-8"),
                dry_run=dry_run,
            ),
        )
    )

    # 4. the standalone sidecar control script (BLUEPRINT §5).
    results.append(
        Result(
            "sidecar-script",
            pack / "bin" / "opencodezen4hermes-sidecar",
            write_text_atomic(
                pack / "bin" / "opencodezen4hermes-sidecar",
                SIDECAR_SCRIPT_TEMPLATE.replace(PORT_TOKEN, str(port)),
                executable=True,
                dry_run=dry_run,
            ),
        )
    )

    # 5. keep the upstream launcher as evidence, once. "Once" is the whole point:
    #    after the first install the file at bin/hermes-webui is our output, so a
    #    re-run must neither overwrite an existing backup nor replace a real
    #    upstream backup with the extended launcher.
    launcher = pack / "bin" / "hermes-webui"
    upstream = pack / "bin" / "hermes-webui.upstream"
    if upstream.exists():
        note = "upstream backup kept"
    elif not launcher.is_file():
        note = "no upstream launcher present — nothing to back up"
    elif LAUNCHER_MARKER in launcher.read_text(encoding="utf-8", errors="replace"):
        note = "existing launcher is already extended — no upstream to back up"
    elif dry_run:
        results.append(Result("launcher-backup", upstream, "created", "would be written"))
        note = ""
    else:
        upstream.parent.mkdir(parents=True, exist_ok=True)
        # copy2 preserves the upstream mode; the launcher is already executable.
        shutil.copy2(launcher, upstream)
        results.append(Result("launcher-backup", upstream, "created"))
        note = ""

    results.append(
        Result(
            "launcher",
            launcher,
            write_text_atomic(
                launcher,
                LAUNCHER_TEMPLATE.replace(PORT_TOKEN, str(port)),
                executable=True,
                dry_run=dry_run,
            ),
            note,
        )
    )
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pack_install",
        description=(
            "Install the opencodezen4hermes sidecar, provider plugin, config seed "
            "and extended launchers into a built hermes-webui-agent pack stage "
            "(docs/BLUEPRINT.md §4). Idempotent; refuses anything that is not a pack."
        ),
    )
    parser.add_argument(
        "--pack",
        metavar="DIR",
        required=True,
        help="pack root, shaped like the tarball contents (hermes-webui/, hermes-agent/, .venv/, bin/)",
    )
    parser.add_argument(
        "--repo-root",
        metavar="DIR",
        default=None,
        help="opencodezen4hermes checkout to copy from (default: the parent of install/)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_SIDECAR_PORT,
        help=f"sidecar port baked into the launchers and the config seed (default {DEFAULT_SIDECAR_PORT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing, creating or backing up anything",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.port < 1 or args.port > 65535:
        print(f"pack_install: --port must be 1..65535, got {args.port}", file=sys.stderr)
        return 1

    pack = Path(args.pack).expanduser().resolve()
    repo_root = (
        Path(args.repo_root).expanduser().resolve()
        if args.repo_root
        else Path(__file__).resolve().parent.parent
    )

    problems = validate_pack(pack)
    if problems:
        print(f"pack_install: refusing to install into {pack}", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    prefix = "dry-run: " if args.dry_run else ""
    print(f"{prefix}installing opencodezen4hermes into {pack}")
    print(f"{prefix}source: {repo_root}   sidecar port: {args.port}")
    try:
        results = install(pack, repo_root, port=args.port, dry_run=args.dry_run)
    except InstallError as exc:
        print(f"pack_install: {exc}", file=sys.stderr)
        return 1

    for result in results:
        print(f"  {result.display(pack)}")

    if args.dry_run:
        print("dry-run: nothing was written")
    else:
        print(
            "done. Start the WebUI with "
            f"{pack / 'bin' / 'hermes-webui'} — the sidecar is started for you."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
