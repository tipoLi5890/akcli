"""`akcli schema` — print a packaged JSON Schema, byte-for-byte.

Fifteen schemas ship inside the wheel (mirrored from the repo's canonical
``schemas/``, byte-identity CI-gated), but until this command none of them was
reachable through the CLI: an orchestrator that ``pip install``-ed akcli had no
way to obtain ``ops.schema.json`` for client-side validation without digging
into site-packages. The output IS the schema file's exact text — already JSON,
already carrying its own version field — so ``--json`` changes nothing.
"""

from __future__ import annotations

import argparse

from ..errors import EXIT
from ._shared import _emit

_SUFFIXES = (".schema.json", ".json")


def _inventory() -> list[str]:
    """Packaged schema filenames (e.g. ``ops.schema.json``), sorted."""
    from importlib import resources
    root = resources.files("akcli.schemas")
    return sorted(r.name for r in root.iterdir() if r.name.endswith(".json"))


def _short(filename: str) -> str:
    """``ops.schema.json`` -> ``ops``; ``ops.capabilities.json`` -> ``ops.capabilities``."""
    for suf in _SUFFIXES:
        if filename.endswith(suf):
            return filename[: -len(suf)]
    return filename


def _resolve(name: str) -> str | None:
    files = _inventory()
    if name in files:
        return name
    for f in files:
        if _short(f) == name:
            return f
    return None


class _SchemaNameChoices:
    """Lazy ``choices``: argparse-native validation (unknown name -> USAGE
    envelope with the full list) without touching package resources until a
    ``schema`` invocation actually parses a name."""

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and _resolve(name) is not None

    def __iter__(self):
        return iter(sorted(_short(f) for f in _inventory()))


def _cmd_schema(args: argparse.Namespace) -> int:
    from ..ops import _schema_text
    filename = _resolve(args.name)
    assert filename is not None  # argparse choices already validated
    # byte-for-byte: the schema file IS the payload (strip nothing, add nothing
    # beyond the trailing newline _emit guarantees)
    _emit(_schema_text(filename).rstrip("\n"))
    return EXIT["OK"]


def register(sub, common) -> None:
    p = sub.add_parser(
        "schema", parents=[common],
        help="print a packaged JSON Schema (ops, findings, netlist, ...) "
             "byte-for-byte for client-side validation")
    p.add_argument("name", metavar="NAME", choices=_SchemaNameChoices(),
                   help="schema name — short form (`ops`, `findings`) or the "
                        "full filename (`ops.schema.json`); unknown names "
                        "fail at parse time with the full list")
    p.set_defaults(handler=_cmd_schema)
