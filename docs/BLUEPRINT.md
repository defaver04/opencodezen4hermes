# BLUEPRINT — binding contract for all contributors

This file is the single source of truth for names, paths, and interfaces in this
repository. Every other document and module must agree with it. If a change needs
a contract change, change this file **first**, in the same commit.

Status: v1, 2026-10-20.

## 1. What this repository delivers

`opencodezen4hermes` bundles the free **OpenCode Zen** model roster into a
`hermes-webui-agent` Linux arm64 pack, so that the Hermes WebUI model-selection
menu lists the free models and they actually serve completions, with no API key.

Three cooperating pieces:

| Piece | Path | Role |
|---|---|---|
| Sidecar | `sidecar/` | Local OpenAI-compatible proxy that shapes every request to satisfy the Zen anonymous-lane gate |
| Provider plugin | `plugin/opencode-zen-free/` | Registers a Hermes `ProviderProfile` whose `base_url` is the sidecar |
| Config seed | `install/` | Writes the `providers.opencode-zen-free` route into `$HERMES_HOME/config.yaml` so the picker admits the slug |

All three are required. Verified 2026-10-20: bundling the plugin **without** the
config seed yields an empty model group (`groups: []`), because
`api/config.py` only admits a plugin provider into `detected_providers` when it is
already credentialed or when its config entry carries a route
(`_has_provider_route`). The seed supplies that route.

## 2. Fixed identifiers

Never vary these; they appear in code, docs, CI, and user-facing messages.

| Thing | Value |
|---|---|
| Repository | `defaver04/opencodezen4hermes` |
| Python package | `sidecar` |
| Distribution name (packaging) | `opencodezen4hermes` |
| Version | single source: `sidecar/__init__.py` → `__version__` |
| Provider slug | `opencode-zen-free` |
| Provider display name | `OpenCode Zen (free)` |
| Sidecar default host | `127.0.0.1` |
| Sidecar default port | `8799` |
| Sidecar base URL | `http://127.0.0.1:8799/v1` |
| Env var prefix | `OPENCODEZEN4HERMES_` |
| Cache dir env | `OPENCODEZEN4HERMES_CACHE_DIR` |
| Upstream lane base | `https://opencode.ai/inference/v1` |
| Gate wire UA | `opencode/1.19.3 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14` |
| Default aux model | `deepseek-v4.1-flash-free` |

## 3. The free roster

Nine models, in this order everywhere (it is the picker order):

```
beam-free  big-pickle  deepseek-v4.1-flash-free  glm-5.3-flash-free
longcat-2.5-preview-free  mimo-v2.6-flash-free  minimax-m3.1-flash-free
nemotron-3.5-lightning-free  nemotron-3.5-super-vl-free
```

`sidecar/catalog.py` is authoritative: it derives this list live from
`https://opencode.ai/inference/v1/models` intersected with `models.dev` pricing,
minus `FREE_LANE_BLOCKLIST`. `plugin/opencode-zen-free/__init__.py`
`FALLBACK_FREE_MODELS` mirrors it for the case where the sidecar has not answered
yet. A test asserts the two agree.

## 4. Pack layout (what the build produces)

Additions to the upstream `hermes-webui-agent` arm64 pack:

```
<pack>/
├── hermes-webui/                                  upstream, untouched
├── hermes-agent/                                  upstream, plus:
│   └── plugins/model-providers/
│       └── opencode-zen-free/{__init__.py,plugin.yaml}
├── sidecar/                                       this repo's sidecar package
│   └── gate/official_tools.json
├── .venv/                                         upstream venv (also runs the sidecar)
├── bin/
│   ├── hermes-webui                               upstream launcher, extended (§6)
│   ├── hermes                                     upstream
│   └── opencodezen4hermes-sidecar                 standalone sidecar control (§5)
└── state/                                         runtime state, incl. sidecar.log
```

`build.sh` gains exactly one new stage step; it must not reorder or rename the
existing eight stages.

## 5. Sidecar control surface

Two ways to run it, both supported:

```
python3 -m sidecar                     # from the repo root (development)
<pack>/bin/opencodezen4hermes-sidecar  # from an installed pack
```

