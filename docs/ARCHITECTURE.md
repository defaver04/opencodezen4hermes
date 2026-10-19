# Architecture

How a model selection in the Hermes WebUI becomes a completion from the OpenCode
Zen free lane, and why every piece in between has to exist.

Every line reference below was checked against the file it names. Paths under
`vendor/upstream/` are seams in the upstream projects, not files in this
repository — they exist in a build checkout, which is what `build.sh` produces in
`.work/`. To follow a reference by hand, unpack the pinned tarballs named in
`sources.lock.json` (or clone `nesquena/hermes-webui` at `v0.52.458` and
`NousResearch/hermes-agent` at `v2026.9.24`) into `vendor/upstream/`. Every other
path below is this repository.

---

## The three pieces and why one is not enough

```
  Hermes WebUI  ──picker──►  config.yaml providers.opencode-zen-free
       │                              ▲
       │ openai client                │ install/seed_config.py
       ▼                              │
  http://127.0.0.1:8799/v1      plugin/opencode-zen-free/  (ProviderProfile)
       │
  sidecar/   gate shaping + session ids + catalog
       │
       ▼
  https://opencode.ai/inference/v1
```

The upstream extension point is a **plugin provider**: a `ProviderProfile`
subclass that Hermes discovers at startup. That covers base URL, auth and the
model list. It does not cover the request *body*, which is what the Zen lane
actually validates — and that is the whole story of this repository:

1. **The profile alone registers the provider but does not admit it to the
   picker.** Without a route in `config.yaml`, `get_available_models()` filters
   the provider out and the user sees `groups: []` with no error. That is why the
   config seed exists and why it is not optional.
2. **A profile cannot rewrite the request body.** `ProviderProfile` has no
   transform hook, and the lane rejects an unshaped body with `403 FreeTierError`
   no matter what the headers say. That is why the sidecar exists: it is a local
   HTTP endpoint that the profile points at, and that endpoint is free to shape
   what it sends upstream.

So the shape is not incidental complexity. Each piece is the minimum that closes
one of those two gaps, and `docs/DESIGN.md` records the alternatives that were
rejected.

---

## The request path, end to end

### 1. The picker builds its list

`vendor/upstream/hermes-webui/api/config.py:9397` —

```python
def get_available_models(*, prefer_cache: bool = False, force_refresh: bool = False) -> dict:
```

This is what the model menu reads. For each provider it decides admission:

- `vendor/upstream/hermes-webui/api/config.py:9930` — `_has_provider_route = any(...)`
  over the keys `("api", "base_url", "api_key", "key_env")`.
- `vendor/upstream/hermes-webui/api/config.py:9971` —
  `_admit_as_known = _is_known_provider and _already_credentialed`.
- `vendor/upstream/hermes-webui/api/config.py:9972` —
  `if not (_admit_as_known or _has_provider_route or _has_models_only_active_route): continue`
- `vendor/upstream/hermes-webui/api/config.py:9976` —
  `detected_providers.add(_canonical)`

`opencode-zen-free` is not a known provider, so `_admit_as_known` is false. It
becomes true only through `_has_provider_route`, which reads the config entry —
and the seed is what puts `base_url` there. **Remove the seed and the loop
`continue`s; the provider never reaches the picker.** This is the single fact
that shapes the whole project.

The model list itself comes from
`vendor/upstream/hermes-webui/api/config.py:9255` —
`def _read_live_provider_model_ids(provider_id: str) -> list[str]`, which imports
`hermes_cli.models.provider_model_ids`.

### 2. the provider's model ids

`vendor/upstream/hermes-agent/hermes_cli/models.py:1603` —

```python
def provider_model_ids(provider: Optional[str], *, force_refresh: bool = False) -> list[str]:
```

For a plugin profile it reaches
`vendor/upstream/hermes-agent/hermes_cli/models.py:1532` —
`def _profile_live_catalog(normalized: str) -> Optional[list[str]]`, which for a
non-`api_key` profile returns `list(profile.fallback_models)` — the nine ids
hardcoded in `plugin/opencode-zen-free/__init__.py:34` as `FALLBACK_FREE_MODELS`.

