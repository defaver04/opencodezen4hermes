#!/usr/bin/env python3
"""Seed the ``providers.opencode-zen-free`` route into a Hermes ``config.yaml``.

Why this file exists
--------------------
Bundling the provider plugin is not enough. ``api/config.py`` only admits a
plugin provider into ``detected_providers`` when the provider is already
credentialed **or** when its ``config.yaml`` entry carries a route
(``_has_provider_route`` — the any-of ``api`` / ``base_url`` / ``api_key`` /
``key_env`` check). The anonymous lane has no credential, so without a config
entry the WebUI picker shows an empty group (``groups: []``) even though the
plugin is loaded and healthy. This script writes that entry.

The shape it writes is fixed by ``docs/BLUEPRINT.md`` §7:

```yaml
providers:
  opencode-zen-free:
    name: OpenCode Zen (free)
    base_url: http://127.0.0.1:8799/v1
    api_key: public
    models_discovered: false
```

``api_key: public`` is a sentinel, *not* a credential: the lane is anonymous and
never reads it, but ``_has_provider_route`` needs a non-empty route field and
``api/providers.py`` then reports ``key_source: config_yaml``.
``models_discovered: false`` is deliberate — it pins the curated roster so a
stale or partial live probe cannot empty the picker.

Contract (do not weaken without changing the BLUEPRINT first)
------------------------------------------------------------
* **Merge only.** Read the YAML, add the block, write it back. Other top-level
  keys and other ``providers`` entries are never dropped or reordered.
* **Surgical when possible.** Adding a brand-new block is done by inserting
  text, not by re-dumping the document, so the user's comments and formatting
  survive. Every insertion is verified by re-parsing; if the verification
  disagrees the script falls back to a full ``safe_dump`` (which is correct but
  normalises formatting).
* **Idempotent.** A second run is a true no-op: it does not even touch the file.
  That matters beyond politeness — ``api/config.py`` memoises the parsed config
  on ``(mtime_ns, size)``, so a needless rewrite would invalidate that cache on
  every launcher boot.
* **Atomic.** Temp file in the same directory, ``fsync``, ``os.replace``. A
  crash mid-write can never truncate an existing config.
* **Backed up once.** The first time the file is modified a
  ``config.yaml.bak.<UTC timestamp>`` copy is left beside it. Later runs do not
  pile up more backups.

Usage
-----
    python3 install/seed_config.py                  # seed the default config
    python3 install/seed_config.py --dry-run        # show what would change
    python3 install/seed_config.py --port 9123      # different sidecar port
    python3 install/seed_config.py --config PATH    # explicit file

Exit codes: ``0`` when the seed is in place or already was (created / updated /
unchanged); ``1`` on any failure, including a missing PyYAML, because in that
case the route was **not** written. ``bin/hermes-webui`` treats a non-zero exit
as a warning and boots anyway (BLUEPRINT §6) — convenience must never block the
WebUI.

PyYAML is required to parse YAML. It ships in the pack venv; a bare system
``python3`` usually lacks it. The import is guarded so that importing this
module (which the test suite does) never explodes on a minimal interpreter.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # PyYAML is in the pack venv, not in a bare system python.
    import yaml
except ImportError:  # pragma: no cover - exercised on hosts without PyYAML
    yaml = None  # type: ignore[assignment]

#: True when this interpreter can actually parse/emit YAML.
HAS_YAML = yaml is not None

#: Provider slug. Fixed by BLUEPRINT §2.
PROVIDER_SLUG = "opencode-zen-free"
#: Display name. Fixed by BLUEPRINT §2.
PROVIDER_NAME = "OpenCode Zen (free)"
#: Sidecar bind host. Fixed by BLUEPRINT §2.
PROVIDER_HOST = "127.0.0.1"
#: Sidecar default port. Mirrors ``sidecar/config.py`` DEFAULT_PORT.
DEFAULT_PORT = 8799
#: Sentinel, not a secret: the lane is anonymous and ignores the value.
PUBLIC_KEY_SENTINEL = "public"

#: The keys this script owns inside the managed provider entry, in order.
MANAGED_KEYS: tuple[str, ...] = ("name", "base_url", "api_key", "models_discovered")

#: Status tokens returned by :func:`seed_config`.
STATUS_CREATED = "created"
STATUS_UPDATED = "updated"
STATUS_UNCHANGED = "unchanged"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

_TMP_PREFIX = ".seed-config-"

_PROVIDERS_KEY_RE = re.compile(r"^providers\s*:(?P<rest>.*)$")
_EMPTY_FLOW_RE = re.compile(r"^\s*\{\s*\}\s*(?:#.*)?$")


@dataclass(slots=True)
class SeedResult:
    """Outcome of one :func:`seed_config` invocation."""

    status: str
    message: str
    path: Path
    changed: bool = False
    dry_run: bool = False
    backup_path: Path | None = None

    @property
    def ok(self) -> bool:
        """True unless the seed failed to be established."""
        return self.status != STATUS_FAILED

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def describe(self) -> str:
        """Human phrasing, honest about dry runs."""
        if self.dry_run and self.changed:
            if self.status == STATUS_CREATED:
                return "would create"
            if self.status == STATUS_UPDATED:
                return "would update"
        return self.status


# ── The managed block ────────────────────────────────────────────────────────


def build_block(port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Return the managed ``providers.opencode-zen-free`` entry (BLUEPRINT §7)."""
    return {
        "name": PROVIDER_NAME,
        "base_url": f"http://{PROVIDER_HOST}:{int(port)}/v1",
        "api_key": PUBLIC_KEY_SENTINEL,
        "models_discovered": False,
    }