`python3 -m sidecar` requires `sidecar/__main__.py` delegating to
`sidecar.server.main`. Exit codes: `0` normal/`--print-config`, `2` port taken.

HTTP surface (see `sidecar/server.py`):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | `{"ok": true, "catalog": {...}}` — the liveness contract |
| `GET` | `/v1/models` | OpenAI list shape; 9 free models |
| `POST` | `/v1/chat/completions` | gate-shaped proxy, SSE passthrough |
| `POST` | `/v1/messages` | Anthropic shape, for the `union-alpha` lane |
| `GET` | `/`, `/v1` | plaintext status page |

`/healthz` is the **only** liveness probe anything in this repo may use. It must
never depend on the upstream lane being reachable.

## 6. Launcher behaviour (the auto-start contract)

`bin/hermes-webui` performs three steps before `exec`ing the WebUI, in order:

1. **Ensure the sidecar** — probe `http://127.0.0.1:$PORT/healthz`; if it does not
   answer, start `bin/opencodezen4hermes-sidecar` detached, wait for `/healthz`
   with a bounded timeout, then continue regardless (a dead sidecar must not stop
   the WebUI from booting).
2. **Ensure the config seed** — call `install/seed_config.py` (copied into the
   pack as `install/seed_config.py`) idempotently; it must never clobber an
   existing `providers.opencode-zen-free` entry nor discard unrelated keys.
3. **exec** the WebUI exactly as upstream does.

Every step is best-effort and logged to `$STATE/launcher.log`. Step 1 and 2
failures are warnings, never fatal: a user whose WebUI refuses to start because a
free-tier convenience failed is a worse outcome than a picker with no free group.

## 7. Config seed — exact shape

```yaml
providers:
  opencode-zen-free:
    name: OpenCode Zen (free)
    base_url: http://127.0.0.1:8799/v1
    api_key: public
    models_discovered: false
```

`api_key: public` is a sentinel, not a secret — the lane is anonymous and ignores
it, but `api/config.py` needs a non-empty route field for `_has_provider_route`,
and `api/providers.py` reports it as `key_source: config_yaml`.

`models_discovered: false` is deliberate: it pins the roster so a stale/partial
live probe cannot empty the picker (see `_provider_models_are_discovered_catalog`).

Seeding must be **merge-only**: read existing YAML if present, add the block only
when the key is absent or lacks `base_url`. Never rewrite, reorder, or drop other
top-level keys. Write atomically (temp file + `os.replace`) and keep a
`.bak.<timestamp>` copy on first modification.

## 8. Testing contract

Tests use the **standard library `unittest` only**. No pytest, no third-party
imports at test time — the pack ships no test runner, and reproducing the failure
on a bare Python must stay possible.

```
python3 -m unittest discover -s tests -v
```

Network-dependent checks are separate, opt-in scripts under `scripts/`
(`verify_live.py`, `doctor.py`), never part of `unittest discover`.

Test files and their owners (do not create a second file for the same concern):

| File | Covers |
|---|---|
| `tests/test_ids.py` | `sidecar/ids.py` shapes, Base62, session regex |
| `tests/test_gate.py` | `sidecar/gate.py` shaping, header set, tool injection |
| `tests/test_catalog.py` | `sidecar/catalog.py` decisions, blocklist, cache |
| `tests/test_config_seed.py` | `install/seed_config.py` merge/idempotence/no-clobber |
| `tests/test_pack_install.py` | `install/pack_install.py` against a synthetic stage |
| `tests/test_plugin_profile.py` | plugin profile fields, fallback roster parity |

## 9. Non-goals

- No paid models in the default roster (`--include-paid` exists for debugging).
- No proxy-pool / egress rotation. Upstream rotates IPs to evade per-IP limits;
  this port deliberately does not, and documents the resulting behaviour instead.
- No vendored upstream source. `sources.lock.json` pins the two upstream tarballs;
  `SOURCE=vendor` reuses `vendor/upstream/` when present.
- No request-body rewriting inside Hermes itself. The sidecar owns the wire.
