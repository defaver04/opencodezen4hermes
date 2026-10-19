"""Runtime configuration for the sidecar.

Everything is overridable by env var so the arm64 pack can be reconfigured
without editing files, and by CLI flag so tests can point at a fake upstream.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

from .gate import DEFAULT_LANE_BASE

#: Port the Hermes provider profile points at. Deliberately high and fixed so the
#: shipped config seed can hard-code it.
DEFAULT_PORT = 8799
DEFAULT_HOST = "127.0.0.1"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw not in {"0", "false", "no", "off", ""}


@dataclass(slots=True)
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    lane_base: str = DEFAULT_LANE_BASE
    #: Overrides the generated wire UA. Only the ``opencode/<semver>`` prefix is
    #: validated upstream, but a user on a machine with a different installed CLI
    #: may want to track it.
    user_agent: str | None = None
    #: Seconds of socket inactivity before the upstream call is abandoned.
    upstream_timeout: float = 300.0
    #: Seconds to wait on the TCP connect alone.
    connect_timeout: float = 15.0
    #: Cache TTL for the model catalog, in seconds.
    catalog_ttl: float = 300.0
    #: Log every proxied request line. Off by default; the WebUI already logs.
    verbose: bool = False
    #: Include paid models in /v1/models in addition to the free roster.
    include_paid: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            host=_env("OPENCODEZEN4HERMES_HOST", DEFAULT_HOST),
            port=_env_int("OPENCODEZEN4HERMES_PORT", DEFAULT_PORT),
            lane_base=_env("OPENCODEZEN4HERMES_LANE_BASE", DEFAULT_LANE_BASE),
            user_agent=os.environ.get("OPENCODEZEN4HERMES_USER_AGENT") or None,
            upstream_timeout=float(_env("OPENCODEZEN4HERMES_TIMEOUT", "300")),
            connect_timeout=float(_env("OPENCODEZEN4HERMES_CONNECT_TIMEOUT", "15")),
            catalog_ttl=float(_env("OPENCODEZEN4HERMES_CATALOG_TTL", "300")),
            verbose=_env_bool("OPENCODEZEN4HERMES_VERBOSE", False),
            include_paid=_env_bool("OPENCODEZEN4HERMES_INCLUDE_PAID", False),
        )

    @property
    def models_url(self) -> str:
        return self.lane_base.rstrip("/") + "/models"

    @property
    def chat_url(self) -> str:
        return self.lane_base.rstrip("/") + "/chat/completions"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opencodezen4hermes-sidecar",
        description=(
            "Local OpenAI-compatible proxy that carries the OpenCode Zen anonymous "
            "free lane into Hermes."
        ),
    )
    parser.add_argument("--host", default=None, help=f"bind host (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=None, help=f"bind port (default {DEFAULT_PORT})")
    parser.add_argument("--lane-base", default=None, help="override the upstream lane base URL")
    parser.add_argument("--catalog-ttl", type=float, default=None, help="model catalog cache TTL (s)")
    parser.add_argument("--include-paid", action="store_true", default=None, help="also list paid models")
    parser.add_argument("--verbose", action="store_true", default=None, help="log every proxied request")
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="print the effective config as JSON and exit (used by the doctor script)",
    )
    return parser


def resolve(argv: list[str] | None = None) -> tuple[Config, argparse.Namespace]:
    """Merge env defaults with CLI overrides. CLI wins."""
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.from_env()
    if args.host is not None:
        config.host = args.host
    if args.port is not None:
        config.port = args.port
    if args.lane_base is not None:
        config.lane_base = args.lane_base
    if args.catalog_ttl is not None:
        config.catalog_ttl = args.catalog_ttl
    if args.include_paid is not None:
        config.include_paid = args.include_paid
    if args.verbose is not None:
        config.verbose = args.verbose
    return config, args
