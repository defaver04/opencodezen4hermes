# Review — adversarial audit of this repo

Scope: everything in this repository, reviewed as if it were a stranger's code
about to be handed a free-model provider that proxies other people's traffic.
Every claim below was produced by running a command on this host (linux
aarch64, Python 3.12.3, 2026-10-20), not by reading the code and guessing.

Three findings were real defects in this repo's own code. They were **fixed**
rather than merely reported; each has a regression test that fails on the old
behaviour. One finding was a documentation claim contradicted by the code, also
fixed. Nothing is listed as "known issue" that could be fixed in the same pass.

---

## Status summary

| # | Severity | Finding | Status |
|---|---|---|---|
| A | BLOCKER | The catalog was fetched once at boot and never again, so `OPENCODEZEN4HERMES_CATALOG_TTL` was dead configuration and a long-running sidecar served a frozen roster forever | **Fixed** |
| B | BLOCKER | `POST /v1/messages` skipped the free-roster check, so it was an unguarded second door onto the lane | **Fixed** |
| C | SHOULD-FIX | `_read_json` trusted `Content-Length` with no cap and no timeout — one client could pin a worker thread or force an unbounded read | **Fixed** |
| D | SHOULD-FIX | No `Origin` validation, so any web page the user visited could POST to loopback and spend their free-lane quota | **Fixed** |
| E | SHOULD-FIX | `docs/ARCHITECTURE.md` claimed `_handle_messages` resolved the model against the catalog; the code (and a test) said otherwise | **Fixed** |
| F | NIT | The tarball is never extracted and booted in CI; the only runtime assertion is a `grep` on one file | Documented |
| G | NIT | The launcher's final `exec` of a missing/non-executable interpreter surfaces as a raw shell error | Documented |

Test suite after the fixes: **408 tests, 0 failures, 0 errors, 38 skipped**
(`python3 -m unittest discover -s tests`). Before the work in this review the
same tree ran 388 tests with 1 failure and 1 error.

---

## A — The catalog never re-derived after boot (BLOCKER, fixed)

**How it was found.** `/healthz` was polled over a long session and
`age_seconds` climbed monotonically — 4.3 → 2084.5 → 2742.7 — with `status`
flipping to `stale` past 600 s and staying there.

**Root cause.** `Catalog.refresh()` (`sidecar/catalog.py:338`) is the only
writer of `self._refreshed_at`, and its only caller was `warm()`
(`sidecar/catalog.py:384`), itself called once from `serve()` before the socket
binds. Nothing refreshed on a timer or per request. The TTL *was* consulted —
but only inside `refresh()`, which therefore had to be called to read it. Dead
configuration wearing a live name.

**Why it was not fatal.** The nine shipped ids are compiled in
(`STATIC_FREE_MODELS`) and `decision()` has a `static_verified` rescue for
`{metadata_deprecated, metadata_model_missing, metadata_pending}`, so the roster
could not silently empty. The failure mode was quieter: a free model added
upstream would never appear without a restart.

**Fix.** New `sidecar/catalog.py:396` `def maybe_refresh(self) -> bool`,
called from `GET /v1/models`, `POST /v1/chat/completions` and
`POST /v1/messages` — and deliberately **not** from `/healthz`, because
BLUEPRINT §5 says liveness must never depend on the lane.

It is stale-while-revalidate, not refresh-on-read:

- returns immediately; the caller answers from the current snapshot and the next
  caller sees the fresh one, so no request ever waits on `models.dev`;
- schedules at most one refresh (`_scheduled`/`_refreshing`, checked under the
  existing lock), so a burst spawns one thread rather than one per request;
- fails quietly — `refresh()` never raises, the reason lands in `last_error`,
  and the previous roster keeps serving.

**Evidence.** `tests/test_catalog.py::MaybeRefreshTest` (7 tests) and
`tests/test_server.py::CatalogRefreshWiringTests` (3 tests), which pin the
wiring itself, including that `/healthz` does *not* trip the hook. Full suite:
408 tests green.

## B — `/v1/messages` bypassed the free-roster gate (BLOCKER, fixed)

**How it was found.** The same model was sent to both POST routes:

```
exo-free via /v1/chat/completions → 400 {"type": "model_not_available",
                                          "(verdict: local_blocklist)"}
exo-free via /v1/messages         → 502 {"type": "upstream_error",
                                          "OpenCode's free tier can only be
                                           used from within OpenCode…"}
```

The second response proves the request reached the lane: the sidecar forwarded a
model it exists to refuse.

**Root cause.** `_handle_messages` did no `decision()` check at all — it read the
body, minted ids, and forwarded. A test documented this
(`test_messages_does_not_gate_check_the_model`, "deliberate"), which is how a bug
acquires a rationale: the comment described the implementation, not a
requirement. The requirement is one sentence — *this sidecar only carries free
models* — and it cannot be route-dependent.

**Fix.** `_handle_messages` now performs the same decision check as
`_handle_chat`, including the missing-`model` case, before minting ids or
touching the network. The replacement test is
`test_messages_gate_checks_the_model_like_the_chat_route`, which asserts 400,
the `model_not_available` kind, *and* that nothing was forwarded.

