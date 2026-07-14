"""``python -m akcli`` entry point (thin shim over :func:`cli.main`)."""

from __future__ import annotations

from .cli import main

raise SystemExit(main())
