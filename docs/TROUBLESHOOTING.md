# Troubleshooting

Symptom → cause → fix. Start with the doctor, which covers most of these
automatically:

```bash
python3 scripts/doctor.py
python3 scripts/doctor.py --pack ~/hermes-pack
```

It prints one `PASS`/`WARN`/`FAIL`/`SKIP` line per check with a one-line fix hint,
and exits 1 only when something actually `FAIL`ed.

A healthy run on this machine looks like:

```
PASS python 3.12.3
PASS sidecar /healthz ok: true (catalog 9 free of 85 live, status=stale)
PASS sidecar /v1/models 9 free model(s), all nine contract ids present
WARN plugin profile ... [stubbed providers.base: fields checked, Hermes itself not exercised]
WARN config seed no config at /root/.hermes/config.yaml
SKIP pack layout
summary: 3 pass, 2 warn, 0 fail, 1 skip -> OK
```

The two `WARN`s are normal in a bare checkout: the seed `WARN` disappears once a
Hermes config exists, and the pack `SKIP` disappears with `--pack DIR`.

---

## The model picker shows no "OpenCode Zen (free)" group

**Cause.** The config seed is missing. This is the single most likely failure,
and it is by design rather than an accident: `api/config.py` only admits a plugin
provider into `detected_providers` when it is credentialed or when its config
entry carries a route. The plugin alone registers the profile, but the picker
filters it out and the result is `groups: []` — no error, no free group, nothing
to click. The seed is what supplies the route.

**Check.**

```bash
python3 scripts/doctor.py
```

Look for the `config seed` line, or check by hand:

```bash
grep -A 4 'opencode-zen-free' "${HERMES_CONFIG_PATH:-${HERMES_HOME:-$HOME/.hermes}/config.yaml}"
```

You want the block from `docs/BLUEPRINT.md` §7:

```yaml
providers:
  opencode-zen-free:
    name: OpenCode Zen (free)
    base_url: http://127.0.0.1:8799/v1
    api_key: public
    models_discovered: false
```

**Fix.** Run the seed with an interpreter that has PyYAML — in a pack that is the
bundled venv:

```bash
<pack>/.venv/bin/python install/seed_config.py
```

Then restart the WebUI. The seed is idempotent and additive: an existing entry is
reported `unchanged`, and other top-level keys in your config are never rewritten
or reordered.

If the seed reports `skipped` and mentions PyYAML, you ran it with an interpreter
that cannot write YAML. The message names the command to use instead.

`base_url` must point at the sidecar's port. If you changed one, change the other
— or re-run the seed and let it follow the port you pass with `--port`.

---

## You moved the sidecar port and the seed will not follow

**Cause.** Deliberate. The seed is a merge tool, not an overwriter: if a
`providers.opencode-zen-free` entry already exists with a different `base_url`,
it reports the drift and changes nothing. Exit code is 0 and the file is left
exactly as it was, so nothing is silently rewritten underneath you.

**Check.**

```
opencode-zen-free: unchanged  (/tmp/seedv/b.yaml)
  already seeded with base_url http://127.0.0.1:8799/v1 (differs from http://127.0.0.1:1111/v1; pass --force to reset)
```

The message names both the current value and the one you asked for, so there is
no guessing about which side is stale.

**Fix.** Re-run with `--force`, which resets the four managed keys (`name`,
`base_url`, `api_key`, `models_discovered`):

```bash
<pack>/.venv/bin/python install/seed_config.py --port 8801 --force
```

```
opencode-zen-free: updated  (<path>)
  http://127.0.0.1:8801/v1
```

`--force` is surgical, not destructive: other keys inside the entry, every other
provider, and your comments all survive. The same is true when the sidecar port
is set through `OPENCODEZEN4HERMES_PORT` rather than `--port` — but note the
launcher passes `--port` explicitly, so in a pack the two must agree.

---

## `403 FreeTierError`

**Cause.** The lane's egress-IP quota. It is not a bug in this repository, and
there is usually nothing to fix locally.