**A note on the removal.** Deleting a passing test needs justification, so: it
asserted the buggy behaviour, it could not pass alongside a correct
implementation, and the behaviour it asserted contradicted BLUEPRINT §5 and the
module's own docstring. It was replaced, not deleted outright.

## C — Unbounded request body read (SHOULD-FIX, fixed)

**How it was found.** A raw socket announcing `Content-Length: 999999999` and
sending no body made the handler block waiting for 999 MB; no response arrived
within a 3 s timeout. The client's own header was the only bound.

**Fix.** `sidecar/server.py:119` `_read_json` now:

- refuses `length > MAX_BODY_BYTES` (`sidecar/server.py:44`, 32 MiB) with 413
  `request_too_large` **before reading** — the announced size is rejected, not
  honoured;
- bounds the body read with `BODY_READ_TIMEOUT` (30 s), answering 408
  `request_timeout`, and restores the socket timeout afterwards so it cannot
  clip the long-lived SSE response that follows.

32 MiB is far beyond a real turn (a long conversation plus inline images) and
small enough that a hostile client cannot make the process allocate its way to
death. `tests/test_server.py::BodyLimitTests` pins all of it, including a floor
assertion so nobody "hardens" the cap down to a value that breaks normal use.

**Residual risk, stated plainly.** A client can still open many connections and
hold them 30 s each; `ThreadingHTTPServer` has no connection cap. The sidecar
binds loopback only, and the guarded path is local, so this is a local-DoS
surface, not a remote one. Not fixed here because it needs a concurrency design
(limits, backpressure) rather than a cap.

## D — Cross-origin POSTs were accepted (SHOULD-FIX, fixed)

**How it was found.** `grep` for `Origin|cors|access-control` in
`sidecar/server.py` returned nothing, and a POST with `Origin:
https://evil.example` was forwarded upstream normally.

**Why it matters.** The sidecar has no authentication by design (the lane is
anonymous, so there is no secret to protect). But loopback is reachable from any
page the user visits, so a visited site could spend their free-lane quota. That
the quota is free is not the same as it being someone else's to spend.

**Fix.** `sidecar/server.py:105` `_cross_origin` refuses a POST whose `Origin`
is neither absent nor loopback (`127.0.0.1`, `localhost`, `[::1]`, any port) with
403 `cross_origin_refused`. `Origin: null` (sandboxed iframe, `file://`) is
treated as cross-origin because it identifies no loopback page. Reads stay open
so a browser can load `/healthz` and `/v1/models`. Hermes and curl send no
`Origin`, so the functional path is untouched — verified live, below.

## E — Documentation contradicted the code (SHOULD-FIX, fixed)

`docs/ARCHITECTURE.md:150-152` said `_handle_messages` "resolve[s] the model
against the catalog, then hand[s] off", and `:259` described
`OPENCODEZEN4HERMES_CATALOG_TTL` as "how often the local snapshot is
re-derived". Both were false when written — the first immediately (the code did
not resolve anything), the second because nothing re-derived. The first is now
true as a result of finding B, the second as a result of finding A, and
`ARCHITECTURE.md` gained a "Keeping the roster fresh" section plus a guard
description so the next reader is not misled. Documentation that describes
intentions rather than behaviour is worse than no documentation, because it
retires the reader's suspicion.

## F — The tarball is never booted in CI (NIT, documented)

**How it was found.** Read both release workflows end to end. The only runtime
assertion on the built artefact is a content check on a single file:
`tar -xzOf "$TARBALL" ./bin/hermes-webui | grep -q "OPENCODEZEN4HERMES"`
(`.github/workflows/release-checkout.yml:118`, `.github/workflows/release.yml:110`).
No workflow extracts the tarball, starts it, and asks it a question.
`build.sh` has no boot smoke test either.

**Why it is only a NIT.** The pack's behaviour is covered by
`tests/test_pack_install.py` (51 tests) against a synthetic tree, and the whole
flow was exercised manually against a stub pack during this review: install
wrote all seven artefacts, and the generated launcher degraded correctly
(`WARN step 1/3 sidecar did not answer … within 20 probe(s)`, `WARN step 2/3 …
missing — cannot run the config seed`, exit 0). Those are warnings by design —
a launcher step must never be fatal.

**Why it is worth stating.** A green build that has never been started is a
claim, not evidence. Closing it needs an arm64 runner with a real upstream tree,
which is what the release workflow already provisions; the step to add is
"extract, start with `--no-browser`, `curl /healthz`, assert `exposed: 9`, kill".
Left open deliberately: it costs minutes of arm64 CI per build, and the honest
answer is that it should be added when the lane's availability is the thing that
keeps breaking, not before.

## G — The launcher's final `exec` is not guarded (NIT, documented)

**How it was found.** Ran the generated launcher against a stub pack whose
`.venv/bin/python` did not exist. Steps 1/3 and 2/3 warned and continued
correctly, then:

```
line 119: /tmp/fakestage/.venv/bin/python: Permission denied
```