Related helpers on the same path:
`hermes_cli/models.py:1565` `probe_profile_catalog`,
`hermes_cli/models.py:1577` `merge_profile_catalog`,
`hermes_cli/models.py:1593` `_drop_delisted_opencode_models`, and
`hermes_cli/models.py:1378` `_api_key_credentials`.

This is why the fallback tuple is a contract and not a convenience: it is what
the picker renders.

### 3. Credentialing the plugin provider

`vendor/upstream/hermes-webui/api/providers.py:2585` —

```python
if not has_key and is_plugin_model_provider(pid):
```

A plugin provider without a key is not treated as unconfigured. The status comes
back as `vendor/upstream/hermes-webui/api/providers.py:2616`
`key_source = "config_yaml"` — the `api_key: public` sentinel from the seed.

`is_plugin_model_provider` is
`vendor/upstream/hermes-webui/api/plugin_providers.py:136`; it works off
`vendor/upstream/hermes-webui/api/plugin_providers.py:83` —
`def plugin_model_provider_ids() -> frozenset[str]`.

Auth status flows through
`vendor/upstream/hermes-agent/hermes_cli/auth.py:2027` —
`def get_auth_status(provider_id: Optional[str] = None) -> Dict[str, Any]`, whose
`_STATUS_BY_AUTH_TYPE` table maps `"api_key"` to `get_api_key_provider_status`
(`hermes_cli/auth.py:2052`). The profile's `auth_type` is `"api_key"` with
`env_vars == ()`, so no environment variable is consulted — the literal
`api_key: public` in the config is the entire credential.

### 4. From profile to sidecar

`plugin/opencode-zen-free/__init__.py`:

- `:29` `SIDECAR_PORT` — 8799, mirroring `sidecar/config.py:17` `DEFAULT_PORT = 8799`.
- `:34` `FALLBACK_FREE_MODELS` — the nine ids in picker order.
- `:47` `class OpenCodeZenFreeProfile(ProviderProfile)`.
- `:50` `fetch_models` — reads `/v1/models` from the sidecar for the live list.
- `:90` `profile = OpenCodeZenFreeProfile(...)` — `base_url` is
  `http://127.0.0.1:8799/v1`; `sidecar/config.py:18` `DEFAULT_HOST = "127.0.0.1"`
  is the matching default.
- `:108` `default_aux_model="deepseek-v4.1-flash-free"`.
- `:112` `register_provider(profile)` — the registration Hermes discovers.

### 5. The sidecar's HTTP surface

`sidecar/server.py:298` `def serve(config: Config) -> int` builds the catalog,
refuses a taken port with exit code 2, warms the catalog for up to 5 s, and then
serves. Routes:

- `sidecar/server.py:171` `def do_GET` — `/healthz` (the only liveness contract),
  `/v1/models`, and the status page.
- `sidecar/server.py:206` `def do_POST` — `/v1/chat/completions` and
  `/v1/messages`.
- `sidecar/server.py:228` `_handle_chat`, `sidecar/server.py:279`
  `_handle_messages` — both resolve the model against the catalog before
  handing off, so the free-roster contract cannot be bypassed by picking a
  different route. Both also trip `sidecar/catalog.py:396` `maybe_refresh`
  first (see "Keeping the roster fresh" below).

Three guards sit in front of the handlers, all in `sidecar/server.py`:

- `:105` `_cross_origin` — a POST carrying a third-party `Origin` is refused
  with 403 `cross_origin_refused`. Hermes and curl send no `Origin` at all, and
  loopback origins are allowed, so only a real web page is stopped. It matters
  because the sidecar has no auth: without this, any site the user visits could
  spend their free-lane quota. Reads are deliberately *not* guarded.
- `:119` `_read_json` — refuses a body larger than `:44` `MAX_BODY_BYTES`
  (32 MiB) with 413 `request_too_large` *without reading it*, and bounds the
  body read itself with `BODY_READ_TIMEOUT` (30 s) so a client cannot pin a
  worker thread by announcing a body it never sends. The timeout is cleared
  afterwards so it cannot clip the long-lived SSE response.
- Every handler tolerates `BrokenPipeError`/`ConnectionResetError`, because a
  closed browser tab is normal, not an error.

Configuration is resolved by `sidecar/config.py:103` `def resolve`, built from
`sidecar/config.py:59` `def from_env` and `sidecar/config.py:81`
`def build_parser` — environment first, then CLI flags on top, so a flag always
wins. `sidecar/config.py:39` `class Config` is the resolved shape, and
`--print-config` prints it including the `base_url` the seed must match.