This is the failure the project is most often asked about, so the evidence is
worth stating precisely: from a datacenter IP, a request that is byte-for-byte
identical to the official CLI's own request returns `403 FreeTierError` for every
free model, while `GET /v1/models` from the same IP returns `200`. The lane
answers the catalog and refuses the completions. That is the lane enforcing a
per-source-IP limit, not the client doing something wrong.

**Check.** The sidecar converts upstream 401/403 into `502` at its edge so a
rejected upstream call is not mistaken for a bad local API key, and it appends an
explanation to the body:

```
The Zen free lane rejected this request as not coming from OpenCode. Most often
this is a quota or egress-IP limit rather than a bug: the anonymous lane is
rate-limited per source IP. Retry later, or set OPENCODEZEN4HERMES_USER_AGENT to
match your installed CLI version.
```

**Fix.**

- Wait and retry — quota windows pass.
- Leave a shared NAT, a VPN, or a datacenter range. A residential egress works
  better, and this is the documented reason.
- If you are behind a machine that has an OpenCode CLI installed, set
  `OPENCODEZEN4HERMES_USER_AGENT` to that CLI's exact version string so the wire
  UA matches what the lane sees from a normal client. The lane only validates the
  `opencode/<semver>` prefix, so this is rarely the deciding factor.