exit 0. A missing interpreter is not the launcher's to fix, and 0 is arguably
right (the launcher's contract is "best effort, never fatal"), but the user sees
a raw shell error rather than a sentence telling them the pack is incomplete.
`docs/TROUBLESHOOTING.md` covers the case; a one-line pre-flight check with a
better message would be an improvement.

---

## What was checked and found sound

Not an exhaustive list of the codebase — a list of the things that were
specifically probed and did not break.

**Routing.** The picker's `@provider:` prefix actually routes, which is the
assumption the whole design rests on. `config.resolve_model_provider(
"@opencode-zen-free:deepseek-v4.1-flash-free")` returns
`('deepseek-v4.1-flash-free', 'opencode-zen-free', 'http://127.0.0.1:8799/v1')`,
so a selection from our group lands on the sidecar.

**The seed is load-bearing.** `scripts/picker_probe.py` runs both arms against
the real upstream picker: with the §7 seed, `opencode-zen-free` appears with 9
models and `has_key=True key_source='config_yaml'`; without it, the group is
absent. `tests/test_picker_integration.py` (12 tests) pins both directions, so a
future upstream change that makes the seed unnecessary fails loudly instead of
quietly leaving a stale dependency in the docs.

**Dependency discipline.** `pyproject.toml` declares `dependencies = []`. An
`ast` audit of every `.py` found exactly three `yaml` imports, all optional and
guarded (`scripts/doctor.py:153`, `install/seed_config.py:81`,
`tests/test_config_seed.py:42`), plus the `providers` imports the plugin cannot
avoid. This is what lets CI run with no install step at all.

**The suite runs in a bare environment.** A copy at `/tmp/ci-sim` run with
`PATH=/usr/bin:/bin` (no PyYAML, no httpx) exits 0: the network-dependent tests
skip via `skipUnless` rather than fail, so a contributor without the pack venv
gets `OK (skipped=38)`, not red.

**Version single-sourcing.** `sidecar/__init__.py:3` `__version__ = "0.1.0"`,
`plugin/opencode-zen-free/plugin.yaml` `version: 0.1.0`, and `pyproject.toml`'s
`dynamic = ["version"]` all agree, enforced by
`tests/test_plugin_profile.py::test_manifest_version_matches_the_sidecar_version`.

**Thread safety.** Every catalog read and write holds
`sidecar/catalog.py:239 self._lock`; the new `_scheduled` flag is checked and set
under the same lock.

**No secrets.** Grep for `ghp_`, `sk-`, `AKIA`, and `PRIVATE KEY` across the
tree: clean. The GitHub PAT used to create this repository was never written to
a file in it.

**Honest degradation.** An unknown model returns 400 with the verdict spelled
out and a pointer to `GET /v1/models`; a 401/403 from the lane is mapped to 502
with the lane's own text attached, so the WebUI does not render a gate verdict
as an auth failure.

---

## Live verification after the changes

The three fixes were re-verified against the real lane, not only in unit tests.
Sidecar restarted on 8799, then:

```
GET  /healthz                  → 200 {"ok": true, "catalog": {"status": "ready",
                                        "exposed": 9, "prices_ready": true}}
POST /v1/chat/completions
     Origin: https://evil.example → 403 {"type": "cross_origin_refused"}
POST /v1/messages  claude-fable-5 → 400 {"type": "model_not_available",
                                        "(verdict: metadata_paid)"}
POST with Content-Length: 1000000000 → 413 {"type": "request_too_large",
                                        "exceeds the 33554432-byte limit"}
```

and real traffic still flows, with Hermes-shaped tools advertised:

```
deepseek-v4.1-flash-free         http=200   2.17s  'pong'
glm-5.3-flash-free               http=200   1.35s  'pong'
```

The guards did not cost the functional path anything, which is the only
acceptable outcome for a hardening change.

---

## What this review did not cover

Stated so the audit is not mistaken for more than it is.

- **The real WebUI in a browser.** The picker was proven by calling
  `api.config.get_available_models()` directly, not by clicking the dropdown.
  The HTML path (`static/ui.js:4048 populateModelDropdown`,
  `<optgroup>`/`<option>` rendering) was read, not exercised.
- **A real arm64 build.** `build.sh` asserts `uname -m == aarch64` and this host
  is aarch64, but it was not run: 1 GB free RAM against a uv + CPython + npm
  build would OOM this box. The pack path was exercised with a stub tree
  instead. The first real build happens on CI.
- **Other networks.** Every lane observation in this repo comes from one
  datacenter IP. The lane is rate-limited per egress IP
  (`/tmp/oz4dsh/docs/ip-pool.md`), so latency and availability on a residential
  connection are unmeasured. `nemotron-3.5-lightning-free` taking 40.5 s behind
  eight other probes is this effect, not a model property.
- **Model quality.** `pong` proves reachability, not usefulness. No benchmark,
  no eval set, no comparison against the paid lane.
- **Long-run stability.** The fixes were verified over minutes. `maybe_refresh`
  under a week of uptime, and the memory profile of a long SSE stream, were not
  measured.
