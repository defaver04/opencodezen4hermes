#!/usr/bin/env bash
# hermes-webui-agent builder — Linux aarch64 (glibc) pack
#
# Menghasilkan dist/hermes-webui-agent-<ver>-linux-arm64.tar.gz berisi:
#   hermes-webui/   snapshot of nesquena/hermes-webui at $WEBUI_REF
#   hermes-agent/   snapshot of NousResearch/hermes-agent at $AGENT_REF
#                   + plugins/model-providers/opencode-zen-free/ (repo ini)
#   sidecar/        paket sidecar opencode-zen-free (repo ini)
#   install/        seed_config.py — penulis route providers.opencode-zen-free
#   .venv/          venv gabungan (cpython 3.14 PBS) — requirements agent + webui
#   bin/hermes-webui   launcher server WebUI (mewarisi lingkungan dari start-proot.sh)
#                      + di-extend: pastikan sidecar hidup, lalu seed config (BLUEPRINT §6)
#   bin/hermes         launcher CLI agent
#   bin/opencodezen4hermes-sidecar   kontrol sidecar standalone
#   SHA256SUMS
#
# Sembilan tahap. Tahap 1–7 sama persis dengan hermes-webui-agent versi upstream;
# tahap 8 adalah SATU-SATUNYA tambahan repo ini (install/pack_install.py ke $STAGE).
# Urutan dan nama tahap lama tidak boleh diubah — lihat docs/BLUEPRINT.md §4.
#
# Dua mode sumber (SOURCE env):
#   SOURCE=tarball (default) — unduh tarball GitHub sesuai sources.lock.json (reproducible).
#   SOURCE=vendor            — pakai source yang sudah di-commit di vendor/upstream/
#                              (workflow checkout-local: patch mobile/plugin tinggal
#                              diedit langsung di repo ini lalu build).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$REPO_ROOT/lib.sh"

SOURCE="${SOURCE:-tarball}"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/dist}"
WORK_DIR="${WORK_DIR:-$REPO_ROOT/.work}"
WEBUI_REF="$(lock .webui.ref)"
AGENT_REF="$(lock .agent.ref)"
NODE_VERSION="$(lock .node.version)"
UV_VERSION="$(lock .uv.version)"
HOST_ARCH="$(uname -m)"
[ "$HOST_ARCH" = "aarch64" ] || die "builder ini menarget linux aarch64 (host: $HOST_ARCH)"

log "1/9 Kriteria awal"
mkdir -p "$OUT_DIR" "$WORK_DIR"
rm -rf "$WORK_DIR"/{node-v*,src,venv,pyroot,webui,agent,stage,_out,patched,uvcache} 2>/dev/null; rm -f "$WORK_DIR"/agent-*.tar.gz "$WORK_DIR"/webui-*.tar.gz "$WORK_DIR"/python.tar.gz

log "2/9 Unduh toolchain + kedua upstream"
UV_TARBALL="$(lock .uv.'linux-arm64')"
NODE_TARBALL="$(lock .node.'linux-arm64')"
curl --retry 3 -L --fail -o "$WORK_DIR/uv.tar.gz" "$UV_TARBALL"
curl --retry 3 -L --fail -o "$WORK_DIR/node.tar.xz" "$NODE_TARBALL"
if [ "$SOURCE" = "vendor" ]; then
  # Mode checkout-local: salin source dari vendor/upstream (yang di-commit di repo).
  [ -d "$REPO_ROOT/vendor/upstream/hermes-webui" ] || die "vendor/upstream/hermes-webui tidak ada"
  [ -d "$REPO_ROOT/vendor/upstream/hermes-agent" ] || die "vendor/upstream/hermes-agent tidak ada"
  cp -a "$REPO_ROOT/vendor/upstream/hermes-webui" "$WORK_DIR/webui"
  cp -a "$REPO_ROOT/vendor/upstream/hermes-agent" "$WORK_DIR/agent"
  log "SOURCE=vendor — memakai vendor/upstream (webui $WEBUI_REF, agent $AGENT_REF)"
fi

if [ "$SOURCE" != "vendor" ]; then
WEBUI_TAR="$WORK_DIR/webui-$WEBUI_REF.tar.gz"
AGENT_TAR="$WORK_DIR/agent-$AGENT_REF.tar.gz"
WEBUI_URL="$(tarball_of webui "$WEBUI_REF")"
AGENT_URL="$(tarball_of agent "$AGENT_REF")"
log "webui: $WEBUI_URL"
log "agent: $AGENT_URL"
curl --retry 3 -sL --fail -o "$WEBUI_TAR" "$WEBUI_URL" || die "gagal unduh webui $WEBUI_URL"
curl --retry 3 -sL --fail -o "$AGENT_TAR" "$AGENT_URL" || die "gagal unduh agent $AGENT_URL"

