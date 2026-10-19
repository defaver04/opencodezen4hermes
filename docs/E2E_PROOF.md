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
- **That the WebUI renders the dropdown from this run.** The picker reads the
  config seed, not the sidecar; that link is proven separately by
  `tests/test_picker_integration.py` (and was established by hand before it —
  with the seed, `get_available_models()` returns one group of nine; without it,
  `groups: []`).
