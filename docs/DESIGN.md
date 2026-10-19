# opencodezen4hermes — Design

Status: v1 (2026-10-20). Owner: defaver04.

## 1. Goal

Make the free OpenCode Zen model roster appear in the Hermes WebUI model-selection
menu and actually serve completions, with no API key, from a Linux arm64
`hermes-webui-agent` pack.

## 2. Why a sidecar and not just a provider profile

The Zen anonymous lane is not a plain OpenAI-compatible endpoint. A request is
admitted only if the body and headers look like the official `opencode` CLI.
Auditing `opencodezen4dsh` (`packages/plugin/src/adapter/{official,fingerprint,ids,messages,routing}.ts`)
and reproducing the lane live shows the gate validates four independent things:

| Gate element | Value | Can a Hermes `ProviderProfile` supply it? |
|---|---|---|
| Wire `User-Agent` | `opencode/<ver> ai-sdk/provider-utils/<ver> runtime/bun/<ver>` | yes, via `default_headers` |
| Disguise headers | `x-opencode-client: cli`, `x-opencode-session`, `x-opencode-request`, `x-opencode-project` | only statically; session ids must be timestamp-valid per request |
| System prompt | the official CLI system prompt verbatim (~9.5 KB) | **no** — Hermes sends its own system prompt |
| Tool set | exactly the 11 official tools (`bash,edit,glob,grep,read,skill,task,todowrite,webfetch,websearch,write`) with `tool_choice:auto` | **no** — Hermes sends its own tool schemas |

The last two cannot be reached from a `ProviderProfile`: `providers/base.py` has no
request-body transform hook (there is no `transform`/`rewrite_request`/
`prepare_payload` field on the dataclass). Therefore the body has to be rewritten
by something that sits on the wire. That is the sidecar.

```
Hermes agent ──openai SDK──▶ 127.0.0.1:8799/v1  ──▶ gate rewrite + headers ──▶ opencode.ai/inference/v1
                              (opencodezen4hermes sidecar)                          (anonymous lane)
```

The sidecar is a single-file Python stdlib server (`sidecar/`), so it needs no Go
toolchain and no extra wheels in the arm64 pack.

## 3. Component map

```
sidecar/
  server.py           ThreadingHTTPServer, /healthz, /v1/models, /v1/chat/completions, /v1/messages
  config.py           env + CLI options, reserved port handling
  gate.py             official system prompt + 11 tools + wire UA + disguise headers
  ids.py              timestamp-valid tU session/request ids (BAS62, descending ses_/ascending msg_)
  messages.py         body rewrite: system prompt replace, tool-set normalise, tool_choice
  toolbridge.py       OpenCode tool-call -> Hermes tool-call rewrite + non-official tunnel
  catalog.py          live GET /v1/models + free filter + models.dev pricing verdict + cache
  upstream.py         httpx/urllib POST with streaming passthrough and error mapping
plugin/
  opencode-zen-free/  Hermes model-provider plugin (profile + plugin.yaml)
  opencodezen4hermes/ Hermes *platform* plugin that seeds WebUI detection + starts the sidecar
packaging/
  build.sh hook, launcher, GitHub Actions
```

## 4. The four integration problems, and their answers

### 4.1 Does a keyless provider reach the WebUI dropdown?

`api/plugin_providers.py` mirrors `providers.list_providers()`, so a plugin-only slug
becomes a candidate. But `api/config.py` only renders a group for a slug that lands
in `detected_providers`, and the detection paths all require *evidence*:
`active_provider`, credential pool, `list_available_providers()` `authenticated`,
a hardcoded env-var table, or a route-bearing `providers:` config entry
(`_has_provider_route` = any of `api`/`base_url`/`api_key`/`key_env`).

A keyless profile is never `authenticated`, so **evidence must be shipped**.
`packaging/` writes a `providers.opencode-zen-free` entry carrying
`base_url` + `api_key: public`, which satisfies `_has_provider_route` and also
makes `get_auth_status()` report `configured: true`, so the picker group renders
even under `providers.only_configured: true`.

### 4.2 The catalog

`hermes_cli/models.py:1532 _profile_live_catalog` falls back to
`profile.fallback_models` when no API key probe is possible. That tuple is fully
under our control, so it carries the seeded free roster. When the sidecar is up,
`profile.fetch_models()` returns the live roster instead, and the free filter is
applied consistently in one place (`sidecar/catalog.py`) so the picker and the
sidecar agree.

Free decision (`catalog.py`), mirroring the upstream plugin:
1. `models.dev` metadata pricing — allowed iff `input == 0 and output == 0`;
   `deprecated` ⇒ denied.
2. Name fallback — id contains `free` (case-insensitive), only while metadata is
   pending or missing.
3. Static blocklist so a stale or mistyped roster can never surface a paid id.

### 4.3 Tool calls come back OpenCode-shaped

The gate forces the 11 official tools, so the model answers with `todowrite`,
`filePath`, and a `bash` call without the `description` Hermes requires. `toolbridge.py`
maps the official names onto Hermes names and repairs argument shapes; Hermes tools
outside the official 11 are carried as extra advertised tools only when the lane has
probed extras-tolerant, and tunnelled otherwise (`toolbridge.py` `advertise` mode).

### 4.4 The lane is IP-gated

Live probes from a datacenter IP return `403 FreeTierError` for every free model
even with a byte-exact official request, while `GET /v1/models` returns 200. The
upstream project ships a whole IP-pool subsystem for exactly this (`docs/ip-pool.md`:
"opencode Zen 匿名通道按出口 IP 限流"). Consequences for this repo:
- The sidecar ships an **honest error surface**: a `403 FreeTierError` becomes an
  actionable message naming the cause (egress IP) rather than a generic failure.
- End-to-end inference is verified on a residential egress, not in CI. CI verifies
  the gate recipe byte-for-byte against the captured fixtures instead.

## 5. Verification strategy

| Claim | How it is proven |
|---|---|
| Gate recipe matches the captured official request | `tests/test_gate.py` compares generated body/headers to `sidecar/gate/fixtures/*.json` |
| Session ids are canonical | `tests/test_ids.py` regex + timestamp window |
| Free filter is correct | `tests/test_catalog.py` against a recorded `/v1/models` + models.dev sample |
| Tool calls are Hermes-shaped | `tests/test_toolbridge.py` |
| Picker group renders | `tests/test_picker.py` drives `api.plugin_providers` + the `detected_providers` rule with the shipped config seed |
| Pack contains the sidecar and it starts | `packaging/sanity.sh` inside the release workflow |
| Live lane works | `scripts/verify_live.py`, run by a human on a residential egress |

## 6. Decisions

- **D1 — sidecar over profile-only.** Forced: the gate needs body rewrite and
  `ProviderProfile` has no body hook. Recorded above.
- **D2 — Python stdlib over Go.** The upstream chose Go to reuse a 4000-LOC
  protocol converter; this port rewrites only the gate envelope and passes SSE
  through untouched, so stdlib `http.server` + `urllib` is enough and avoids
  shipping a second toolchain in an arm64 pack.
- **D3 — ship the config seed rather than patch WebUI.** `_has_provider_route`
  is a supported seam; patching `api/config.py` would fork a 3000-line file and
  break on every upstream bump.
- **D4 — do not vendored-patch vendor/upstream.** The plugin + seed live in the
  repo root and the build stages them into the pack, so `vendor/upstream` stays a
  clean mirror of upstream and `SOURCE=tarball` keeps working.