# Verifikasi sha256 jika tercatat (bukan 'auto-on-first-build') — locks reproducibility.
# Saat pertama pinning tag: jalankan sekali dengan RECORD_SHA=1 untuk mengisi 'auto-on-first-build'.
AGENT_SHA="$(lock .agent.sha256)"
WEBUI_SHA="$(lock .webui.sha256)"
if [ "$AGENT_SHA" != "auto-on-first-build" ] && [ "$AGENT_SHA" != "null" ]; then
  echo "$AGENT_SHA  $AGENT_TAR" | sha256sum --check --strict - || die "sha256 agent mismatch"
fi
if [ "$WEBUI_SHA" != "auto-on-first-build" ] && [ "$WEBUI_SHA" != "null" ]; then
  echo "$WEBUI_SHA  $WEBUI_TAR" | sha256sum --check --strict - || die "sha256 webui mismatch"
fi
if [ "${RECORD_SHA:-0}" = "1" ]; then
  record_sha() {  # $1 file json, $2 key, $3 sha
    python3 - "$1" "$2" "$3" <<'PYEOF'
import json, sys
path, key, sha = sys.argv[1], sys.argv[2], sys.argv[3]
if sha in ("", "auto-on-first-build", "null"):
    sys.exit(0)
d = json.load(open(path))
d[key]["sha256"] = sha
json.dump(d, open(path, "w"), indent=2)
PYEOF
  }
  record_sha "$REPO_ROOT/sources.lock.json" agent "$(sha256sum "$AGENT_TAR" | cut -d' ' -f1)"
  record_sha "$REPO_ROOT/sources.lock.json" webui "$(sha256sum "$WEBUI_TAR" | cut -d' ' -f1)"
  log "sha256 kedua upstream tercatat di sources.lock.json"
fi
fi  # akhir if SOURCE != vendor (blok unduh tarball)

log "3/9 Ekstrak"
mkdir "$WORK_DIR/src"
tar -C "$WORK_DIR/src" -xzf "$WORK_DIR/uv.tar.gz"
tar -C "$WORK_DIR" -xJf "$WORK_DIR/node.tar.xz"
if [ "$SOURCE" != "vendor" ]; then
  mkdir "$WORK_DIR/webui" "$WORK_DIR/agent"
  tar -C "$WORK_DIR/webui" --strip-components=1 -xzf "$WEBUI_TAR"
  tar -C "$WORK_DIR/agent" --strip-components=1 -xzf "$AGENT_TAR"
fi
export PATH="$WORK_DIR/src/uv-aarch64-unknown-linux-gnu:$WORK_DIR/node-v${NODE_VERSION}-linux-arm64/bin:$PATH"
uv --version || die "uv tidak jalan"
node --version || die "node tidak jalan"

log "4/9 Patch"
apply_patches "$WORK_DIR/webui" webui
apply_patches "$WORK_DIR/agent" agent
PATCHED="$WORK_DIR/patched"
touch "$PATCHED"

log "5/9 Venv gabungan (cpython 3.14 PBS)"
PYTHON_TARBALL="$WORK_DIR/python.tar.gz"
PBS_URL="$(pbs_url cpython 3.14.7 20260901)"
curl --retry 3 -L --fail -o "$PYTHON_TARBALL" "$PBS_URL"
mkdir "$WORK_DIR/pyroot"
tar -C "$WORK_DIR/pyroot" -xzf "$PYTHON_TARBALL"
PYINST="$(find "$WORK_DIR/pyroot" -maxdepth 1 -mindepth 1 -type d | head -1)"
PY="$PYINST/bin/python3"
"$PY" --version
uv venv --python "$PY" "$WORK_DIR/venv" --seed --clear
VPY="$WORK_DIR/venv/bin/python"
# Deps agent: pakai uv.lock upstream agar persis sama dengan CI mereka.
# --python "$VPY" menarget venv kita meski pyproject pinned versi mereka.
(
  cd "$WORK_DIR/agent"
  UV_CACHE_DIR="$WORK_DIR/uvcache" uv export --frozen --no-hashes --no-emit-project --format requirements-txt -o "$WORK_DIR/agent-reqs.txt"
  UV_CACHE_DIR="$WORK_DIR/uvcache" uv pip install --python "$VPY" -r "$WORK_DIR/agent-reqs.txt"
)
# Extras web dashboard (dipaketkan agar dashboard jalan tanpa lazy-install)
UV_CACHE_DIR="$WORK_DIR/uvcache" uv pip install --python "$VPY" \
  "fastapi==0.133.1" "uvicorn==0.41.0" "starlette==1.3.1" "python-multipart==0.0.32" "httpx2==2.7.0"