- Understand the limit before working around it: this project deliberately ships
  no proxy pool and no egress rotation. See
  [../README.md#batasan-yang-jujur](../README.md#batasan-yang-jujur).

Probe the lane directly, bypassing the sidecar, to confirm the diagnosis:

```bash
python3 scripts/verify_live.py --model deepseek-v4.1-flash-free --max-tokens 300
```

`rejected` in the verdict column with an HTTP 403 is the lane; `error` is
something else.

---

## Responses come back empty

**Cause.** `max_tokens` is too small for a reasoning model. These models emit
hidden reasoning tokens before any visible text, and the hidden tokens count
against the budget. At `max_tokens` 10 or 32 a reasoning model spends the entire
allowance before it starts answering, so the completion is legitimately empty.

**Check.** The models that do this in this roster are `longcat-2.5-preview-free`,
`nemotron-3.5-lightning-free`, and `nemotron-3.5-super-vl-free`; `big-pickle` is
the related case where a `</think>` marker leaks into otherwise valid text.
`docs/GATE.md` and [E2E_PROOF.md](E2E_PROOF.md) both record the measurement.

**Fix.** Use a budget of at least 300:

```bash
python3 scripts/verify_live.py --max-tokens 300
```

`scripts/verify_live.py` already defaults to 300 and says why in `--help`. An
empty answer at `max_tokens 10` means nothing; an empty answer at 300 is worth
investigating.

---

## Port 8799 is already in use

**Cause.** Something already holds the port — most often a sidecar you started
earlier and forgot, or a leftover process from a previous WebUI run.

**Check.** Start the sidecar and read the exit code. It is deliberately specific:

```
ERROR port 8877 on 127.0.0.1 is taken by another process; set OPENCODEZEN4HERMES_PORT
```

Exit status is **2** for a taken port, distinct from 1 for other errors, so a
script can tell the two apart.

**Fix.** Either use the running sidecar, or move to a free port and keep the seed
in step:

```bash
python3 -m sidecar --port 8801
/tmp/venv/bin/python install/seed_config.py --port 8801
```

The two must agree — the seed writes the port into `base_url`, and the sidecar
listens on the port you pass it. If an entry already exists at the old port, the
seed reports the drift rather than overwriting it; see "You moved the sidecar
port and the seed will not follow" below.

The sidecar also treats "my own address already answers `/healthz`" as success
rather than as a conflict, so restarting it over a live instance is a no-op
instead of an error.

---

## The sidecar is not running

**Cause.** Nothing started it, or it crashed. In a pack the launcher starts it
for you; in a checkout nothing does.

**Check.** The launcher's decisions are all logged:

```bash
tail -n 40 <pack>/state/launcher.log
tail -n 40 <pack>/state/sidecar.log
```

Every line is prefixed with a UTC timestamp. A healthy launcher run logs three
steps:

```
2026-10-19T20:39:59Z [launcher] step 1/3 sidecar already answering at http://127.0.0.1:8799
2026-10-19T20:39:59Z [launcher] step 2/3 seeding providers.opencode-zen-free on port 8799
2026-10-19T20:39:59Z [launcher] step 2/3 config seed ok
2026-10-19T20:39:59Z [launcher] step 3/3 exec bootstrap.py with HERMES_WEBUI_PORT=8789
```

A failure keeps the same shape but is prefixed `WARN`, and it is also echoed to
stderr as `hermes-webui: WARN ...` so it is visible even without reading the log:

```
2026-10-19T20:41:02Z [launcher] WARN step 1/3 sidecar did not answer http://127.0.0.1:8799/healthz within 20 probe(s) — see <pack>/state/sidecar.log; continuing anyway
```

`$STATE` is `$HERMES_WEBUI_STATE_DIR` if set, otherwise `<pack>/state`.

**Fix.** Start it, in a pack:

```bash
<pack>/bin/opencodezen4hermes-sidecar
```

or in a checkout:

```bash
cd /root/hermess-webui-agent && python3 -m sidecar --verbose
```

Then confirm it independently of the WebUI:

```bash
curl -s http://127.0.0.1:8799/healthz
```

`/healthz` is the only liveness contract and it never depends on the upstream
lane being reachable — it answers even when the lane is down, precisely so a
network problem cannot make the local health check lie.

Note that the launcher's steps are best-effort and never fatal: the WebUI starts
even if the sidecar failed. A sidecar that did not come up is a `WARN` line in
`launcher.log`, not a startup crash. So "the WebUI runs fine" does not mean the
sidecar is running.

---

## The model list is stale

**Cause.** The catalog is cached. `--catalog-ttl` / `OPENCODEZEN4HERMES_CATALOG_TTL`
default to 300 seconds, and the pricing metadata from models.dev is cached for a
day — pricing moves on the order of weeks, so the long TTL is intentional.

**Check.** `/healthz` reports exactly how old the snapshot is:

```json
{"ok": true, "catalog": {"status": "stale", "total": 85, "exposed": 9, "prices_ready": true, "age_seconds": 1549.4, "last_error": null}}
```

`status: "stale"` with a nonzero `age_seconds` is normal and means a refresh is
in flight; `last_error` is where a fetcher failure is recorded.

**Fix.** For a one-off, set the TTL to zero on the command line:

```bash
python3 -m sidecar --catalog-ttl 0
```

Do not leave it there — every request now re-fetches, which is both slow and
unfriendly to the lane.

A model that disappeared from the list on its own is the derived roster working
as intended: it was delisted upstream or its pricing stopped being zero. A model
that *stays* listed after being delisted is the bug the derivation exists to
prevent.

---

## The picker lists a model but every request fails

**Cause.** Model-specific protocol or lifecycle states, not a transport problem.

**Check.** The sidecar maps the lane's error types to a specific sentence, so
read the message rather than the status code:

| Upstream error type | What it means |
|---|---|
| `ModelProtocolUnsupported` | The model rejects the chat-completions shape (reasoning-only). |
| `ModelDeprecated` | Delisted upstream. Refresh the model list. |
| `ModelUnavailable` | Temporarily unavailable upstream. Retry shortly. |
| region-blocked | Region-locked from your network. |

**Fix.** For `ModelProtocolUnsupported` there is no local fix — that is why
`jev-1.13-free` is in `FREE_LANE_BLOCKLIST` and never reaches the picker. If you
meet one in the wild, use another model and report the id.

---

## A large context window is rejected

**Cause.** Some of these models advertise a context window far past 200 K, and a
client may second-guess the number it was told.

**Check.** The values the sidecar serves come from the live catalog, with exactly
one local override: `mimo-v2.6-flash-free` is reported as 1 048 576 even though
models.dev says 200 000. See [MODELS.md](MODELS.md).

**Fix.** Trust `/v1/models` over any hardcoded table, including the one in
[MODELS.md](MODELS.md) — that table is a snapshot, and the point of deriving the
roster is that the snapshot is allowed to be out of date.
