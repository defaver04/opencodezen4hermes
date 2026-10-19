# Install

Three install paths, in the order most people need them. Every command below was
run against this repository; the "proves it worked" line is the real output, not
a plausible one.

If something does not work, read [TROUBLESHOOTING.md](TROUBLESHOOTING.md) before
filing anything — the first two entries there cover most failures.

---

## Path 1 — install into a pack you already have

Use this when you have a `hermes-webui-agent-*-linux-arm64.tar.gz` and want the
free roster inside it. The pack is the upstream artefact; this repository bolts
onto it.

```bash
# 1. unpack the upstream pack
mkdir -p ~/hermes-pack
tar -xzf hermes-webui-agent-*-linux-arm64.tar.gz -C ~/hermes-pack
cd ~/hermes-pack
```

```bash
# 2. lay down the sidecar, the plugin, the config seed and the extended launcher
python3 install/pack_install.py --pack .
```

Run it from the repository checkout, not from the pack — `install/pack_install.py`
is a build/install-time tool that copies files out of this repository. It refuses
anything that is not a pack:

```
pack_install: refusing to install into /tmp/notapack
  - missing /tmp/notapack/hermes-webui/server.py (is this really a pack root?)
  - missing /tmp/notapack/.venv/bin/python (pack venv not present?)
```

A successful install against a pack whose launcher does not yet carry the layer
prints this:

```
installing opencodezen4hermes into /tmp/fakepack
source: /root/hermess-webui-agent   sidecar port: 8799
  [created] <pack>/sidecar
  [created] <pack>/hermes-agent/plugins/model-providers/opencode-zen-free
  [created] <pack>/install/seed_config.py
  [created] <pack>/bin/opencodezen4hermes-sidecar
  [created] <pack>/bin/hermes-webui.upstream
  [updated] <pack>/bin/hermes-webui
done. Start the WebUI with /tmp/fakepack/bin/hermes-webui — the sidecar is started for you.
```

The `<pack>` labels are literal — the tool prints them verbatim rather than
expanding an absolute path — and only the `done.` line names the real directory.
A `[created] <pack>/bin/hermes-webui.upstream` line means the upstream launcher
was preserved as evidence before it was replaced. If the launcher already carries
the layer, the backup step is skipped and the note explains why:
`(existing launcher is already extended — no upstream to back up)`.

**Proves it worked:** exit code 0 and the `done. Start the WebUI with …` line.
Re-running is safe and idempotent; the second run reports `[unchanged]` for the
files that already match and keeps the existing `bin/hermes-webui.upstream`:

```
  [unchanged] <pack>/bin/opencodezen4hermes-sidecar
  [unchanged (upstream backup kept)] <pack>/bin/hermes-webui
```

Useful flags: `--dry-run` reports what would change and writes nothing (not even
a backup), `--port N` bakes a different sidecar port into both launchers and the
seed, `--repo-root DIR` copies from a checkout other than this one.

```bash
# 3. start it — the launcher brings the sidecar up itself
./bin/hermes-webui
```

Note `install/pack_install.py` is also exactly what `build.sh` stage 8 invokes,
so a pack built from source needs no separate install step.

---

## Path 2 — build the pack from source

Use this on an **aarch64** host when you want the whole tarball. It downloads a
toolchain and both upstream sources and takes a while — budget at least a few
hundred MB of disk under `.work/` and a couple of GB free overall:

| Download | Size |
|---|---|
| `uv` 0.12.3 (aarch64) | ~19 MB |
| Node 26.7.0 (linux-arm64) | ~31 MB |
| CPython 3.14.7 (python-build-standalone) | ~91 MB |
| `hermes-webui` + `hermes-agent` source tarballs | two archives, size varies by ref |

The finished pack is around 120 MB — `build.sh:236` guards the tar against
pulling in `node_modules` precisely to keep it there. `.work/` is scratch and is
not needed afterwards.

```bash
git clone https://github.com/defaver04/opencodezen4hermes
cd opencodezen4hermes
bash build.sh
```

Two source modes, selected with the `SOURCE` environment variable:

```bash
SOURCE=tarball bash build.sh    # default: download upstream tarballs at the refs pinned in sources.lock.json
SOURCE=vendor  bash build.sh    # reuse an upstream tree at vendor/upstream/ if your checkout has one
```

The default is the one that works from a fresh clone: this repository does **not**
ship `vendor/upstream/`, so `SOURCE=vendor` is only for a checkout where you have
put one there yourself. If it is missing, the builder stops with:

```
[builder FATAL] vendor/upstream/hermes-webui tidak ada
```

(that line is printed in red by `lib.sh:die`, which exits 1). It stops *after*
stage 2 has already downloaded `uv` and Node, so the failure is not free — check
for `vendor/upstream/hermes-webui` before starting a `SOURCE=vendor` build.

`SOURCE` selects where the *upstream sources* come from, not whether there is a
download: the toolchain is fetched from the pinned `sources.lock.json` URLs in
both modes.

Stage 8 of nine is the only stage this repository adds. It runs:

```
python3 "$REPO_ROOT/install/pack_install.py" --pack "$STAGE"
```

and then asserts the five pack artefacts exist and that `bin/hermes-webui`
mentions `OPENCODEZEN4HERMES`, so a pack that silently lost the layer fails at
build time rather than at first run.

**Proves it worked:** stage 9 ends with `Selesai! Artefak di /root/hermess-webui-agent/dist:`
followed by an `ls -lh` of that directory, and `dist/` holds exactly two files —
the tarball and `SHA256SUMS`:

```
hermes-webui-agent-v0.52.458-v2026.9.24-linux-arm64.tar.gz
SHA256SUMS
```

The name is built at `build.sh:257` as
`hermes-webui-agent-${VER_ID}-linux-arm64.tar.gz`. Upstream computed `VER_ID`
with `echo … | tr -c 'a-zA-Z0-9._-' '-'`, and because `tr -c` also maps
`echo`'s trailing newline to `-`, that name carried a double dash
(`…v2026.9.24--linux-arm64…`). This repo uses `printf` instead, so the name is
clean. The first release here (`pack-20261019-2141`) still has the double dash
because it was built before the fix; every later build does not.

Either way, prefer the glob `hermes-webui-agent-*-linux-arm64.tar.gz` over
retyping the literal name — that is what the install and upgrade snippets in
[../README.md](../README.md) do, and it survives this kind of change.

To verify the download, put both files in one folder and run:

```
sha256sum -c --strict SHA256SUMS
```

`--strict` matters: without it `sha256sum` prints a warning and still exits 0
when a listed file is missing, so a broken `SHA256SUMS` would pass unnoticed.
Use `--strict` and require the `OK` line.

That advice exists because the file was broken. Upstream `build.sh:220` ran
`sha256sum "$OUT_DIR/$TARBALL_NAME" > "$OUT_DIR/SHA256SUMS"`, which records the
builder's own path:

```
b3ca0513…  dist/hermes-webui-agent-…-linux-arm64.tar.gz
```

In a download folder there is no `dist/`, so the check fails — quietly, for the
reason above. Releases built before `build.sh:266` was fixed carry that form;
this repo now writes a bare filename by `cd`-ing into `OUT_DIR` first. The
published `pack-local-20261019-2157-namecheck` is a pre-fix artefact: its
`SHA256SUMS` still has the `dist/` prefix, so either strip the prefix with
`sed 's|dist/||' SHA256SUMS` or compare the hash by eye.

The builder refuses to run on any host that is not `aarch64`, printing, in red:

```
[builder FATAL] builder ini menarget linux aarch64 (host: x86_64)
```

and exiting 1. It is the first thing that fires on an x86 laptop. Cross-building
is not supported; use an arm64 machine or an arm64 container.

Verify the unpacked result with the doctor:

```bash
python3 scripts/doctor.py --pack /path/to/unpacked-pack
```

---

## Path 3 — development checkout

Use this when you are changing the sidecar and want it running without a pack.
There is no install step: the sidecar is a plain package run from the repository
root.

```bash
cd /root/hermess-webui-agent
python3 -m sidecar --verbose
```

```
opencodezen4hermes sidecar on http://127.0.0.1:8799/v1 -> https://opencode.ai/inference/v1
```

