"""Central registry of every external-contract version number (leaf, rank 20).

Before 0.21 the version stamps were scattered: ``model.SCHEMA_VERSION``,
``journal.JOURNAL_VERSION``, ``CAPABILITIES_VERSION``, and five hardcoded
``"1"``/``"1.0"`` literals in command envelopes.  ``test_json_version_stamps``
guaranteed every payload HAS a stamp but nothing guaranteed the stamp VALUES
agreed with the schemas that describe them.  This module is the one place a
version lives; two gates keep it honest:

* ``tests/test_contract_registry.py`` asserts every
  ``properties.schema_version.const`` in ``schemas/*.json`` equals its
  registry entry, and
* the same file's AST scan refuses a NEW hardcoded version literal in a
  payload builder (the existing consumers read the registry).

Protocol versions are deliberately SEPARATE keys — op-list, symbol-spec,
design-intent and contract-check are four different input protocols; folding
them into one number would force every consumer to migrate whenever any one
of them breaks.

Zero imports on purpose: ``model`` (rank 20) and ``cli`` (rank 90) both read
this module; any import here risks a cycle.
"""

from __future__ import annotations

from typing import Final

__all__ = ["SCHEMA_VERSIONS", "PROTOCOL_VERSIONS", "JOURNAL_VERSION"]

SCHEMA_VERSIONS: Final[dict[str, str]] = {
    # normalized model exports (model.SCHEMA_VERSION is an alias of these)
    "schematic": "2.1",
    "netlist": "2.0",
    # per-family payloads
    "doc": "1",
    "draw-result": "1.0",
    "capabilities": "1.0",
    "render": "1.0",
    "facts": "1.0",
    "proposals": "1.0",
    "bom-lock": "1",
    # the committed library baseline (akcli-library.baseline.json)
    "library-baseline": "1.0",
    # the generic envelope stamp (_stamp default / error envelope / small
    # hand-built payloads: library audit, release preflight, render summary)
    "envelope": "1",
}

PROTOCOL_VERSIONS: Final[dict[str, int]] = {
    "oplist": 2,
    "symbol-spec": 1,
    "design-intent": 1,
    "contract-check": 1,
    "sim": 1,
}

JOURNAL_VERSION: Final[str] = "1.0"
