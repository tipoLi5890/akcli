"""The version registry (``akcli.contract``) is the ONE home of stamp values.

Two drift classes this file closes:

* **schema ↔ code drift** — a ``schemas/*.json`` declaring
  ``properties.schema_version.const`` must agree with the registry entry the
  code stamps from; before the registry, ``netlist.schema.json`` saying
  ``1.3`` and the code saying ``1.4`` would have shipped silently.
* **stray literals** — a NEW hardcoded ``"schema_version": "..."`` (or
  ``journal_version``) string literal in product code bypasses the registry;
  the AST scan refuses it (the registry module itself is the one exemption).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from akcli import contract, journal, model, ops, symbol_spec

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "akcli"

# schema file -> registry key (only files that pin a const today)
_SCHEMA_CONSTS = {
    "schematic.schema.json": "schematic",
    "netlist.schema.json": "netlist",
    "doc.schema.json": "doc",
    "draw-result.schema.json": "draw-result",
    "library-baseline.schema.json": "library-baseline",
}


def test_schema_consts_match_registry():
    for fname, key in _SCHEMA_CONSTS.items():
        doc = json.loads((ROOT / "schemas" / fname).read_text(encoding="utf-8"))
        prop = doc.get("properties", {}).get("schema_version", {})
        declared = prop.get("const") or prop.get("enum", [None])[0]
        assert declared == contract.SCHEMA_VERSIONS[key], (
            f"{fname} declares schema_version {declared!r} but the registry "
            f"says {contract.SCHEMA_VERSIONS[key]!r}"
        )


def test_consumers_read_the_registry():
    assert model.SCHEMA_VERSION == contract.SCHEMA_VERSIONS["schematic"]
    assert journal.JOURNAL_VERSION == contract.JOURNAL_VERSION
    assert ops.PROTOCOL_VERSION == contract.PROTOCOL_VERSIONS["oplist"]
    assert symbol_spec.PROTOCOL_VERSION == contract.PROTOCOL_VERSIONS["symbol-spec"]


def test_no_stray_version_literals():
    """No product dict literal carries a hardcoded version-string value."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "_vendor" in path.parts or path.name == "contract.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant)
                        and k.value in ("schema_version", "journal_version")
                        and isinstance(v, ast.Constant)
                        and isinstance(v.value, str)):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} "
                        f"{k.value}={v.value!r}")
    assert not offenders, (
        "hardcoded version literal(s) — stamp from akcli.contract instead: "
        f"{offenders}"
    )