**Proves it worked:** that log line, and a clean `/healthz` from another shell:

```bash
curl -s http://127.0.0.1:8799/healthz
```

```json
{"ok": true, "catalog": {"status": "stale", "total": 85, "exposed": 9, "prices_ready": true, "age_seconds": 1549.4, "last_error": null}}
```

`status` is `stale` while the twenty-four-hour metadata cache is being refreshed
in the background; `exposed: 9` is what matters, and `total: 85` is the whole Zen
catalog the roster was derived from.

Inspect the resolved configuration instead of starting anything:

```bash
python3 -m sidecar --print-config
```

That prints JSON including `base_url`, which is the value the plugin profile and
the config seed must agree on.

Flags for this path: `--host`, `--port`, `--lane-base`, `--catalog-ttl`,
`--include-paid`, `--verbose`, `--print-config`. Environment variables override
the defaults and CLI flags override the environment; see the table in the
[README](../README.md#environment).

### Seeding a Hermes config by hand

The launcher does this for you in a pack. In a checkout you run it yourself, and
it needs PyYAML — which in a pack means the bundled interpreter
(`<pack>/.venv/bin/python`), or any virtualenv that has it:

```bash
<pack>/.venv/bin/python install/seed_config.py --config ~/.hermes/config.yaml
```

```
opencode-zen-free: created  (/root/.hermes/config.yaml)
  http://127.0.0.1:8799/v1
```

A second run is a no-op and says so:

```
opencode-zen-free: unchanged  (/root/.hermes/config.yaml)
  already seeded with base_url http://127.0.0.1:8799/v1
```

Where it writes, in order: `--config PATH`, then `$HERMES_CONFIG_PATH`, then
`$HERMES_HOME/config.yaml`, then `~/.hermes/config.yaml`.

**On a config you already have.** The merge is additive-only. Given:

```yaml
# User comment up here
model:
  provider: openai
  name: gpt-4o
providers:
  openai:
    api_key: sk-real
```

the seed appends its block under the existing `providers:` key and touches
nothing else — the comment, the `model:` block and the `openai` entry all
survive verbatim:

```yaml
# User comment up here
model:
  provider: openai
  name: gpt-4o
providers:
  openai:
    api_key: sk-real
  opencode-zen-free:
    name: OpenCode Zen (free)
    base_url: http://127.0.0.1:8799/v1
    api_key: public
    models_discovered: false
```

The first modification of an existing file leaves a `.bak.<timestamp>` copy
beside it and says so:

```
opencode-zen-free: created  (/tmp/seedv/b.yaml)
  http://127.0.0.1:8799/v1
  backup: /tmp/seedv/b.yaml.bak.20261019T203958Z
```

**When the port has moved.** If an entry exists with a different `base_url`, the
seed reports the drift and changes nothing — exit code 0, no backup, file
untouched:

```
opencode-zen-free: unchanged  (/tmp/seedv/b.yaml)
  already seeded with base_url http://127.0.0.1:8799/v1 (differs from http://127.0.0.1:1111/v1; pass --force to reset)
```

Pass `--force` to reset the four managed keys (`name`, `base_url`, `api_key`,
`models_discovered`) to their canonical values. Other keys inside the entry and
every other provider are still preserved — including comments, because the
update goes through the same surgical text edit as a fresh insert:

```
opencode-zen-free: updated  (/tmp/seedv/b.yaml)
  http://127.0.0.1:8799/v1
```

**Under an interpreter without PyYAML** it does not guess — it skips, on stderr,
and tells you which interpreter to use. Exit code is 0 here, so read the line:

```
opencode-zen-free: skipped  (/tmp/seedv/d.yaml)
  PyYAML is not importable, so the config seed cannot be written. Run this with the pack interpreter: <pack>/.venv/bin/python install/seed_config.py
```

Other flags: `--port N` (must match the sidecar), `--dry-run` (reports
`would create` / `would update` and writes no file and no backup), `--force`
(described above).

---

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

Standard library only; no network, no pytest, no third-party package. Some tests
skip rather than fail when a real Hermes tree is not reachable — the skip reason
names the variable to set.