# mcp SDK utk hermes-webui/mcp_server.py — versinya WAJIB 1.x (mcp_server.py cek
# 'mcp>=1.28,<2'); jangan pakai extra [mcp] agent yang 2.0.
UV_CACHE_DIR="$WORK_DIR/uvcache" uv pip install --python "$VPY" "mcp>=1.28,<2"
# Deps WebUI sendiri
UV_CACHE_DIR="$WORK_DIR/uvcache" uv pip install --python "$VPY" \
  -r "$WORK_DIR/webui/requirements.txt"

log "6/9 Build produk upstream (TUI + web dashboard)"
# CATATAN v2026.9.24: scripts/build/{tui,web}.mjs TIDAK ADA di checkout tag (hapus vs main
# f97608f). Build frontend resmi melalui launcher:
#   TUI  : node ui-tui/scripts/build.mjs (keluarkan dist/entry.js di ui-tui)
#   Web  : npm run build di web/ (vite outDir -> hermes_cli/web_dist, lihat
#          hermes_cli/main_web_build.py:88-90)
(
  cd "$WORK_DIR/agent"
  export CI=1 NPM_CONFIG_FUND=false NPM_CONFIG_AUDIT=false
  # Heap 2GB (host 5.6G, banyak dipakai venv+cache): tsc -b OOM dengan heap default ~256MB
  export NODE_OPTIONS="--max-old-space-size=2048"
  npm install --no-update-notifier --no-audit --no-fund 2>&1 | tail -1 || true
  node ui-tui/scripts/build.mjs
  ( cd web && npm run build )
)

log "7/9 Assemble pack"
STAGE="$WORK_DIR/stage"
mkdir "$STAGE"
# PENTING: mv agent/webui/venv SEBELUM membuat subtree apa pun di dalam
# $STAGE/hermes-agent. mkdir sebelum mv membuat dest sudah ada => mv
# menaruh src DI DALAM dest (stage/hermes-agent/agent/...), bukan merename.
mv "$WORK_DIR/webui" "$STAGE/hermes-webui"
mv "$WORK_DIR/agent" "$STAGE/hermes-agent"
mv "$WORK_DIR/venv" "$STAGE/.venv"
# PORTABILITY venv: symlink python venv menunjuk ABSOLUTE path CI runner.
# Rewrite ke relatif DAN ikutkan runtime python PBS sebagai .pyroot/.
if [ -d "$WORK_DIR/pyroot" ]; then
  mv "$WORK_DIR/pyroot" "$STAGE/.pyroot"
fi
PYREAL="$(find "$STAGE/.pyroot" -maxdepth 3 -path '*/bin/python3' 2>/dev/null | head -1)"
if [ -n "$PYREAL" ]; then
  RELPY="$(python3 - "$STAGE" "$PYREAL" <<'PYEOF'
import os, sys
stage, target = sys.argv[1], sys.argv[2]
print(os.path.relpath(target, os.path.join(stage, ".venv/bin")))
PYEOF
)"
  ln -sfn "$RELPY" "$STAGE/.venv/bin/python"
  ln -sfn python "$STAGE/.venv/bin/python3"
  ln -sfn python "$STAGE/.venv/bin/python3.14"
else
  die "pyroot python3 tidak ketemu (venv tetap punya symlink absolut CI)"
fi
# pyvenv.cfg home juga menunjuk path CI; tulis ulang home ke .pyroot relatif
if [ -f "$STAGE/.venv/pyvenv.cfg" ] && [ -n "$RELPY" ]; then
  PYDIRREL="$(dirname "$RELPY")/.."
  sed -i "s|^home = .*|home = $PYDIRREL|" "$STAGE/.venv/pyvenv.cfg"
fi
# TUI build: entry.js ke hermes_cli/tui_dist; launcher runtime cek
# ui-tui/dist ATAU tui_dist — salin dari path stage (agent sudah berpindah).
mkdir -p "$STAGE/hermes-agent/hermes_cli/tui_dist"
if [ -d "$STAGE/hermes-agent/ui-tui/dist" ]; then
  cp -a "$STAGE/hermes-agent/ui-tui/dist/." "$STAGE/hermes-agent/hermes_cli/tui_dist/"
fi
mkdir "$STAGE/bin"
make_launcher "$STAGE/bin/hermes-webui"
make_agent_launcher "$STAGE/bin/hermes"

