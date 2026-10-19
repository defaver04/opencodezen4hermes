#!/usr/bin/env bash
# helper functions untuk build.sh
set -euo pipefail

log() { echo -e "\033[1;34m[builder]\033[0m $*"; }
die() { echo -e "\033[1;31m[builder FATAL]\033[0m $*" >&2; exit 1; }

# baca path JSON dari sources.lock.json tanpa dependensi jq: pakai python3
# Pemakaian: lock ".webui.ref"  |  lock ".uv.'linux-arm64'"
lock() {
  REQUIRE_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}" \
  python3 -c '
import json, os, sys
path = sys.argv[1]
d = json.load(open(os.path.join(os.environ["REQUIRE_ROOT"], "sources.lock.json")))
cur = d
part = ""
for ch in path:
    if ch == ".":
        if part:
            cur = cur[part]
            part = ""
    elif ch == "'"'"'":
        continue
    else:
        part += ch
if part:
    cur = cur[part]
print(cur if not isinstance(cur, (dict, list)) else json.dumps(cur))
' "$1"
}

# tarball URL fallback: *latest tarball API of GitHub*
tarball_of() {
  local which="$1" ref="$2"
  local repo
  repo=$(lock ".$which.repo")
  if [ "$ref" = "main" ] || [ "$ref" = "master" ]; then
    echo "https://github.com/$repo/archive/heads/$(head_branch "$which").tar.gz"
  else
    echo "https://github.com/$repo/archive/$ref.tar.gz"
  fi
}

head_branch() { [ "$1" = "webui" ] && echo master || echo main; }

apply_patches() {
  local dir="$1" which="$2"
  local pdir="$REPO_ROOT/patches/$which"
  [ -d "$pdir" ] || return 0
  shopt -s nullglob
  local pfiles=("$pdir"/*.patch)
  shopt -u nullglob
  [ ${#pfiles[@]} -eq 0 ] && return 0
  for p in "${pfiles[@]}"; do
    log "  patch: $(basename "$p") -> $which"
    patch -d "$dir" -p1 --forward < "$p" || die "patch gagal: $p"
  done
}

pbs_url() {
  # $1=impl $2=version $3=release tag → cpython-<ver>+<tag>-aarch64-…
  local impl="$1" ver="$2" tag="$3"
  echo "https://github.com/astral-sh/python-build-standalone/releases/download/${tag}/${impl}-${ver}%2B${tag}-aarch64-unknown-linux-gnu-install_only.tar.gz"
}

make_launcher() {
  local path="$1"
  cat > "$path" <<'LAUNCH'
#!/usr/bin/env bash
# hermes-webui launcher (relatif terhadap pack root)
set -euo pipefail
SELF="$(realpath "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"
export HERMES_WEBUI_AGENT_DIR="$ROOT/hermes-agent"
export HERMES_WEBUI_PYTHON="$ROOT/.venv/bin/python"
export HERMES_WEBUI_PORT="${HERMES_WEBUI_PORT:-8789}"
mkdir -p "${HERMES_WEBUI_STATE_DIR:-$ROOT/state}"
exec "$HERMES_WEBUI_PYTHON" "$ROOT/hermes-webui/bootstrap.py" --no-browser --host 0.0.0.0 "$@"
LAUNCH
  chmod +x "$path"
}

make_agent_launcher() {
  local path="$1"
  cat > "$path" <<'LAUNCH'
#!/usr/bin/env bash
# hermes CLI launcher (agent checkout via shared venv)
set -euo pipefail
SELF="$(realpath "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"
export PYTHONPATH="$ROOT/hermes-agent"
# NOTE v2026.9.24: entry point resmi = hermes_cli.main:main (pyproject [project.scripts])
exec "$ROOT/.venv/bin/python" -P -c 'import sys
from hermes_cli.main import main
sys.exit(main())' "$@"
LAUNCH
  chmod +x "$path"
}
