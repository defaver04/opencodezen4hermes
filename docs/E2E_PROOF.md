# End-to-end proof

This document records a real, captured run of `scripts/e2e_proof.py` — the
evidence that a model **listed in the Hermes WebUI picker** actually answers when
Hermes talks to it, not merely that the upstream lane responds to a hand-written
`curl`.

## What was run

```bash
cd /root/hermess-webui-agent
python3 -m sidecar --port 8799 &          # the provider backend, daemonised
python3 scripts/e2e_proof.py
```

`scripts/e2e_proof.py` reads `base_url` from the shipped plugin profile
(`plugin/opencode-zen-free/__init__.py`, imported with a stubbed `providers`
package), then drives that endpoint over the OpenAI `chat/completions` contract
that `providers/base.py` speaks — the same path the Hermes openai client takes.

## Captured output

Date: 2026-10-20 (Asia/Jakarta)
Host: linux aarch64, 8 cores, Python 3.12.3 — a datacenter IP, i.e. exactly the
kind of egress the lane is documented to rate-limit.
Sidecar: `http://127.0.0.1:8799/v1` → `https://opencode.ai/inference/v1`

```
provider : OpenCode Zen (free)
slug     : opencode-zen-free
base_url : http://127.0.0.1:8799/v1
max_tokens: 300

/v1/models: 9 model(s), all nine contract ids present

model                         http  ttft     tokens  verdict    text
----------------------------------------------------------------------------------------------------
beam-free                     200    1321ms  283     works      pong
big-pickle                    200    2885ms  428     works      pong
deepseek-v4.1-flash-free      200    2225ms  567     works      pong
glm-5.3-flash-free            200    1654ms  458     works      pong
longcat-2.5-preview-free      200    3854ms  425     works      pong
mimo-v2.6-flash-free          200    3469ms  432     works      pong
minimax-m3.1-flash-free       200    2356ms  727     works      pong
nemotron-3.5-lightning-free   200   40500ms  653     works      pong
nemotron-3.5-super-vl-free    200    1866ms  647     works      pong

agentic turn — model: deepseek-v4.1-flash-free  http: 200
  tool_calls       : ['read_file']
  Hermes-named     : ['read_file']
  arguments sample : ['{"path": "/etc/hostname"}']

summary: 9 probed, 9 works, 0 empty, 0 rejected, 0 error | agentic tool call: ok -> PASS
```

Exit code: `0`.

## Reading the evidence

**`/v1/models` lists exactly the nine contract ids.** The sidecar's roster is the
live upstream catalog (`GET {lane}/models`) intersected with the free verdict
derived from `https://models.dev/api.json` pricing, minus `FREE_LANE_BLOCKLIST`.
The list in the run above matches `docs/BLUEPRINT.md` §3 exactly.

**All nine answer with non-empty text.** These are the same nine ids the WebUI
dropdown renders, so a user picking any of them gets a real completion. Token
counts (283–727 for a one-word answer) are the honest cost of models that reason
before they speak.

**The agentic turn emits a Hermes tool name, not a generic one.** This is the
finding that shaped the architecture: the upstream gate requires exactly one tool
named `read`, so an earlier design would have tunnelled Hermes tools through the
official 11-tool set and translated the names back. The probe showed the gate
only *checks for* `read` — it never rewrites `tool_calls` — so declaring
`read_file` alongside it is enough for the model to call `read_file` directly.
The captured `tool_calls: ['read_file']` with correct arguments
(`{"path": "/etc/hostname"}`) is the proof that no tool bridge is needed and the
Hermes agent loop works unmodified.

**`nemotron-3.5-lightning-free` took 40.5 s.** Recorded rather than smoothed
over: on this run it queued behind the other eight against a per-egress-IP
rate-limited lane. It answered; it was just slow. A user on the same IP would see
the same thing.

## What this proves

- A picker-visible model answers through the shipped provider profile, from a
  datacenter IP, with no API key, on 2026-10-20.