### 6. Shaping the request

`sidecar/gate.py` holds the measured gate (`docs/GATE.md`):

- `:26` `DEFAULT_LANE_BASE` — `https://opencode.ai/inference/v1`.
- `:31` `WIRE_UA` — the UA the lane accepts.
- `:34` `GATE_REQUIRED_TOOL = "read"`.
- `:98` `def build_headers(session_id, request_id, *, api_key=None)`.
- `:116` `def shape_body(body, *, session_id, request_id)`.
- `:129` `shaped["stream"] = True`.

`stream: true` is forced rather than requested because the lane rejects
non-streaming requests outright. The `read` tool is injected only when the caller
did not already declare one named exactly `read`, and caller tool names are
preserved — which is what lets real Hermes tools (`read_file`, `write_file`)
survive the round trip, as captured in [E2E_PROOF.md](E2E_PROOF.md).

Session identity comes from `sidecar/ids.py`: `:22`
`CANONICAL_SESSION_PATTERN = r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$"` is the lane's
constraint, `:58` `generate_session_id`, `:63` `generate_request_id`, `:72`
`canonical_session_id` (stable per conversation) and `:97` `conversation_signal`
(the deterministic conversation fingerprint both are derived from). The cache is
bounded by `_MAX_CACHED_SESSIONS = 512` in the same module.

### 7. Upstream, and the error path

`sidecar/server.py:244` `_relay_stream` copies SSE chunks through without
buffering or re-encoding. Errors go through `sidecar/server.py:234`
`_relay_upstream_error`, which maps a lane 401/403 to `502`
(`sidecar/server.py:240`: `if status in {401, 403}:` → `HTTPStatus.BAD_GATEWAY`)
while passing a genuine 4xx through unchanged. A rejected upstream call should
never look like a bad local API key.

The message attached to that error comes from `sidecar/upstream.py:56`
`def explain_error(status: int, payload: bytes) -> str`, which keys off the
lane's error types — `FreeTierError` (the egress-IP/quota explanation),
`ModelProtocolUnsupported`, `ModelDeprecated`, `ModelUnavailable` — plus the
region-blocked and 429 cases.

### 8. The catalog

`sidecar/catalog.py` decides the roster:

- `:36` `METADATA_URL = "https://models.dev/api.json"`.
- `:46` `FREE_LANE_BLOCKLIST` — `exo-free`, `jev-1.13-free`,
  `ling-3.0-flash-fin-free`.
- `:57` `STATIC_FREE_MODELS` — the bootstrap list.
- `:71` `LOCAL_MODEL_OVERRIDES` — the one hand-maintained number.
- `:199` `def decide(model, prices, ready)` — metadata verdict first, name
  heuristic only as a fallback.
- `:222` `class Catalog`; `:262` `_fetch_live`; `:338` `refresh`; `:384` `warm`;
  `:395` `decision`; `:409` `models`; `:442` `snapshot`.

`snapshot()` is what `/healthz` reports and `models()` is what `/v1/models`
serves. [MODELS.md](MODELS.md) covers the decision chain in full.

---

## The pack launcher

Inside a pack the sidecar is not installed into site-packages — it sits at
`<pack>/sidecar`, which is why every generated script sets `PYTHONPATH="$ROOT"`
before `-m sidecar`.

`install/pack_install.py` writes `bin/hermes-webui` from a template that performs
the three contract steps (`docs/BLUEPRINT.md` §6) before `exec`ing the WebUI:

1. **ensure the sidecar** — probe `$SIDECAR_URL/healthz` with stdlib `urllib`
   (not `curl`, which may be absent), and if it is silent, start
   `bin/opencodezen4hermes-sidecar` detached under `setsid`, then wait up to
   `OPENCODEZEN4HERMES_LAUNCHER_TICKS` half-second ticks.
2. **ensure the config seed** — run `install/seed_config.py --port "$SIDECAR_PORT"`
   with the pack interpreter.
3. **exec the WebUI** — `"$HERMES_WEBUI_PYTHON" "$ROOT/hermes-webui/bootstrap.py"
   --no-browser --host 0.0.0.0 "$@"`.

