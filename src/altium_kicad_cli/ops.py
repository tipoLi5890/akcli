"""Op-list vocabulary, ``PROTOCOL_VERSION`` and a zero-dependency validator.

The validator mirrors ``schemas/ops.schema.json`` structurally without pulling in
``jsonschema`` at runtime (``jsonschema`` is a dev/test-only dependency). It rejects
free rotation angles, malformed wire vertex arrays, unknown ops and protocol
mismatches using the frozen ERROR codes from :mod:`errors`.

Coordinate/unit contract (SPEC §2.1): origin top-left, +Y down, units mils,
default 50-mil grid. Rotation is an enum ``{0,90,180,270}`` (``add_text`` may use
any ``angle``); mirror is ``{none,x,y}``; wire/port endpoints are ``[x,y]`` points
or a ``"REF.PIN"`` pin reference string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import AkcliError

PROTOCOL_VERSION: int = 1

# Core ops (mirror schemas/ops.schema.json) + documented sugar (place_gnd/place_vcc).
_CORE_OPS: frozenset[str] = frozenset(
    {
        "place_component",
        "set_component_transform",
        "set_component_parameters",
        "add_wire",
        "add_junction",
        "add_no_connect",
        "add_net_label",
        "place_power_port",
        "add_bus",
        "add_bus_entry",
        "add_text",
        "delete_component",
        "delete_object",
        "move_component",
    }
)
_SUGAR_OPS: frozenset[str] = frozenset({"place_gnd", "place_vcc"})
OP_NAMES: frozenset[str] = _CORE_OPS | _SUGAR_OPS  # 13 ops total

_VALID_ROTATIONS: frozenset[int] = frozenset({0, 90, 180, 270})
_VALID_MIRRORS: frozenset[str] = frozenset({"none", "x", "y"})
_VALID_SCOPES: frozenset[str] = frozenset({"local", "global", "hierarchical"})
_VALID_TARGETS: frozenset[str] = frozenset({"kicad", "altium"})

# Required fields per op (mirror the schema's `required` arrays).
_OP_REQUIRED: dict[str, list[str]] = {
    "place_component": ["lib_id", "designator", "x_mil", "y_mil"],
    "set_component_transform": ["designator"],
    "set_component_parameters": ["designator"],
    "add_wire": ["vertices"],
    "add_junction": ["at"],
    "add_no_connect": ["pin"],
    "add_net_label": ["name", "at"],
    "place_power_port": ["lib_id", "net_name", "at"],
    "add_bus": ["vertices"],
    "add_bus_entry": ["at"],
    "add_text": ["text", "at"],
    "place_gnd": ["at"],
    "place_vcc": ["at"],
    "delete_component": ["designator"],
    "delete_object": ["uuid"],
    "move_component": ["designator", "x_mil", "y_mil"],
}


@dataclass
class OpError:
    """A single structural problem found in an op-list.

    ``op_index`` is ``-1`` for document-level problems (protocol/target/ops shape).
    ``code`` is a frozen ERROR code from :mod:`errors`.
    """

    op_index: int
    op: str | None
    code: str
    message: str


class Op:
    """Typed accessor wrapper over a raw op dict."""

    __slots__ = ("raw",)

    def __init__(self, raw: dict) -> None:
        self.raw = raw

    @property
    def name(self) -> str | None:
        return self.raw.get("op")

    def __getitem__(self, key: str):
        return self.raw[key]

    def get(self, key: str, default=None):
        return self.raw.get(key, default)


def _is_point(v: object) -> bool:
    return (
        isinstance(v, (list, tuple))
        and len(v) == 2
        and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in v)
    )


def _is_endpoint(v: object) -> bool:
    # endpoint = [x,y] point OR a "REF.PIN" reference string
    if isinstance(v, str):
        return v.count(".") >= 1 and not v.startswith(".") and not v.endswith(".")
    return _is_point(v)


def _check_rotation(idx: int, name: str, op: dict, key: str, out: list[OpError]) -> None:
    if key in op and op[key] not in _VALID_ROTATIONS:
        out.append(OpError(idx, name, "BAD_ANGLE", f"{key} {op[key]!r} not in {{0,90,180,270}}"))


def _validate_op(idx: int, op: object) -> list[OpError]:
    out: list[OpError] = []
    if not isinstance(op, dict):
        out.append(OpError(idx, None, "OP_UNSUPPORTED", "op must be a JSON object"))
        return out
    name = op.get("op")
    if name not in OP_NAMES:
        out.append(OpError(idx, name if isinstance(name, str) else None,
                           "OP_UNSUPPORTED", f"unknown op {name!r}"))
        return out

    for req in _OP_REQUIRED.get(name, []):
        if req not in op:
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name} missing required field {req!r}"))

    # rotation/orientation enums (add_text uses free `angle`, not validated here)
    _check_rotation(idx, name, op, "rotation", out)
    _check_rotation(idx, name, op, "orientation", out)

    if "mirror" in op and op["mirror"] not in _VALID_MIRRORS:
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           f"mirror {op['mirror']!r} not in {{none,x,y}}"))

    if name == "add_net_label" and "scope" in op and op["scope"] not in _VALID_SCOPES:
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           f"scope {op['scope']!r} not in {{local,global,hierarchical}}"))

    if name in ("add_wire", "add_bus"):
        verts = op.get("vertices")
        if not isinstance(verts, list) or len(verts) < 2:
            out.append(OpError(idx, name, "NON_ORTHOGONAL_WIRE",
                               "vertices must be an array of >= 2 points"))
        else:
            endpoint_ok = _is_endpoint if name == "add_wire" else _is_point
            for v in verts:
                if not endpoint_ok(v):
                    out.append(OpError(idx, name, "NON_ORTHOGONAL_WIRE",
                                       f"malformed vertex {v!r}"))
                    break
    return out


def validate_oplist(doc: dict) -> list[OpError]:
    """Structurally validate an op-list document; return all problems found.

    Never raises for structural issues — returns a complete ``OpError`` list so a
    caller can report everything at once. Document-level issues use ``op_index=-1``.
    """
    errs: list[OpError] = []
    if not isinstance(doc, dict):
        return [OpError(-1, None, "OP_UNSUPPORTED", "op-list must be a JSON object")]

    pv = doc.get("protocol_version")
    if pv != PROTOCOL_VERSION:
        errs.append(OpError(-1, None, "PROTOCOL_MISMATCH",
                            f"protocol_version {pv!r} != {PROTOCOL_VERSION}"))

    tf = doc.get("target_format")
    if tf not in _VALID_TARGETS:
        errs.append(OpError(-1, None, "OP_UNSUPPORTED",
                            f"target_format {tf!r} not in {{kicad,altium}}"))

    ops = doc.get("ops")
    if not isinstance(ops, list):
        errs.append(OpError(-1, None, "OP_UNSUPPORTED", "ops must be an array"))
        return errs

    for i, op in enumerate(ops):
        errs.extend(_validate_op(i, op))
    return errs


def load_oplist(path: str | Path) -> dict:
    """Load and JSON-parse an op-list file. Malformed JSON -> ``OP_UNSUPPORTED``."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AkcliError("OP_UNSUPPORTED", f"invalid op-list JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise AkcliError("OP_UNSUPPORTED", "op-list root must be a JSON object")
    return doc


def _schemas_dir() -> Path:
    # repo_root/schemas (this file: repo_root/src/altium_kicad_cli/ops.py)
    return Path(__file__).resolve().parents[2] / "schemas"


def load_capabilities() -> dict:
    """Load ``schemas/ops.capabilities.json`` (per-executor support matrix)."""
    cap = _schemas_dir() / "ops.capabilities.json"
    return json.loads(cap.read_text(encoding="utf-8"))
