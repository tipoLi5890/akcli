"""Shared test isolation.

The `jlc` family caches HTTP responses under the user's cache directory by
default; tests must neither READ a developer's warm cache (a mocked-offline
test would silently pass on cached data) nor WRITE junk into it.

Tests are also hermetic: the default opener factories of both networked
modules are replaced with one that raises, so any test that would touch the
real network fails loudly and attributably instead of flaking (or silently
passing against live data). Tests inject an ``opener=`` or monkeypatch
``_default_opener`` themselves — a per-test monkeypatch simply overrides the
guard.
"""

from __future__ import annotations

import pytest

from altium_kicad_cli.parts import easyeda as _easyeda
from altium_kicad_cli.parts import search as _search


@pytest.fixture(autouse=True)
def _no_jlc_cache(monkeypatch):
    monkeypatch.setenv("AKCLI_JLC_CACHE", "off")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _blocked():
        raise RuntimeError(
            "network disabled in tests: pass opener=... or monkeypatch "
            "_default_opener on parts.search / parts.easyeda")

    monkeypatch.setattr(_search, "_default_opener", _blocked)
    monkeypatch.setattr(_easyeda, "_default_opener", _blocked)
