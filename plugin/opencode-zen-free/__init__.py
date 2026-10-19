"""Hermes model-provider plugin: the OpenCode Zen anonymous free lane.

Two jobs:

1. Register a ``ProviderProfile`` whose ``base_url`` points at the local
   ``opencodezen4hermes`` sidecar, so every Hermes/WebUI chat completion goes
   through the gate rewrite.
2. Expose the free roster. ``fetch_models`` asks the sidecar when it is up and
   falls back to the curated tuple otherwise, which is what the picker reads
   before any credential exists.

Why the sidecar is a separate process rather than a ``ProviderProfile`` feature:
the lane requires a rewritten request *body* (``stream:true`` plus a ``read``
tool). ``providers/base.py`` exposes no body-transform hook, so the rewrite has
to happen on the wire. See ``docs/DESIGN.md`` §2.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from providers import register_provider
from providers.base import ProviderProfile

#: Must match ``sidecar/config.py`` DEFAULT_PORT and the shipped config seed.
SIDECAR_PORT = int(os.environ.get("OPENCODEZEN4HERMES_PORT") or 8799)
SIDECAR_HOST = os.environ.get("OPENCODEZEN4HERMES_HOST") or "127.0.0.1"

#: Curated roster used when the sidecar has not answered yet. Sorted for a
#: stable picker order. Kept in sync with ``sidecar/catalog.py``.
FALLBACK_FREE_MODELS: tuple[str, ...] = (
    "beam-free",
    "big-pickle",
    "deepseek-v4.1-flash-free",
    "glm-5.3-flash-free",
    "longcat-2.5-preview-free",
    "mimo-v2.6-flash-free",
    "minimax-m3.1-flash-free",
    "nemotron-3.5-lightning-free",
    "nemotron-3.5-super-vl-free",
)


class OpenCodeZenFreeProfile(ProviderProfile):
    """Anonymous access to the Zen free tier via the local sidecar."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Ask the sidecar for the live roster.

        Returning ``None`` (rather than raising) on failure is what lets
        ``hermes_cli/models.py`` fall back to ``fallback_models``; a raise would
        surface as a picker error instead of a graceful degradation.
        """
        url = (base_url or self.base_url or "").rstrip("/") + "/models"
        if not url.startswith("http"):
            return None
        request = urllib.request.Request(
            url,
            headers={
                "authorization": f"Bearer {api_key or 'public'}",
                # The sidecar is local and does not inspect this, but a
                # well-formed UA keeps reverse-proxy logs readable.
                "user-agent": "hermes-cli/opencodezen4hermes",
                "accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError):
            return None
        items = payload.get("data") if isinstance(payload, dict) else None
        models = [
            item["id"]
            for item in (items or [])
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
        ]
        return models or None


profile = OpenCodeZenFreeProfile(
    name="opencode-zen-free",
    display_name="OpenCode Zen (free)",
    description=(
        "Free OpenCode Zen models over the anonymous lane, carried by the local "
        "opencodezen4hermes sidecar. No API key required."
    ),
    signup_url="https://opencode.ai",
    # Deliberately no env_vars: this provider must never look like it wants a
    # credential. `auth_type` stays api_key so `get_auth_status()` reports
    # `configured` from the shipped config seed, which is what admits the slug
    # into the WebUI picker (api/config.py `_has_provider_route`).
    env_vars=(),
    base_url=f"http://{SIDECAR_HOST}:{SIDECAR_PORT}/v1",
    auth_type="api_key",
    supports_model_listing=True,
    supports_health_check=False,
    fallback_models=FALLBACK_FREE_MODELS,
    default_aux_model="deepseek-v4.1-flash-free",
    default_max_tokens=8192,
)

register_provider(profile)
