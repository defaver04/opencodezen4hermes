# Models

The roster is nine free OpenCode Zen models. This document explains where that
list comes from, why it is derived rather than hardcoded, what is deliberately
excluded, and what each model is good for.

---

## The roster

In picker order, with the values the sidecar serves today at
`http://127.0.0.1:8799/v1/models`:

| # | Model id | Context | Max output | Modalities |
|---|---|---|---|---|
| 1 | `beam-free` | 1 048 576 | 131 072 | text |
| 2 | `big-pickle` | 200 000 | 32 000 | text |
| 3 | `deepseek-v4.1-flash-free` | 200 000 | 32 000 | text, image |
| 4 | `glm-5.3-flash-free` | 200 000 | 32 000 | text, image, video, pdf |
| 5 | `longcat-2.5-preview-free` | 1 000 000 | 131 072 | text, image |
| 6 | `mimo-v2.6-flash-free` | 1 048 576 | 32 000 | text, image, audio, video |
| 7 | `minimax-m3.1-flash-free` | 1 000 000 | 512 000 | text, image, video |
| 8 | `nemotron-3.5-lightning-free` | 262 144 | 262 144 | text |
| 9 | `nemotron-3.5-super-vl-free` | 1 048 576 | 131 072 | text, image |

This order is a contract (`docs/BLUEPRINT.md` §3) — and it is alphabetical, which
is not a coincidence. `Catalog.models()` ends in `return sorted(out)`
(`sidecar/catalog.py:421`), so the served order is always lexicographic; the
contract order was chosen to match it rather than requiring a separate rank
table. `STATIC_FREE_MODELS` and `FALLBACK_FREE_MODELS` are written in a different
literal order on purpose — they are sets that get sorted on the way out, so
matching them to each other is unnecessary.

`plugin/opencode-zen-free/__init__.py` carries the same nine ids in
`FALLBACK_FREE_MODELS` so the picker has something to show before the sidecar's
catalog has warmed.

`deepseek-v4.1-flash-free` is additionally the profile's `default_aux_model` —
the model Hermes reaches for on small helper tasks.

---

## Why the free list is derived, not hardcoded

`sidecar/catalog.py` runs a three-stage chain and intersects the results:

- **S1** — `GET {lane}/models`. The live catalog with every id, free and paid.
- **S2** — `GET https://models.dev/api.json`. Pricing metadata; a model is free
  when `input == 0 and output == 0`.
- **S3** — `STATIC_FREE_MODELS`. A short compile-time-verified bootstrap list,
  used only while S1 or S2 has never loaded.

Exposed roster = **S1 ∩ S2-allowed**, or S3 minus the blocklist while metadata is
still pending.

The reason is not elegance; it is that a hardcoded list rots in a specific,
harmful way. The lane is not a published API and its catalog changes: models are
added, models are delisted, and a delisted id that stays in a hardcoded picker
produces a menu entry that fails only when a user clicks it. Deriving the roster
means a delisted model disappears on its own.

The subtle part is the precedence in `decide()`:

```
if not ready or not prices:        return fallback("metadata_pending")
if price is None:                  return fallback("metadata_model_missing")
if price.deprecated:               return Decision(False, "metadata_deprecated", True)
if price.input == 0 and price.output == 0:
                                   return Decision(True, "metadata_free", True)
if price.input is None or price.output is None:
                                   return Decision(False, "metadata_cost_unknown", False)
return Decision(False, "metadata_paid", True)
```

**A ready metadata verdict always wins, including a negative one.** The `"free"
in id` name heuristic fires only when metadata cannot speak — when prices are
still loading, or when the id is absent from the feed. Getting that order
backwards is exactly how the upstream project kept a delisted
`deepseek-v4-flash-free` in its picker forever: the name said "free", the id was
listed, and nobody re-checked the pricing.

The practical consequence for users: `models_discovered: false` in the config
seed plus a live sidecar means the list you see is whatever the lane and
models.dev agreed on at the last refresh, not a list frozen in this repository.

### One local override

`LOCAL_MODEL_OVERRIDES` raises `mimo-v2.6-flash-free` to a 1 048 576 context
window. models.dev declares 200 000 for it, but the MiMo V2.6 Flash model card
and the official CLI config both say 1 MiB, and the live lane accepts the larger
number. It is the single hand-maintained number in the catalog and it is
commented as such in the source.

---

## The blocklist

`FREE_LANE_BLOCKLIST` holds three ids that models.dev considers free and that the
lane lists, but that the chat surface cannot actually serve. Each has a distinct
reason, recorded in `sidecar/catalog.py`:

| Model id | Why it is excluded |
|---|---|
| `exo-free` | `410 ModelDeprecated` — delisted upstream. |
| `jev-1.13-free` | `400 ModelProtocolUnsupported` — a reasoning-only model that rejects the chat-completions request shape. |
| `ling-3.0-flash-fin-free` | `400 "Model is unavailable"` — succeeded by the 3.1 line. |

The source carries the rule for removing an entry: *"Remove an entry only after a
live streaming probe answers 200 or 429."* A 403 does not count, because a 403 is
the per-egress-IP quota talking, not the model.

`include_paid` (`OPENCODEZEN4HERMES_INCLUDE_PAID=1`, or `--include-paid`) widens
the exposed roster past the free verdict for debugging. It is off by default and
the default roster never contains a paid model.

---

## Per-model notes

Observed against the live lane from this host on 2026-10-20 at `max_tokens` 300
unless stated otherwise. Verdicts come from `scripts/verify_live.py` and
`scripts/e2e_proof.py`; the token counts and latencies are in
[E2E_PROOF.md](E2E_PROOF.md).

- **`beam-free`** — the largest window in the roster, 1 MiB. Returns "pong"
  immediately; the right default when a task needs to hold a lot of text.
- **`big-pickle`** — a reasoning model. It answers `200`, but the raw text can
  begin with a leaked `</think>` marker before the actual reply. Harmless for a
  chat UI; something to strip if you parse the output strictly.
- **`deepseek-v4.1-flash-free`** — the profile default and the fastest responder
  measured (TTFT ≈ 2.2 s). Accepts images. Start here.
- **`glm-5.3-flash-free`** — the only roster entry advertising `pdf` input, so it
  is the one to pick when documents go in. Also accepts images and video.
- **`longcat-2.5-preview-free`** — 1 M window, reasoning. At `max_tokens` 32 it
  returns empty text: the whole budget goes to hidden reasoning tokens. At 300 it
  answers normally.
- **`mimo-v2.6-flash-free`** — 1 MiB window via the local override, and the
  widest modality set: text, image, audio, video.
- **`minimax-m3.1-flash-free`** — the largest output budget in the roster at
  512 000 tokens, and a 1 M context to go with it. Pick it for long generations.
- **`nemotron-3.5-lightning-free`** — context and max output are the same
  number, 262 144. Slowest observed: 40.5 s for one turn in the nine-model run,
  queueing behind the others on a rate-limited egress IP.
- **`nemotron-3.5-super-vl-free`** — the vision-language entry: 1 MiB window,
  image input. Also returns empty text at tiny budgets.

### On empty responses

Several of these models reason before they speak, and the reasoning tokens count
against `max_tokens`. At a budget of 10 or 32 they spend everything on hidden
tokens and emit nothing visible. This is not a broken model and not a sidecar
bug: `scripts/verify_live.py` defaults to `--max-tokens 300` and says so in
`--help`. If a model answers empty, raise the budget before investigating
anything else.
