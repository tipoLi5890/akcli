#!/usr/bin/env python3
"""The ONE schema entry point: generate ``schemas/*.json``, mirror, verify.

``akcli.opspec`` is the op vocabulary; ``schemas/ops.schema.json`` and
``schemas/ops.capabilities.json`` are GENERATED views of it, and
``src/akcli/schemas/`` is the packaged mirror that ships in wheels. Those are
two steps that must never be run apart — a regenerated canonical schema with a
stale mirror leaves the CLI serving yesterday's contract out of the wheel — so
they live behind one command:

    python3 tools/schemas.py build   # regenerate from opspec + refresh mirror
    python3 tools/schemas.py check   # exit 1 on generator drift OR mirror drift

``tools/gen_ops_schema.py`` and ``tools/sync_schemas.py`` remain as thin
compatibility entry points onto the two halves.

What lives WHERE, deliberately:

* ``opspec`` carries the vocabulary — ops, fields, required-ness, enumerated
  kinds, prose, cross-field constraints, the capability matrix.
* this file carries the ENCODING — the op-list envelope (protocol_version /
  target_format / sheets / groups), the ``$defs`` primitives, and the
  ``kind -> JSON-schema fragment`` table that compiles a :class:`Field` into
  the shape it promises.

The mirror half is a two-way SET comparison, not a one-way copy:
``akcli.ops.schema_io._schema_text`` loads the packaged mirror FIRST, so a
mirror file whose canonical original was deleted or renamed is not dead weight
— the wheel would keep serving that retired schema forever. Orphaned mirrors
are therefore deleted (or reported under ``--check``).
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"                      # canonical
MIRROR = ROOT / "src" / "akcli" / "schemas"        # packaged in wheels
sys.path.insert(0, str(ROOT / "src"))

from akcli import contract, opspec  # noqa: E402  (needs the sys.path line above)
from akcli.opspec import Field, OpSpec  # noqa: E402


# --------------------------------------------------------------------------- #
# kind -> JSON-schema fragment
# --------------------------------------------------------------------------- #
# One row per ``Field.kind``. ``vertices`` is the only parameterised kind: its
# element shape comes from ``Field.item_kind`` (add_wire accepts pin refs,
# add_bus is points-only).
_KIND_SCHEMA: dict[str, dict] = {
    "str": {"type": "string"},
    "num": {"type": "number"},
    "bool": {"type": "boolean"},
    "dict": {"type": "object"},
    "posint": {"type": "integer", "minimum": 1},
    "nonneg_int": {"type": "integer", "minimum": 0},
    "array_count": {"type": "integer", "minimum": 1, "maximum": 200},
    "point": {"$ref": "#/$defs/point"},
    "endpoint": {"$ref": "#/$defs/endpoint"},
    "anchor": {"$ref": "#/$defs/anchor"},
    "anchor_ref": {"$ref": "#/$defs/anchorref"},
    "rotation": {"$ref": "#/$defs/rotation"},
    "mirror": {"$ref": "#/$defs/mirror"},
    "scope": {"$ref": "#/$defs/scope"},
    "strpair": {"$ref": "#/$defs/strpair"},
    "strtriple": {"$ref": "#/$defs/strtriple"},
    "fill": {"enum": opspec.enum("fill")},
    "route_style": {"enum": opspec.enum("route_style")},
    "array_direction": {"enum": opspec.enum("array_direction")},
    "strlist": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    "match": {
        "type": "object",
        "properties": {
            "kind": {"enum": opspec.enum("match_kind")},
            "name": {"type": "string"},
            "at": {"$ref": "#/$defs/point"},
        },
        "required": ["kind"],
        "additionalProperties": False,
    },
    "sheetpins": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "type": {"enum": opspec.enum("sheet_pin_type")},
                "side": {"enum": opspec.enum("sheet_pin_side")},
                "offset_mil": {"type": "number"},
            },
            "required": ["name", "type", "side", "offset_mil"],
            "additionalProperties": False,
        },
    },
}

# Composite kinds read better with the prose ahead of their nested structure,
# so their ``description`` goes right after ``type`` instead of last.
_DOC_AFTER_TYPE = frozenset({"match", "sheetpins"})

# --------------------------------------------------------------------------- #
# static envelope
# --------------------------------------------------------------------------- #
_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_ID_BASE = "https://github.com/tipoLi5890/akcli/schemas"

# The op-list protocol major lives in ONE place (``akcli.contract``); both
# generated files stamp it from there, never as a literal.
_OPLIST_PROTOCOL = contract.PROTOCOL_VERSIONS["oplist"]

_ENVELOPE_PROPERTIES: dict[str, dict] = {
    "protocol_version": {"const": _OPLIST_PROTOCOL},
    "target_format": {"enum": opspec.enum("target_format")},
    "target_file": {"type": "string"},
    "run_id": {
        "type": "string",
        "description": "Optional transaction id (multi-sheet apply): recorded "
                       "in the workspace journal and named by `akcli undo "
                       "--run`. Auto-generated when absent.",
    },
    "sheets": {
        "type": "object",
        "description": "Multi-sheet routing map (0.20): declares the child "
                       ".kicad_sch files a 'sheet'-tagged op may target. Keys "
                       "are free names referenced by per-op 'sheet' tags; each "
                       "'file' resolves relative to the draw target's "
                       "directory and must already exist (`akcli new` it "
                       "first). One op-list can then edit the root and its "
                       "children in ONE atomic transaction (all files verify "
                       "before the first write; a partial commit rolls back).",
        "additionalProperties": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "The child .kicad_sch, relative to the "
                                   "draw target's directory.",
                },
            },
            "required": ["file"],
            "additionalProperties": False,
        },
    },
    "groups": {
        "type": "object",
        "description": "Functional groups (modules). Any op may carry an "
                       "optional 'group': '<NAME>' tag referencing an entry "
                       "here; the op's [x, y] coordinates are then GROUP-LOCAL "
                       "and akcli translates them by the group's origin before "
                       "validation (absolute = local + origin). Pin anchors "
                       "('REF.PIN', 'mid()') are position-independent and "
                       "never translate. Membership is persisted on placed "
                       "symbols as a hidden 'Group' property.",
        "additionalProperties": {
            "type": "object",
            "properties": {
                "origin": {
                    "$ref": "#/$defs/point",
                    "description": "[x_mil, y_mil] world origin of the group's "
                                   "local coordinate frame.",
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable module title (used by group "
                                   "frames).",
                },
                "frame": {
                    "type": "boolean",
                    "description": "Hint that this group wants a visual border "
                                   "frame (see `akcli groups frame`).",
                },
            },
            "required": ["origin"],
            "additionalProperties": False,
        },
    },
    "ops": {"type": "array", "items": {"$ref": "#/$defs/op"}},
}

# Shared value grammars the field kinds point at. Order is the emitted
# ``$defs`` order (``op`` is appended last, generated).
_PRIMITIVE_DEFS: dict[str, dict] = {
    "point": {"type": "array", "items": {"type": "number"},
              "minItems": 2, "maxItems": 2},
    "pinref": {"type": "string", "pattern": r"^[^.]+\.[^.]+$"},
    "midref": {
        "type": "string",
        "description": "mid(REF.PIN,REF.PIN): midpoint of two axis-aligned "
                       "pins, snapped to the 50-mil grid along the wire axis.",
        "pattern": r"^mid\(\s*[^,()\s]+\.[^,()\s]+\s*,\s*[^,()\s]+\.[^,()\s]+"
                   r"\s*\)$",
    },
    "endpoint": {"oneOf": [{"$ref": "#/$defs/point"},
                           {"$ref": "#/$defs/pinref"}]},
    "anchor": {"oneOf": [{"$ref": "#/$defs/point"},
                         {"$ref": "#/$defs/pinref"},
                         {"$ref": "#/$defs/midref"}]},
    "rotation": {"enum": opspec.enum("rotation")},
    "mirror": {"enum": opspec.enum("mirror")},
    "scope": {"enum": opspec.enum("scope")},
    "strpair": {"type": "array", "items": {"type": "string"},
                "minItems": 2, "maxItems": 2},
    "strtriple": {"type": "array", "items": {"type": "string"},
                  "minItems": 3, "maxItems": 3},
    "anchorref": {
        "type": "string",
        "description": "Relative-placement anchor: 'REF.PIN' (that pin's world "
                       "coordinate) or bare 'REF' (the component origin). "
                       "Resolved by the writer against the live document, so "
                       "the anchor may be placed earlier in the SAME op-list. "
                       "offset_mil is a world-frame [dx, dy] from the anchor; "
                       "the result grid-snaps. Exactly one of x_mil/y_mil or "
                       "anchor per op.",
        "pattern": r"^[^.()\s]+(\.[^.()\s]+)?$",
    },
}

_CAPS_NOTE = (
    "Altium is IMPORT-ONLY: there is no Altium write executor and none is "
    "planned. The 'altium' column is an informational record of which ops an "
    "Altium writer would have to implement; plan/draw always build "
    "target_format 'kicad'. Do not select an Altium write target."
)

# Row order of the capability matrix — presentation only (the schema branch
# order is the vocabulary's). Membership is asserted against opspec below, so
# a new op cannot be forgotten here, only mis-sorted.
_CAPS_ORDER: tuple[str, ...] = (
    "place_component", "set_component_transform", "set_component_parameters",
    "add_wire", "route_net", "add_junction", "add_no_connect", "add_net_label",
    "place_power_port", "add_bus", "add_bus_entry", "add_text",
    "add_rectangle", "add_text_box", "set_title_block", "add_sheet",
    "place_gnd", "place_vcc", "delete_component", "delete_object",
    "move_component", "rename_net",
)


# --------------------------------------------------------------------------- #
# compilation
# --------------------------------------------------------------------------- #
def field_schema(field: Field) -> dict:
    """The JSON-schema fragment one :class:`~akcli.opspec.Field` promises."""
    if field.kind == "vertices":
        frag: dict = {"type": "array",
                      "items": {"$ref": f"#/$defs/{field.item_kind}"},
                      "minItems": 2}
    else:
        try:
            frag = copy.deepcopy(_KIND_SCHEMA[field.kind])
        except KeyError:
            raise SystemExit(
                f"opspec field kind {field.kind!r} has no schema fragment — "
                f"add a _KIND_SCHEMA row") from None
    if field.doc:
        if field.kind in _DOC_AFTER_TYPE:
            rest = {k: v for k, v in frag.items() if k != "type"}
            frag = {"type": frag["type"], "description": field.doc, **rest}
        else:
            frag["description"] = field.doc
    return frag


def op_branch(spec: OpSpec) -> dict:
    """One ``$defs.op.oneOf`` branch: the closed shape of a single op."""
    properties: dict[str, dict] = {"op": {"const": spec.name}}
    for universal in opspec.UNIVERSAL_FIELDS:
        properties[universal.name] = field_schema(universal)
    for field in spec.fields:
        properties[field.name] = field_schema(field)
    branch: dict = {
        "additionalProperties": False,
        "properties": properties,
        "required": ["op", *spec.required_names()],
    }
    for constraint in spec.constraints:
        if not constraint.schema_enforced:
            continue                     # prose only; the validator enforces it
        if constraint.kind == "exactly_one":
            branch["oneOf"] = [{"required": list(group)}
                               for group in constraint.groups]
        elif constraint.kind == "at_least_one_field":
            branch["minProperties"] = 2
        else:
            raise SystemExit(f"{spec.name}: schema-enforced constraint kind "
                             f"{constraint.kind!r} has no encoding")
    if spec.doc:
        branch["description"] = spec.doc
    return branch


def build_schema() -> dict:
    """``schemas/ops.schema.json`` as a dict (insertion order = file order)."""
    defs = dict(_PRIMITIVE_DEFS)
    defs["op"] = {
        "type": "object",
        "required": ["op"],
        "oneOf": [op_branch(spec) for spec in opspec.ALL],
    }
    return {
        "$schema": _SCHEMA_DRAFT,
        "$id": f"{_ID_BASE}/ops.schema.json",
        "title": "AkcliOpList",
        "type": "object",
        "required": ["protocol_version", "target_format", "ops"],
        "properties": copy.deepcopy(_ENVELOPE_PROPERTIES),
        "$defs": defs,
    }


def build_capabilities() -> dict:
    """``schemas/ops.capabilities.json`` as a dict (per-executor matrix)."""
    executable = {spec.name for spec in opspec.CORE}
    if set(_CAPS_ORDER) != executable:
        raise SystemExit(
            "_CAPS_ORDER drifted from opspec.CORE: "
            f"{sorted(executable ^ set(_CAPS_ORDER))}")
    rows: dict[str, dict] = {}
    for name in _CAPS_ORDER:
        spec = opspec.by_name(name)
        row: dict = {"kicad": spec.kicad, "altium": spec.altium}
        if spec.caps_note is not None:
            row["notes"] = spec.caps_note
        rows[name] = row
    return {
        "$schema": _SCHEMA_DRAFT,
        "$id": f"{_ID_BASE}/ops.capabilities.json",
        "title": "AkcliOpCapabilities",
        "protocol_version": _OPLIST_PROTOCOL,
        "description": "Per-executor support matrix. true = supported; "
                       "false = ERROR: OP_UNSUPPORTED.",
        "executors": ["kicad", "altium"],
        "altium_write_wired": False,
        "altium_write_note": _CAPS_NOTE,
        "ops": rows,
    }


def render(doc: dict) -> str:
    """The canonical on-disk form (2-space indent, unescaped non-ASCII, LF)."""
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _targets() -> dict[str, str]:
    """``{schema basename: canonical text}`` for both generated files."""
    return {"ops.schema.json": render(build_schema()),
            "ops.capabilities.json": render(build_capabilities())}


# --------------------------------------------------------------------------- #
# the two halves
# --------------------------------------------------------------------------- #
def generate(*, check: bool) -> int:
    """Render both schemas from opspec into ``schemas/`` (or report drift)."""
    drift: list[str] = []
    for name, want in _targets().items():
        path = SCHEMA_DIR / name
        have = path.read_text(encoding="utf-8") if path.exists() else None
        if have == want:
            continue
        drift.append(name)
        if not check:
            with path.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(want)
            print(f"wrote schemas/{name}")
    if check:
        if drift:
            print("schema drift (regenerate with `python3 tools/schemas.py "
                  "build`): " + ", ".join(drift))
            return 1
        print("schemas match opspec (2 file(s))")
    elif not drift:
        print("schemas already up to date (2 file(s))")
    return 0


def schema_names() -> list[str]:
    """Every schema basename on EITHER side (so orphan mirrors are seen)."""
    return sorted({p.name for p in SCHEMA_DIR.glob("*.json")}
                  | {p.name for p in MIRROR.glob("*.json")})


def sync(*, check: bool) -> int:
    """Copy ``schemas/*.json`` into the packaged mirror, pruning orphans."""
    drift: list[str] = []
    for name in schema_names():
        src, dst = SCHEMA_DIR / name, MIRROR / name
        if not src.exists():                       # orphaned mirror
            drift.append(f"{name} (orphan: no schemas/{name})")
            if not check:
                dst.unlink()
                print(f"removed orphan {name}")
            continue
        want = src.read_bytes()
        have = dst.read_bytes() if dst.exists() else None
        if have == want:
            continue
        drift.append(name)
        if not check:
            dst.write_bytes(want)
            print(f"synced {name}")
    if check and drift:
        print("mirror drift: " + ", ".join(drift))
        return 1
    if not drift:
        print(f"mirrors up to date ({len(schema_names())} file(s))")
    return 0


_USAGE = ("usage: python3 tools/schemas.py {build|check} "
          "(or {generate|sync} [--check] for one half)")


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    rest = argv[1:]
    check = "--check" in rest
    if cmd == "build":
        return generate(check=False) or sync(check=False)
    if cmd == "check":
        # run BOTH halves even if the first fails: one report, every drift
        return max(generate(check=True), sync(check=True))
    if cmd == "generate":
        return generate(check=check)
    if cmd == "sync":
        return sync(check=check)
    print(_USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
