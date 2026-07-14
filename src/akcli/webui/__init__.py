"""Local web dashboard for akcli (``akcli view``).

One server (``server.py``) hosts both pages — ``/calc`` (calculator bench)
and ``/live`` (schematic watch timeline) — binds 127.0.0.1 only, has zero
third-party dependencies, and serves its single-page HTML from package data.
Born as standalone scripts under ``tools/``, then two separate servers; now
a single process so no port juggling is needed.
"""

from __future__ import annotations

from importlib.resources import files


def page(name: str) -> bytes:
    """A packaged dashboard page (``calc.html`` / ``live.html``) as bytes."""
    return (files(__package__) / name).read_bytes()