def _entry_is_seeded(entry: object) -> bool:
    """True when *entry* already carries a usable route (a non-empty base_url)."""
    return isinstance(entry, dict) and bool(str(entry.get("base_url") or "").strip())


def _entry_is_canonical(entry: object, block: dict[str, Any]) -> bool:
    """True when every managed key is present with exactly the shipped value."""
    if not isinstance(entry, dict):
        return False
    return all(entry.get(key) == block[key] for key in MANAGED_KEYS)


# ── Path resolution ──────────────────────────────────────────────────────────


def resolve_config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Return the config file to seed.

    Precedence mirrors ``api/config.py::_get_config_path``: an explicit
    ``--config`` wins, then ``$HERMES_CONFIG_PATH``, then
    ``$HERMES_HOME/config.yaml``, then ``~/.hermes/config.yaml``.
    """
    if explicit is not None and str(explicit).strip():
        return Path(str(explicit)).expanduser()
    env_path = os.environ.get("HERMES_CONFIG_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        return Path(hermes_home).expanduser() / "config.yaml"
    return Path.home() / ".hermes" / "config.yaml"


# ── Text rendering ───────────────────────────────────────────────────────────
#
# The surgeon's rule: never re-serialise a file the user hand-wrote. A full
# ``safe_dump`` is always *correct* but it deletes every comment and reflows
# every line, which is a rude thing to do to someone's config. So the happy path
# edits the text in place, and the caller re-parses the result and compares it to
# the intended document. The dump is the fallback when the surgery disagrees —
# which bounds the worst case at "comments lost", never "config corrupted".

#: Matches a top-level ``providers: ...`` line.
_PROVIDERS_KEY_RE = re.compile(r"^providers\s*:(?P<rest>.*)$")

#: Matches any mapping key line, capturing its indentation and raw key token.
_KEY_LINE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>\S.*?)\s*:(?:\s|$)")

#: A right-hand side that is empty or a flow-mapping placeholder.
_EMPTY_VALUE_RE = re.compile(r"^(?:\{\s*\})?$")


def _strip_comment(raw: str) -> str:
    """Return *raw* without a trailing ``# ...`` comment (quotes respected)."""
    quote: str | None = None
    for index, char in enumerate(raw):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "#":
            return raw[:index]
    return raw


def _key_token_is(raw_key: str, key: str) -> bool:
    """True when a raw YAML key token denotes *key* (quoted forms included)."""
    token = raw_key.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        token = token[1:-1]
    return token == key


def _indent_width(indent: str) -> int:
    return len(indent.expandtabs(8))


