"""Free-model catalog: live roster ∩ models.dev pricing verdict.

Port of ``opencodezen4dsh/packages/plugin/src/adapter/catalog.ts`` trimmed to
what a sidecar needs. The decision chain matters and is reproduced exactly:

  S1  GET {lane}/models         live catalog (free and paid ids)
  S2  GET https://models.dev/api.json   pricing metadata -> the free verdict
  S3  compile-time verified ids  bootstrap list while S1/S2 are pending

Exposed roster = S1 ∩ S2-allowed, or S3 minus the blocklist while S1 is pending.

The subtle part, and the reason this is a port rather than a ``"free" in id``
check: a *ready* metadata verdict always wins, including a negative one. The name
fallback only fires when metadata cannot speak (pending, or the id is absent from
the feed). Getting that order backwards is how the upstream kept a delisted
``deepseek-v4-flash-free`` in its picker forever.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .gate import WIRE_UA

logger = logging.getLogger(__name__)

#: models.dev feed. TTL is a day: pricing changes on the order of weeks.
METADATA_URL = "https://models.dev/api.json"
METADATA_TTL = 24 * 60 * 60

#: Ids the live chat lane can never serve. Kept because the Zen catalog still
#: lists them, so S1 alone would expose dead entries. Each has a distinct reason:
#:   jev-1.13-free            400 ModelProtocolUnsupported (reasoning-only model
#:                            that rejects the chat-completions shape)
#:   exo-free                 410 ModelDeprecated / delisted upstream
#:   ling-3.0-flash-fin-free  400 "Model is unavailable", succeeded by 3.1
#: Remove an entry only after a live streaming probe answers 200 or 429.
FREE_LANE_BLOCKLIST: frozenset[str] = frozenset(
    {
        "exo-free",
        "jev-1.13-free",
        "ling-3.0-flash-fin-free",
    }
)

#: Bootstrap roster, used only while the live catalog has never loaded. Verified
#: against the anonymous lane; the list is deliberately short so a stale boot
#: cannot advertise something that no longer exists.
STATIC_FREE_MODELS: tuple[str, ...] = (
    "big-pickle",
    "deepseek-v4.1-flash-free",
    "glm-5.3-flash-free",
    "mimo-v2.6-flash-free",
    "minimax-m3.1-flash-free",
    "nemotron-3.5-lightning-free",
    "nemotron-3.5-super-vl-free",
    "longcat-2.5-preview-free",
    "beam-free",
)

#: models.dev declares ``mimo-v2.6-flash-free`` with a 200k window, but the MiMo
#: V2.6 Flash model card and the official CLI config both use 1 MiB.
LOCAL_MODEL_OVERRIDES: dict[str, dict[str, Any]] = {
    "mimo-v2.6-flash-free": {"context_window": 1048576},
}


@dataclass(slots=True)
class ModelPrice:
    input: float | None = None
    output: float | None = None
    deprecated: bool = False
    reasoning: bool = False
    effort_values: list[str] = field(default_factory=list)
    context_window: int | None = None
    max_output: int | None = None
    modalities: list[str] | None = None


@dataclass(slots=True)
class Decision:
    allowed: bool
    source: str
    known: bool


def is_free_name(model: str) -> bool:
    return "free" in (model or "").lower()


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _metadata_deprecated(raw: dict[str, Any]) -> bool:
    if raw.get("deprecated") is True:
        return True
    status = str(raw.get("status") or raw.get("lifecycle") or "").lower()
    if status in {"deprecated", "retired", "disabled"}:
        return True
    return raw.get("deprecated_at") is not None or raw.get("retirement_date") is not None


def _decode_effort_values(raw: Any) -> list[str]:
    """models.dev ``reasoning_options`` -> the ``effort`` ladder, if declared."""
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for option in raw:
        if not isinstance(option, dict):
            continue
        if option.get("type") != "effort" or not isinstance(option.get("values"), list):
            continue
        for value in option["values"]:
            if isinstance(value, str) and value and value not in values:
                values.append(value)
    return values


def _decode_modalities(raw: Any) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    kinds: list[str] = []
    for kind in raw:
        if isinstance(kind, str) and kind and kind not in kinds:
            kinds.append(kind)
    return kinds or None


def decode_models_dev(payload: Any) -> dict[str, ModelPrice]:
    """Extract the OpenCode section of the models.dev feed.

    The feed is keyed by provider id. An exact ``opencode``/``opencode-zen`` key
    wins; a merely opencode-suggesting key is accepted only when its own id or
    name mentions opencode. Anything else is ignored, so a future feed with a
    similarly named reseller cannot redefine our pricing.
    """
    result: dict[str, ModelPrice] = {}
    if not isinstance(payload, dict):
        return result

    def rank(key: str) -> int:
        lower = key.lower()
        if lower in {"opencode", "opencode-zen", "opencode_zen"}:
            return 0
        if "opencode" in lower:
            return 1
        return 2

    for key in sorted(payload.keys(), key=lambda k: (rank(k), k)):
        if rank(key) > 1:
            continue
        provider = payload[key]
        if not isinstance(provider, dict):
            continue
        if rank(key) == 1:
            identity = f"{provider.get('id') or ''} {provider.get('name') or ''}".lower().strip()
            if "opencode" not in identity:
                continue
        models = provider.get("models")
        if not isinstance(models, dict):
            continue
        for model_key, raw in models.items():
            if not isinstance(raw, dict):
                continue
            model_id = raw.get("id") if isinstance(raw.get("id"), str) and raw.get("id") else model_key
            cost = raw.get("cost") if isinstance(raw.get("cost"), dict) else {}
            limit = raw.get("limit") if isinstance(raw.get("limit"), dict) else {}
            modalities = raw.get("modalities") if isinstance(raw.get("modalities"), dict) else {}
            context_window = limit.get("context")
            max_output = limit.get("output")
            result[model_id] = ModelPrice(
                input=_num(cost.get("input")),
                output=_num(cost.get("output")),
                deprecated=_metadata_deprecated(raw),
                reasoning=raw.get("reasoning") is True,
                effort_values=_decode_effort_values(raw.get("reasoning_options")),
                context_window=int(context_window) if _num(context_window) is not None else None,
                max_output=int(max_output) if _num(max_output) is not None else None,
                modalities=_decode_modalities(modalities.get("input")),
            )
        if result:
            return result
    return result


def decide(model: str, prices: dict[str, ModelPrice], ready: bool) -> Decision:
    """Free verdict for one model. Metadata-first, name-fallback second."""
    name_free = is_free_name(model)

    def fallback(source: str) -> Decision:
        if name_free:
            return Decision(True, "name_free", False)
        return Decision(False, source, False)

    if not ready or not prices:
        return fallback("metadata_pending")
    price = prices.get(model)
    if price is None:
        return fallback("metadata_model_missing")
    if price.deprecated:
        return Decision(False, "metadata_deprecated", True)
    if price.input == 0 and price.output == 0:
        return Decision(True, "name_and_metadata_free" if name_free else "metadata_free", True)
    if price.input is None or price.output is None:
        return Decision(False, "metadata_cost_unknown", False)
    return Decision(False, "metadata_paid", True)


class Catalog:
    """Thread-safe holder for the live roster + pricing verdict + disk cache."""

    def __init__(
        self,
        lane_base: str,
        cache_dir: str | None = None,
        *,
        ttl: float = 300.0,
        user_agent: str | None = None,
        include_paid: bool = False,
    ) -> None:
        self._lane_base = lane_base.rstrip("/")
        self._cache_dir = cache_dir
        self._ttl = ttl
        self._user_agent = user_agent or WIRE_UA
        self._include_paid = include_paid
        self._lock = threading.Lock()
        self._live: set[str] = set()
        self._prices: dict[str, ModelPrice] = {}
        self._prices_ready = False
        self._refreshed_at = 0.0
        self._last_error = ""
        self._refreshing = False
        #: Set while a background refresh is in flight, so a burst of requests
        #: right after the TTL lapses spawns one refresher instead of hundreds.
        self._scheduled = False

    # -- fetchers ---------------------------------------------------------

    def _http_get_json(self, url: str, *, timeout: float = 30.0) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "authorization": "Bearer public",
                "user-agent": self._user_agent,
                "x-opencode-client": "cli",
                "accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def _fetch_live(self) -> set[str]:
        payload = self._http_get_json(self._lane_base + "/models")
        items = payload.get("data") if isinstance(payload, dict) else payload
        ids = {
            item["id"]
            for item in (items or [])
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
        }
        if not ids:
            raise ValueError("models endpoint returned an empty list")
        return ids

    def _fetch_prices(self) -> dict[str, ModelPrice]:
        prices = decode_models_dev(self._http_get_json(METADATA_URL))
        if not prices:
            raise ValueError("models.dev contains no OpenCode metadata")
        return prices

    # -- cache ------------------------------------------------------------

    @property
    def _metadata_cache_path(self) -> str | None:
        if not self._cache_dir:
            return None
        return os.path.join(self._cache_dir, "models-dev-cache.json")

    def _load_cached_prices(self) -> dict[str, ModelPrice]:
        path = self._metadata_cache_path
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if time.time() - float(raw.get("updated_at", 0)) > 7 * METADATA_TTL:
                return {}
            prices: dict[str, ModelPrice] = {}
            for model_id, entry in (raw.get("prices") or {}).items():
                if isinstance(entry, dict):
                    prices[model_id] = ModelPrice(**entry)
            return prices
        except (OSError, ValueError, TypeError):
            return {}

    def _save_cached_prices(self, prices: dict[str, ModelPrice]) -> None:
        path = self._metadata_cache_path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "updated_at": time.time(),
                        "prices": {
                            model_id: {
                                "input": price.input,
                                "output": price.output,
                                "deprecated": price.deprecated,
                                "reasoning": price.reasoning,
                                "effort_values": price.effort_values,
                                "context_window": price.context_window,
                                "max_output": price.max_output,
                                "modalities": price.modalities,
                            }
                            for model_id, price in prices.items()
                        },
                    },
                    handle,
                )
            os.replace(tmp, path)
        except OSError:
            logger.debug("could not persist models.dev cache", exc_info=True)

    # -- refresh ----------------------------------------------------------

    def refresh(self, *, force: bool = False) -> None:
        """Refresh the live roster and pricing. Never raises."""
        with self._lock:
            if self._refreshing:
                return
            fresh_enough = (not force) and (time.time() - self._refreshed_at) < self._ttl
            if fresh_enough and self._live:
                return
            self._refreshing = True

        try:
            live: set[str] | None = None
            try:
                live = self._fetch_live()
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as error:
                self._last_error = f"models endpoint: {error}"

            prices: dict[str, ModelPrice] | None = None
            if not self._prices_ready:
                cached = self._load_cached_prices()
                if cached:
                    with self._lock:
                        self._prices = cached
                        self._prices_ready = True
            if not self._prices_ready or force:
                try:
                    prices = self._fetch_prices()
                except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as error:
                    self._last_error = f"models.dev: {error}"

            with self._lock:
                if live:
                    self._live = live
                    self._refreshed_at = time.time()
                    self._last_error = ""
                if prices:
                    self._prices = prices
                    self._prices_ready = True
        finally:
            with self._lock:
                self._refreshing = False
                snapshot = dict(self._prices)
                have_prices = self._prices_ready
            if have_prices and snapshot:
                self._save_cached_prices(snapshot)

    def warm(self, timeout: float = 4.0) -> None:
        """Block briefly for the first fetch so the picker is never empty."""
        with self._lock:
            if self._live:
                return
        thread = threading.Thread(target=self.refresh, kwargs={"force": True}, daemon=True)
        thread.start()
        thread.join(timeout)

    def maybe_refresh(self) -> bool:
        """Re-derive the snapshot in the background once it outlives ``ttl``.

        Without this the roster is frozen at whatever ``warm()`` saw at boot:
        ``refresh()`` is the only writer of ``_refreshed_at`` and nothing else
        calls it, so a sidecar left running for days never notices a model that
        was added upstream. Called on the read paths, it gives the documented
        ``OPENCODEZEN4HERMES_CATALOG_TTL`` its advertised meaning.

        Deliberately non-blocking and stale-while-revalidate: the caller answers
        from the current snapshot and the next caller sees the fresh one. A
        failed refresh is not an error here — ``refresh()`` never raises and
        records the reason in ``last_error`` for ``/healthz`` to report.

        Returns True when a refresh was scheduled by this call.
        """
        with self._lock:
            if self._refreshing or self._scheduled:
                return False
            if self._refreshed_at and (time.time() - self._refreshed_at) < self._ttl:
                return False
            self._scheduled = True
        threading.Thread(target=self._scheduled_refresh, daemon=True).start()
        return True

    def _scheduled_refresh(self) -> None:
        try:
            self.refresh(force=True)
        finally:
            with self._lock:
                self._scheduled = False

    # -- queries ----------------------------------------------------------

    def decision(self, model: str) -> Decision:
        if model in FREE_LANE_BLOCKLIST:
            return Decision(False, "local_blocklist", True)
        with self._lock:
            prices = dict(self._prices)
            ready = self._prices_ready
        verdict = decide(model, prices, ready)
        # A compile-time verified id stays vouched even when stale metadata
        # claims deprecation: it only dies when it leaves the live catalog.
        if not verdict.allowed and model in STATIC_FREE_MODELS:
            if verdict.source in {"metadata_deprecated", "metadata_model_missing", "metadata_pending"}:
                return Decision(True, "static_verified", False)
        return verdict

    def models(self) -> list[str]:
        """The roster to expose. Live roster minus blocklist, or the bootstrap."""
        with self._lock:
            live = set(self._live)
        if not live:
            return sorted(m for m in STATIC_FREE_MODELS if m not in FREE_LANE_BLOCKLIST)
        out: list[str] = []
        for model in live:
            if model in FREE_LANE_BLOCKLIST:
                continue
            if self._include_paid or self.decision(model).allowed:
                out.append(model)
        return sorted(out)

    def limits(self, model: str) -> dict[str, int] | None:
        with self._lock:
            price = self._prices.get(model)
        if price is None:
            return None
        for key, value in LOCAL_MODEL_OVERRIDES.get(model, {}).items():
            setattr(price, key, value)
        captured: dict[str, int] = {}
        if price.context_window is not None:
            captured["context_window"] = price.context_window
        if price.max_output is not None:
            captured["max_output"] = price.max_output
        return captured or None

    def modalities(self, model: str) -> list[str] | None:
        with self._lock:
            price = self._prices.get(model)
        return list(price.modalities) if price and price.modalities else None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            live = len(self._live)
            refreshed = self._refreshed_at
            error = self._last_error
            prices_ready = self._prices_ready
        age = None if not refreshed else round(time.time() - refreshed, 1)
        return {
            "status": "pending" if not refreshed else ("stale" if (age or 0) > 600 else "ready"),
            "total": live,
            "exposed": len(self.models()),
            "prices_ready": prices_ready,
            "age_seconds": age,
            "last_error": error or None,
        }