log "8/9 Pasang lapisan opencodezen4hermes ke stage"
# SATU-SATUNYA langkah tambahan repo ini. Dijalankan setelah stage 7 selesai
# (pack sudah rakit, bin/hermes-webui upstream sudah ada) dan SEBELUM tar di
# stage 9, supaya sidecar + plugin + seed ikut masuk ke dalam pack.
#
# Yang dilakukan install/pack_install.py (lihat docs/BLUEPRINT.md §4):
#   1. sidecar/                       -> $STAGE/sidecar/
#   2. plugin/opencode-zen-free/      -> $STAGE/hermes-agent/plugins/model-providers/
#   3. install/seed_config.py         -> $STAGE/install/seed_config.py
#   4. bin/hermes-webui               -> launcher di-extend (BLUEPRINT §6):
#                                        pastikan sidecar hidup, seed config, lalu exec.
#                                        Aslinya disimpan sebagai bin/hermes-webui.upstream.
#   5. bin/opencodezen4hermes-sidecar -> kontrol sidecar standalone.
# Idempoten: menjalankannya dua kali tidak menggandakan apa pun.
PACK_INSTALL="$REPO_ROOT/install/pack_install.py"
[ -f "$PACK_INSTALL" ] || die "install/pack_install.py tidak ada — repo tidak lengkap"
python3 "$PACK_INSTALL" --pack "$STAGE"
# Post-condition: kalau keempat artefak ini tidak mendarat, pack cacat dan
# sebaiknya gagal di sini (build), bukan nanti di sanity check CI.
[ -f "$STAGE/sidecar/server.py" ] || die "stage sidecar/server.py hilang"
[ -f "$STAGE/sidecar/gate/official_tools.json" ] || die "stage sidecar/gate/official_tools.json hilang"
[ -f "$STAGE/install/seed_config.py" ] || die "stage install/seed_config.py hilang"
[ -f "$STAGE/hermes-agent/plugins/model-providers/opencode-zen-free/__init__.py" ] || die "stage plugin opencode-zen-free hilang"
[ -f "$STAGE/bin/opencodezen4hermes-sidecar" ] || die "stage bin/opencodezen4hermes-sidecar hilang"
if ! grep -q "OPENCODEZEN4HERMES" "$STAGE/bin/hermes-webui"; then
  die "bin/hermes-webui belum di-extend (launcher masih versi upstream)"
fi
log "lapisan opencodezen4hermes terpasang: $(du -sm "$STAGE/sidecar" | cut -f1) MB sidecar"

log "9/9 Pasang produk TUI+web ke agent, tar.gz, checksum"
# node_modules hanya dibutuhkan saat build; tidak dibawa ke pack.
rm -rf "$STAGE/hermes-agent/web/node_modules" "$STAGE/hermes-agent/ui-tui/node_modules" \
       "$STAGE/hermes-agent/node_modules" "$STAGE/hermes-agent/apps"
# Pengaman: tar tidak boleh memasukkan node_modules (jaga agar pack tetap ~120MB).
[ ! -d "$STAGE/hermes-agent/web/node_modules" ] || die "web/node_modules masih ada di stage"
[ ! -d "$STAGE/hermes-agent/ui-tui/node_modules" ] || die "ui-tui/node_modules masih ada di stage"
# File inti wajib ada di stage SEBELUM tar.
if [ ! -f "$STAGE/hermes-agent/run_agent.py" ]; then
  log "FATAL-debug: isi \$STAGE/hermes-agent (20):"
  ls -a "$STAGE/hermes-agent" | head -20 || true
  log "FATAL-debug: lokasi run_agent.py di \$STAGE:"
  find "$STAGE" -maxdepth 4 -name 'run_agent.py' 2>/dev/null || true
  log "FATAL-debug: isi \$WORK_DIR (10):"
  ls -a "$WORK_DIR" | head -10 || true
  die "stage run_agent.py hilang"
fi
[ -f "$STAGE/hermes-webui/server.py" ] || die "stage server.py hilang"
[ -f "$STAGE/.venv/bin/python" ] || die "stage venv python hilang"
log "ukuran stage: $(du -sm "$STAGE" | cut -f1) MB"
log "per-dir stage:"
du -sm "$STAGE"/* "$STAGE"/.[!.]* 2>/dev/null || true
VER_ID="$(echo "${WEBUI_REF}-${AGENT_REF}" | tr -c 'a-zA-Z0-9._-' '-')"
TARBALL_NAME="hermes-webui-agent-${VER_ID}-linux-arm64.tar.gz"
# --exclude node_modules sebagai lapis kedua (rm -rf di atas + guard).
tar -czf "$OUT_DIR/$TARBALL_NAME" --exclude='node_modules' -C "$STAGE" .
sha256sum "$OUT_DIR/$TARBALL_NAME" > "$OUT_DIR/SHA256SUMS"
log "Selesai! Artefak di \$OUT_DIR:"
ls -lh "$OUT_DIR"