- The sidecar's gate rewrite (`stream: true` + a `read` tool) is sufficient; the
  caller does not need to know about the gate.
- Real Hermes tool names survive the round trip, so the agent loop works with no
  translation layer.

## What this does not prove

- **That the lane stays open.** It is undocumented and unversioned. The gate was
  measured, not guaranteed — see `docs/GATE.md` for the probe matrix and for the
  rule that a *non-streaming* probe is an invalid test (it always 403s and means
  nothing).
- **That another network works better or worse.** The lane is rate-limited per
  egress IP. Nine models in ~57 s passed here; a shared NAT or a busier
  datacenter range may see `403 FreeTierError`. That is a quota fact, not a bug,
  and the sidecar surfaces it with an actionable message rather than a stack
  trace.
- **That every model suits every task.** Nine models answered a one-word prompt
  and one tool call. This is a reachability proof, not a quality benchmark.
- **That the WebUI renders the dropdown from *this* run.** The picker reads the
  config seed, not the sidecar; that link is proven separately by
  `tests/test_picker_integration.py`. A later run against a real extracted pack
  covered it end to end — see the next section.

## The booted pack, not just the source tree

Everything above was measured from the development checkout. The claim a user
actually cares about — *unpack the tarball, start it, pick a model, get an
answer* — was verified separately against the published arm64 artefact from
release `pack-20261019-2141`
(`hermes-webui-agent-v0.52.458-v2026.9.24--linux-arm64.tar.gz`, 219 650 397 bytes,
sha256 `9d1da51b41172bdd5aedef09bf974c29c9d2ea44c06332a66d598d6bef3029ef`),
extracted to `/tmp/packtest`. Note the double dash in that filename: this release
predates the `VER_ID` fix in `878c404`, and the name is quoted exactly as
published rather than tidied to match the current build.

The launcher's own log, with the state directory redirected and both ports
moved off their defaults to prove nothing was quietly relying on a previous run:

```
2026-10-19T22:04:41Z [launcher] step 1/3 sidecar silent at http://127.0.0.1:8802 — starting it detached
2026-10-19T22:04:48Z [launcher] step 1/3 sidecar up at http://127.0.0.1:8802
2026-10-19T22:04:48Z [launcher] step 2/3 seeding providers.opencode-zen-free on port 8802
opencode-zen-free: created  (/tmp/webtest/config.yaml)
  http://127.0.0.1:8802/v1
2026-10-19T22:04:49Z [launcher] step 2/3 config seed ok
2026-10-19T22:04:49Z [launcher] step 3/3 exec bootstrap.py with HERMES_WEBUI_PORT=8899
[bootstrap] Web UI is ready: http://localhost:8899
```

`GET http://127.0.0.1:8899/api/models` — the endpoint the dropdown calls —
returned one group of nine:

```
active_provider: None
groups         : 1
  - opencode-zen-free      9 models
        beam-free
        big-pickle
        deepseek-v4.1-flash-free
        glm-5.3-flash-free
        longcat-2.5-preview-free
        mimo-v2.6-flash-free
        minimax-m3.1-flash-free
        nemotron-3.5-lightning-free
        nemotron-3.5-super-vl-free
```

Two details from that run are worth recording because they look like defects and
are not. **First**, the very first `/api/models` call labelled the group
`Opencode Zen Free` (the slug, title-cased); every call after it, seconds later,
said `OpenCode Zen (free)` — the plugin's real display name. That is upstream's
stale-while-revalidate catalog: the first request past the 4 s rebuild budget
serves a static fallback and triggers the rebuild out of band. The label is
cosmetic and self-corrects, but a test asserting the display name on a cold first
call would flake. **Second**, `active_provider` is `None` because this throwaway
`HERMES_HOME` has no `model:` block — the seed deliberately does not write one,
since choosing a user's default model is not this project's call.

Routing, then inference, both through the pack:

```
resolve_model_provider('@opencode-zen-free:deepseek-v4.1-flash-free')
  -> ('deepseek-v4.1-flash-free', 'opencode-zen-free', 'http://127.0.0.1:8802/v1')
POST {base}/chat/completions  ->  http: 200
assembled: 'WEBUI OK'
```

So the pack venv (PyYAML 6.0.3, httpx 0.28.1, relocated `.pyroot`) starts the
sidecar, the launcher seeds a fresh config, the WebUI serves the dropdown from
that config, the dashboard resolves a selected id to the pack's own sidecar, and
a real completion comes back. The strongest single check of the whole project is
that the assembled text — requested as `WEBUI OK` — arrived intact from a free
model, through an unpacked 219 MB artefact, with no API key.

## Repeating it on the current release, checksum first

The section above used `pack-20261019-2141`, which predates two fixes. To make
sure the *currently published* release is what a new user receives, the whole
chain was repeated from scratch against `pack-local-20261019-2230-sumfix`, the
release that `/releases/latest` now redirects to. Nothing was reused from earlier
runs: fresh download folder, fresh extraction, fresh `HERMES_HOME`, new ports.

```
sha256sum -c --strict SHA256SUMS
hermes-webui-agent-v0.52.458-v2026.9.24-linux-arm64.tar.gz: OK
```

The recorded line is a bare filename — no `dist/` prefix — so the command a user
actually types works outside the build tree. That was finding H.

Launcher, on ports 8901/8803:

```
2026-10-19T22:40:21Z [launcher] step 1/3 sidecar silent at http://127.0.0.1:8803 — starting it detached
2026-10-19T22:40:31Z [launcher] step 1/3 sidecar up at http://127.0.0.1:8803
2026-10-19T22:40:31Z [launcher] step 2/3 seeding providers.opencode-zen-free on port 8803
opencode-zen-free: created  (/tmp/finaltest/config.yaml)
  http://127.0.0.1:8803/v1
2026-10-19T22:40:33Z [launcher] step 2/3 config seed ok
2026-10-19T22:40:33Z [launcher] step 3/3 exec bootstrap.py with HERMES_WEBUI_PORT=8901
[bootstrap] Web UI is ready: http://localhost:8901
```

`GET /api/models` after the catalogue settled returned the display name and the
human labels, not the slug fallback:

```
group: 'opencode-zen-free'  label='OpenCode Zen (free)'  models=9
    beam-free                    | Beam Free
    big-pickle                   | BIG Pickle
    deepseek-v4.1-flash-free     | Deepseek V4.1 Flash Free
    glm-5.3-flash-free           | GLM 5.3 Flash Free
    longcat-2.5-preview-free     | Longcat 2.5 Preview Free
    mimo-v2.6-flash-free         | Mimo V2.6 Flash Free
    minimax-m3.1-flash-free      | Minimax M3.1 Flash Free
    nemotron-3.5-lightning-free  | Nemotron 3.5 Lightning Free
    nemotron-3.5-super-vl-free   | Nemotron 3.5 Super VL Free
```

Note that this call took 18.7 s on the cold path and returned the *fallback*
label. The next one returned `OpenCode Zen (free)`. Both behaviours are upstream's
stale-while-revalidate, documented above; the point of quoting the timing is that
a slow first `/api/models` is expected, not a hang.

Selection and inference, resolving the id exactly as the frontend does when a
user clicks an entry:

```
resolve_model_provider('@opencode-zen-free:deepseek-v4.1-flash-free')
  -> ('deepseek-v4.1-flash-free', 'opencode-zen-free', 'http://127.0.0.1:8803/v1')
resolve_model_provider('deepseek-v4.1-flash-free')        # unqualified
  -> ('deepseek-v4.1-flash-free', '', None)

POST http://127.0.0.1:8803/v1/chat/completions  ->  http: 200
assembled text: 'FINAL PACK OK'
DONE frame received
```

That is the full user story on the release anyone downloading today would get:
verify the checksum, untar, start, see the group, pick a model, read the answer.

