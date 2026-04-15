"""Module entry point for ``python -m tcp_http``."""

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
