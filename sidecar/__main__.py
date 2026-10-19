"""Allow ``python3 -m sidecar`` — the development entry point (BLUEPRINT §5)."""

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
