# The Zen free-lane gate — decoded from live probes (2026-10-20)

Every claim below was measured against `https://opencode.ai/inference/v1/chat/completions`
from this host, one variable at a time. The upstream `opencodezen4dsh` docs describe a
stricter gate than what is actually enforced; where they differ, the probe wins and the
probe is quoted.

## Summary — the whole gate in four lines

```
1. header  user-agent   MUST start with "opencode/"
2. header  x-opencode-session  MUST be "ses_" + 12 hex + 14 Base62
3. body    stream       MUST be true
4. body    tools        MUST contain a function named exactly "read"
```

Everything else the upstream plugin does — the 11-tool official set, the 9.5 KB official
system prompt, `x-opencode-client`, `x-opencode-request`, `x-opencode-project`,
`tool_choice:auto`, `stream_options`, the Zen user uuid — is **optional**. It is passed
through anyway (harmless, and it keeps us aligned if upstream tightens), but the port does
not depend on it.

## Evidence

### Streaming is mandatory

| Request | Result |
|---|---|
| `stream: true` | **200** |
| `stream: false` | 403 `FreeTierError` |
| `stream` absent | 403 `FreeTierError` |

This is the trap that cost the first round of investigation: a non-streaming probe is not a
weaker gate test, it is an *invalid* one. Any future doctor script must stream.

### The `user-agent` must begin with `opencode/`

| `user-agent` | Result |
|---|---|
| `opencode/1.19.3 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14` | 200 |
| `opencode/1.19.3` | **200** |
| `opencode/1 ai-sdk/provider-utils/1` | 403 |
| `opencode/1 runtime/bun/1` | 403 |
| `Mozilla/5.0 ai-sdk/provider-utils/1` | 403 |

The upstream claim that an `ai-sdk`/`bun` token is *required* is **false** as of today:
`opencode/1.19.3` alone passes. But `opencode/1 ...` fails, so the parser is stricter than a
prefix check — it looks like a real `name/version` parse plus a version floor. We therefore
send the full documented UA and keep `opencode/<semver>` as the floor.

### `x-opencode-session` must be canonical, and nothing else about it is checked

| `x-opencode-session` | Result |
|---|---|
| `ses_` + tU(Date.now()) | 200 |
| `ses_` + `0`×12 + `b`×14 (1970 timestamp) | **200** |
| `ses_` + `a`×12 + `b`×14 | **200** |
| `ses_` + tU(+30 d) / tU(−1 h) | 200 |
| `ses_zzz` | 403 |
| absent | 403 |

`^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$` is the whole rule. The upstream "server validates the
timestamp" finding (392 KB binary audit, `ids.ts` comments) **no longer holds** — a 1970
timestamp passes. We still generate timestamp-valid ids because it costs nothing and would
survive a re-tightening, but we no longer *depend* on it.

### The tool rule is a single required name: `read`

This is the most useful correction. The upstream `ensureFreeLaneShape` rewrites the tool list
to exactly the 11 official tools. That is unnecessary — and actively harmful for a Hermes
port, because it destroys the tool names Hermes actually understands.

| `tools` | Result |
|---|---|
| exactly the 11 official | 200 |
| `bash,edit,glob,grep` (4 official names) | 403 |
| `bash,edit,glob,grep,read` (5) | **200** |
| single official `read` | **200** |
| single official `bash` / `edit` / `glob` / `grep` / `skill` / `task` / `todowrite` / `webfetch` / `websearch` / `write` | 403 |
| custom tool named `read`, trivial schema | **200** |
| custom tool named `read_file` | 403 |
| custom tool named `Read` / `READ` / `read_` | 403 |
| 8 Hermes-named tools, none named `read` | 403 |
| 8 Hermes-named tools **+ `read`** | **200** |
| 1 huge custom tool (3 KB description) | 403 |
| `tools` absent | 403 |

So: **one tool named exactly `read`, plus whatever else you like.** Description and schema
are not compared (`official read, params stripped` → 200), position does not matter, extras
are tolerated, and `tool_choice` is irrelevant (`none` and `required` both pass).

### Not required at all

`authorization`, `x-opencode-client`, `x-opencode-request`, `x-opencode-project`,
`stream_options`, `tool_choice`, the official system prompt (a minimal or absent system
message passes), and `body.user`.

## Why this matters for the port

Because only one tool name is required, the sidecar **does not** have to tunnel Hermes tools
through a fake official set. It can advertise the real Hermes tools and just guarantee a
`read` entry is present. The model then calls `read_file`, `glob`, `bash` by their real
names, which is exactly what a Hermes agent expects. That removes the entire tool-bridge
problem class — see `docs/DESIGN.md` §4.3.

The official system prompt is likewise optional. Hermes sends its own system prompt; we pass
it through untouched. No prompt surgery, no context-window waste on a 9.5 KB CLI preamble.

## Live roster (same session, `max_tokens` 32, one prompt)

| model | result |
|---|---|
| `deepseek-v4.1-flash-free` | 200, "pong" |
| `big-pickle` | 200 (emits a `</think>` prefix) |
| `glm-5.3-flash-free` | 200, "pong" |
| `mimo-v2.6-flash-free` | 200, "pong" |
| `minimax-m3.1-flash-free` | 200, "pong" |
| `beam-free` | 200, "pong" |
| `longcat-2.5-preview-free` | 200, empty text at 32 tokens |
| `nemotron-3.5-lightning-free` | 200, empty text |
| `nemotron-3.5-super-vl-free` | 200, empty text |
| `jev-1.13-free` | 400 `ModelProtocolUnsupported` |

`jev-1.13-free` is a reasoning-only model that rejects the chat-completions shape, so the
catalog keeps it but the sidecar surfaces its 400 verbatim rather than pretending it works.
The empty-at-32-tokens models are reasoning models that spend the budget on hidden tokens;
they are fine at real budgets and are not excluded.

## Reproducing

```bash
python3 scripts/verify_live.py --model deepseek-v4.1-flash-free
```

The script streams a minimal prompt and asserts a 200 plus at least one content delta. It
exists because a non-streaming check would report a false failure — see above.