Every step logs to `$STATE/launcher.log` and every failure is a `WARN`, never
fatal. The reasoning is in the template's own comment: a WebUI that refuses to
start because a free-tier convenience failed is a worse outcome than a picker
with no free group. The cost of that choice is real — *a running WebUI does not
prove the sidecar is up*, so read `state/launcher.log` rather than assuming.

The launcher also keeps the sidecar port separate from `HERMES_WEBUI_PORT`
(default 8789): conflating them would make step 1 probe the WebUI itself.

`build.sh` calls `install/pack_install.py --pack "$STAGE"` once, in stage 8 of
nine, then asserts the five pack artefacts and that `bin/hermes-webui` mentions
`OPENCODEZEN4HERMES`. The upstream launcher is preserved once as
`bin/hermes-webui.upstream` and never overwritten afterwards, so the original
remains available as evidence.

---

## Timeouts and why they are what they are

| Setting | Default | Why |
|---|---|---|
| `OPENCODEZEN4HERMES_CONNECT_TIMEOUT` | 15 s | Connect only. A dead lane should fail fast. |
| `OPENCODEZEN4HERMES_TIMEOUT` | 300 s | Socket inactivity. Reasoning models are slow; `nemotron-3.5-lightning-free` took 40.5 s for one turn in the recorded run. |
| `OPENCODEZEN4HERMES_CATALOG_TTL` | 300 s | How often the local snapshot is re-derived — see "Keeping the roster fresh". |
| models.dev metadata TTL | 24 h | Pricing moves on the order of weeks; refetching it per request would be rude and slow. |
| Launcher health wait | 20 ticks × 0.5 s = 10 s | The sidecar warms its catalog for up to 5 s before binding, so a shorter window would report a timeout on a healthy first boot. |

---

## Keeping the roster fresh

The catalog is fetched once at boot (`serve()` calls `warm()`) and re-derived
lazily afterwards by `sidecar/catalog.py:396` `def maybe_refresh() -> bool`,
which the read paths call: `GET /v1/models`, `POST /v1/chat/completions`, and
`POST /v1/messages`. `GET /healthz` deliberately does **not** call it, so
liveness never depends on the lane.

The contract is stale-while-revalidate, not refresh-on-read:

- It returns immediately. The caller is answered from the current snapshot; the
  *next* caller sees the result. A request must never wait on `models.dev`.
- It schedules at most one refresh at a time. `_scheduled` and `_refreshing`
  are checked under the catalog lock, so a burst of traffic arriving the moment
  the TTL lapses spawns one background thread, not one per request.
- It fails quietly. `refresh()` never raises; the reason lands in
  `last_error` and surfaces through `/healthz`, and the previous roster keeps
  being served.

This hook is the only reason `OPENCODEZEN4HERMES_CATALOG_TTL` means anything.
`refresh()` is the sole writer of `_refreshed_at`, and before the hook existed
its only caller was `warm()` at boot — so the TTL was read once and then never
consulted again, and a sidecar left running for a week would never notice a
model added upstream. The nine shipped ids are compiled in
(`sidecar/catalog.py` `STATIC_FREE_MODELS`) and the `decision()` static rescue
covers a metadata outage, so the *old* behaviour was not fatal — it just
silently froze the roster. `docs/REVIEW.md` records the finding and the fix.

---

## What is deliberately absent

- **No tool-name translation layer.** The gate requires a tool named `read`, but
  it only checks for its presence — it never rewrites `tool_calls`. Declaring
  Hermes's own tools alongside `read` is sufficient, and the captured
  `tool_calls: ['read_file']` in [E2E_PROOF.md](E2E_PROOF.md) is the evidence.
  An earlier design tunnelled every Hermes tool through the official 11-tool set
  and translated the names back; the probe removed the need for all of it.
- **No non-streaming path.** There is no code for it because the lane never
  accepts one; writing one would be dead code that implies a capability the
  upstream does not have.
- **No proxy pool or egress rotation.** See the honest-limitations section in the
  [README](../README.md#batasan-yang-jujur).
- **No request rewriting inside Hermes.** Shaping lives in the sidecar, so the
  upstream tree stays unpatched and a new upstream release does not conflict with
  a local fork.
- **No vendored upstream source.** `vendor/upstream/` is a build convenience, not
  a shipped artefact, and a fresh clone does not have it.