def _dump_entry_lines(entry: dict[str, Any], indent: str) -> list[str]:
    """Render one ``<slug>: {...}`` entry as indented YAML lines.

    PyYAML does the quoting, so a name or URL containing YAML metacharacters is
    escaped exactly as the parser expects.
    """
    assert yaml is not None  # callers guard on HAS_YAML
    dumped = yaml.safe_dump(
        {PROVIDER_SLUG: entry},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return [f"{indent}{line}\n" for line in dumped.splitlines() if line.strip()]


@dataclass(slots=True)
class _ProvidersBlock:
    """Where the top-level ``providers`` mapping lives in the raw text."""

    key_index: int
    #: First line index past the block's indented content (exclusive).
    end: int
    #: Indentation string of the block's child lines.
    child_indent: str
    #: Line range of an existing ``opencode-zen-free:`` entry, when present.
    entry_range: tuple[int, int] | None = None


def _scan_providers_block(lines: list[str]) -> _ProvidersBlock | None:
    """Locate the ``providers`` block and our entry inside it, if any.

    Returns ``None`` when there is no usable ``providers:`` line — either the
    key is absent, or it holds an inline flow mapping this function will not
    hand-edit (``providers: {…}``), in which case the caller re-dumps.
    """
    key_index: int | None = None
    for index, line in enumerate(lines):
        match = _PROVIDERS_KEY_RE.match(line)
        if match is None:
            continue
        rest = _strip_comment(match.group("rest")).strip()
        if rest == "" or rest == "{}":  # ``providers:`` / ``providers: {}``
            key_index = index
        break

    if key_index is None:
        return None

    # ``providers: {}`` sits on one line; either way the scan below walks the
    # following indented lines, so an inline-empty key that *is* followed by a
    # body is handled the same as a bare ``providers:`` key.
    start = key_index + 1

    child_indent: str | None = None
    end = start
    entry_range: tuple[int, int] | None = None
    cursor = start
    entry_start: int | None = None
    entry_indent_width: int | None = None

    while cursor < len(lines):
        line = lines[cursor]
        stripped = line.strip()
        if stripped == "" or line.lstrip().startswith("#"):
            cursor += 1
            continue
        if line[0] not in " \t":
            break  # next top-level key ends the block

        indent = line[: len(line) - len(line.lstrip())]
        if child_indent is None:
            child_indent = indent
        end = cursor + 1

        if entry_start is None:
            key_match = _KEY_LINE_RE.match(line)
            if key_match is not None and _key_token_is(key_match.group("key"), PROVIDER_SLUG):
                entry_start = cursor
                entry_indent_width = _indent_width(indent)
        elif entry_indent_width is not None and _indent_width(indent) <= entry_indent_width:
            # A sibling entry at the same level starts; ours ended before it.
            entry_range = (entry_start, cursor)
            entry_start = None
        cursor += 1

    if entry_start is not None:
        entry_range = (entry_start, end)

    return _ProvidersBlock(
        key_index=key_index,
        end=end,
        child_indent=child_indent or "  ",
        entry_range=entry_range,
    )


def _render_surgical(text: str, entry: dict[str, Any]) -> str | None:
    """Add or replace the managed entry without re-dumping the document.

    Returns the new file text, or ``None`` when a surgical edit is not safe for
    this file. The caller always re-parses and compares, so returning ``None``
    (or even returning something subtly wrong) cannot corrupt the config.
    """
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    block = _scan_providers_block(lines)

    if block is None:
        # No ``providers:`` line at all (or an inline flow mapping). Appending a
        # whole block at the end of the document only re-parses to the intended
        # doc when the key is genuinely absent; the caller's verification step
        # decides that, and falls back to the dump otherwise.
        if _top_level_key_present(lines, "providers"):
            return None
        prefix = "".join(lines)
        if prefix.strip():
            prefix = prefix.rstrip("\n") + "\n\n"
        return prefix + "providers:\n" + "".join(_dump_entry_lines(entry, "  "))

    entry_lines = _dump_entry_lines(entry, block.child_indent)

    if block.entry_range is not None:
        start, stop = block.entry_range
        # The replacement is a fresh serialisation, so any comment the user left
        # *inside* our entry would be dropped. That is the one thing the surgeon
        # exists to prevent, so carry those comments across, re-indented to sit
        # with the fields they annotate. Comment text is never parsed, so
        # keeping a stale note ("# pinned to the old port") is safe — losing it
        # is not.
        carried = [
            f"{block.child_indent}  # {line.lstrip()[1:].strip()}\n"
            for line in lines[start + 1 : stop]
            if line.lstrip().startswith("#")
        ]
        if carried:
            entry_lines = entry_lines[:1] + carried + entry_lines[1:]
        return "".join(lines[:start] + entry_lines + lines[stop:])

    if block.end == block.key_index + 1:
        # Empty block (``providers:`` with nothing under it): write under the key.
        return "".join(lines[: block.key_index + 1] + entry_lines + lines[block.end :])

    return "".join(lines[: block.end] + entry_lines + lines[block.end :])


def _top_level_key_present(lines: list[str], key: str) -> bool:
    """True when *key* appears as a top-level mapping key anywhere in *lines*."""
    for line in lines:
        if line[:1] in " \t" or not line.strip():
            continue
        match = _KEY_LINE_RE.match(line)
        if match is not None and _key_token_is(match.group("key"), key):
            return True
    return False


def _render_dump(doc: dict[str, Any]) -> str:
    """Full re-serialisation: always correct, but drops comments and reflows."""
    assert yaml is not None
    return yaml.safe_dump(
        doc,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


# ── Writing ──────────────────────────────────────────────────────────────────


def _atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* atomically, preserving an existing file's mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode: int | None = None
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        mode = None

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=_TMP_PREFIX, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            try:
                os.chmod(tmp_path, mode)
            except OSError:  # pragma: no cover - exotic filesystems
                pass
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _backup_once(path: Path, dry_run: bool) -> Path | None:
    """Copy the current file to ``<name>.bak.<stamp>`` if no backup exists yet."""
    if not path.exists():
        return None
    existing = sorted(path.parent.glob(path.name + ".bak.*"))
    if existing:
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = path.parent / f"{path.name}.bak.{stamp}"
    if dry_run:
        return backup
    backup.write_bytes(path.read_bytes())
    return backup


# ── The operation ────────────────────────────────────────────────────────────


def seed_config(
    path: str | os.PathLike[str] | None = None,
    *,
    port: int = DEFAULT_PORT,
    force: bool = False,
    dry_run: bool = False,
) -> SeedResult:
    """Ensure ``providers.opencode-zen-free`` exists in the Hermes config.

    ``path`` may be given explicitly; otherwise :func:`resolve_config_path`
    applies. Returns a :class:`SeedResult`; never raises for expected failures
    (bad YAML, unreadable file, missing PyYAML) — those come back as
    ``STATUS_FAILED`` / ``STATUS_SKIPPED`` with a message.
    """
    target = resolve_config_path(path).resolve()
    block = build_block(port)

    if not HAS_YAML:
        return SeedResult(
            STATUS_SKIPPED,
            "PyYAML is not importable, so the config seed cannot be written. "
            "Run this with the pack interpreter: "
            "<pack>/.venv/bin/python install/seed_config.py",
            target,
        )

    text: str | None
    try:
        text = target.read_text(encoding="utf-8") if target.exists() else None
    except OSError as exc:
        return SeedResult(STATUS_FAILED, f"cannot read {target}: {exc}", target)

    if text is not None and text.strip() == "":
        text = None

    doc: dict[str, Any]
    if text is None:
        doc = {}
    else:
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:  # type: ignore[union-attr]
            detail = str(exc).replace("\n", " ").strip()
            return SeedResult(
                STATUS_FAILED,
                f"{target} is not valid YAML; left untouched ({detail})",
                target,
            )
        if loaded is None:
            doc = {}
        elif isinstance(loaded, dict):
            doc = loaded
        else:
            return SeedResult(
                STATUS_FAILED,
                f"{target} does not contain a YAML mapping at the top level; left untouched",
                target,
            )

    providers = doc.get("providers")
    if providers is None:
        providers = {}
    elif not isinstance(providers, dict):
        return SeedResult(
            STATUS_FAILED,
            f"{target} has a non-mapping 'providers' key; left untouched. "
            "Fix it by hand or pass --force-free config.",
            target,
        )

    entry = providers.get(PROVIDER_SLUG)
    if entry is not None and not isinstance(entry, dict):
        return SeedResult(
            STATUS_FAILED,
            f"{target} has providers.{PROVIDER_SLUG} as a "
            f"{type(entry).__name__}, not a mapping; refusing to overwrite it. "
            "Remove that entry (or fix it) and re-run.",
            target,
        )

    if _entry_is_seeded(entry) and _entry_is_canonical(entry, block) and not force:
        return SeedResult(
            STATUS_UNCHANGED,
            f"already seeded with base_url {block['base_url']}",
            target,
        )

    if not force and _entry_is_seeded(entry):
        # Route present but values drifted (e.g. a stale port). Only --force
        # rewrites managed values, so a user's deliberate override survives.
        current = str(entry.get("base_url") or "")  # type: ignore[union-attr]
        return SeedResult(
            STATUS_UNCHANGED,
            f"already seeded with base_url {current} (differs from "
            f"{block['base_url']}; pass --force to reset)",
            target,
        )

    status = STATUS_UPDATED if _entry_is_seeded(entry) else STATUS_CREATED
    merged_entry: dict[str, Any] = dict(entry) if isinstance(entry, dict) else {}
    if force or status == STATUS_UPDATED:
        merged_entry.update(block)
    else:
        merged_entry = {**block, **merged_entry}

    new_doc = dict(doc)
    new_doc["providers"] = {**providers, PROVIDER_SLUG: merged_entry}

    if dry_run:
        return SeedResult(
            status,
            f"{block['base_url']} (dry run, nothing written)",
            target,
            changed=True,
            dry_run=True,
        )

    # 1. Surgical edit first: it keeps the user's comments and layout. It
    #    handles both the insert case and the replace case, so a drifted entry
    #    rewritten under --force does not cost the user their comments.
    new_text: str | None = _render_surgical(text or "", merged_entry)

    # 2. Verify by re-parsing, else fall back to a full dump.
    def _accepts(candidate: str | None) -> bool:
        if candidate is None:
            return False
        try:
            return yaml.safe_load(candidate) == new_doc  # type: ignore[union-attr]
        except yaml.YAMLError:  # type: ignore[union-attr]
            return False

    if not _accepts(new_text):
        new_text = _render_dump(new_doc)
    if not _accepts(new_text):
        return SeedResult(
            STATUS_FAILED,
            f"could not produce a YAML document equivalent to the merge for {target}; "
            "left untouched",
            target,
        )

    backup: Path | None = None
    try:
        backup = _backup_once(target, dry_run=False)
        _atomic_write_text(target, new_text)  # type: ignore[arg-type]
    except OSError as exc:
        return SeedResult(STATUS_FAILED, f"cannot write {target}: {exc}", target, backup_path=backup)

    return SeedResult(
        status,
        f"{block['base_url']}",
        target,
        changed=True,
        backup_path=backup,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed_config.py",
        description=(
            "Write the providers.opencode-zen-free route into a Hermes config.yaml "
            "so the WebUI model picker admits the free OpenCode Zen group. "
            "Merge-only and idempotent: other keys and other providers are never "
            "touched, and a second run changes nothing."
        ),
        epilog=(
            "Config path: --config, else $HERMES_CONFIG_PATH, else "
            "$HERMES_HOME/config.yaml, else ~/.hermes/config.yaml. "
            "Exit 0 when the seed is in place (created/updated/unchanged), "
            "1 on failure or when PyYAML is missing."
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="config.yaml to seed (overrides the env-var resolution chain)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"sidecar port to write into base_url (default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing or creating directories",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "reset the managed keys (name, base_url, api_key, models_discovered) to "
            "their canonical values; other keys inside the entry and every other "
            "provider are still preserved"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.port < 1 or args.port > 65535:
        print(f"seed_config: --port must be 1..65535, got {args.port}", file=sys.stderr)
        return 1

    result = seed_config(
        args.config,
        port=args.port,
        force=args.force,
        dry_run=args.dry_run,
    )

    stream = sys.stdout if result.ok else sys.stderr
    detail = result.describe()
    print(f"{PROVIDER_SLUG}: {detail}  ({result.path})", file=stream)
    if result.message and result.message not in {detail, ""}:
        print(f"  {result.message}", file=stream)
    if result.backup_path is not None:
        print(f"  backup: {result.backup_path}", file=stream)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
