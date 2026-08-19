"""Allow running as ``python -m roundtable``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
