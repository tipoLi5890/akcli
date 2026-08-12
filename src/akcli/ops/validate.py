"""The zero-dependency op-list validator (+ loading and the prepare pipeline).

Mirrors ``schemas/ops.schema.json`` structurally without pulling in
``jsonschema`` at runtime (``jsonschema`` is a dev/test-only dependency) —
both are views of :mod:`akcli.opspec`, so the mirror cannot drift. It rejects
free rotation angles, malformed wire vertex arrays, unknown ops and protocol
mismatches using the frozen ERROR codes from :mod:`akcli.errors`, and
additionally lints: per-field TYPES, unknown fields (with a difflib
did-you-mean, or an explicit "renamed in protocol 2" line for the fields
:data:`_RENAMED_IN_V2` moved; keys starting with ``_`` are ignored as
annotations), and duplicate ``(designator, unit)`` placements across the
document.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import Path

from ..classify import refdes_prefix as _classify_refdes_prefix
from ..errors import AkcliError
from .base import (
    PROTOCOL_VERSION,
    OpError,
    _in,
    _is_anchor,
    _is_anchor_ref,
    _is_endpoint,
    _is_point,
)
from .groups import _GROUP_META_FIELDS, is_resolved, resolve_groups
from .lints import (
    _ANNOTATION_KEY_TAGS,
    _duplicate_placements,
    _duplicate_power_ports,
)
from .macros import expand_macros
from .vocab import (
    _MACRO_DESIGNATOR_ROLES,
    _MATCH_KINDS,
    _OP_FIELDS,
    _OP_REQUIRED,
    _ROLE_PREFIXES,
    _SHEET_PIN_SIDES,
    _SHEET_PIN_TYPES,
    _VALID_FILLS,
    _VALID_MIRRORS,
    _VALID_ROTATIONS,
    _VALID_ROUTE_STYLES,
    _VALID_SCOPES,
    _VALID_TARGETS,
    MACRO_OPS,
    MACRO_OPTIONAL,
    MACRO_REQUIRED,
    OP_NAMES,
)

# The ONE refdes-prefix implementation lives in classify (strips a
# synthesized ``$`` — irrelevant for op-list-authored designators, identical
# everywhere else); alias kept for this module's macro-role checks.
_refdes_prefix = _classify_refdes_prefix


def _macro_role_slots(name: str, op: dict) -> tuple[str, tuple[str, ...]] | None:
    """``(field, roles)`` for a macro's designator slots, or ``None`` if unknown.

    ``place_divider``/``place_decoupling``/``place_pullup`` expose ``lib_id``:
    an override that is still the same part class (``Device:R_Small``) keeps
    the check, an override to a different class hands the role question back
    to the caller and disables it.
    """
    entry = _MACRO_DESIGNATOR_ROLES.get(name)
    if entry is None:
        return None
    field, roles = entry
    lib = op.get("lib_id")
    if isinstance(lib, str) and lib:
        part = lib.split(":")[-1]
        if _refdes_prefix(part) not in _ROLE_PREFIXES[roles[0]]:
            return None
    return field, roles


def _check_macro_designator_roles(idx: int, name: str, op: dict,
                                  out: list[OpError]) -> None:
    """Reject a designator whose prefix contradicts its positional slot's part."""
    entry = _macro_role_slots(name, op)
    if entry is None:
        return
    field, roles = entry
    raw = op.get(field)
    if raw is None:
        return
    values = [raw] if field == "designator" else raw
    if not isinstance(values, list):
        return                      # shape errors belong to the expander
    # the slot order the user should see is the template's own default
    # designators (["Y1", "C1", "C2"] -> "Y, C, C") — one canonical spelling
    default = MACRO_OPTIONAL.get(name, {}).get(field)
    defaults = [default] if field == "designator" else default
    order = ", ".join(_refdes_prefix(d) for d in defaults
                      if isinstance(d, str)) if isinstance(defaults, list) else ""
    for slot, (value, role) in enumerate(zip(values, roles)):
        if not isinstance(value, str) or not value:
            continue
        want = _ROLE_PREFIXES[role]
        got = _refdes_prefix(value)
        if got in want:
            continue
        where = field if field == "designator" else f"{field}[{slot}]"
        out.append(OpError(
            idx, name, "OP_UNSUPPORTED",
            f"{name}.{where} {value!r}: slot {slot} is the {role} — expected a "
            f"designator prefixed {'/'.join(sorted(want))}, got {got or value!r}"
            f" (slot order is [{order}]; see `akcli ops template {name}`)"))


def _check_rotation(idx: int, name: str, op: dict, key: str, out: list[OpError]) -> None:
    if key in op and not _in(op[key], _VALID_ROTATIONS):
        out.append(OpError(idx, name, "BAD_ANGLE", f"{key} {op[key]!r} not in {{0,90,180,270}}"))


# Generic kind checkers: predicate + human description for the error message.
_KIND_CHECKS: dict[str, tuple] = {
    "str": (lambda v: isinstance(v, str), "a string"),
    "num": (lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "a number"),
    "posint": (lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 1,
               "an integer >= 1"),
    "bool": (lambda v: isinstance(v, bool), "true or false"),
    "point": (_is_point, "an [x, y] point"),
    "endpoint": (_is_endpoint, 'an [x, y] point or "REF.PIN"'),
    "anchor": (_is_anchor,
               'an [x, y] point, "REF.PIN" or "mid(REF.PIN,REF.PIN)"'),
    "dict": (lambda v: isinstance(v, dict), "an object"),
    "fill": (lambda v: _in(v, _VALID_FILLS),
             'one of "none", "outline", "background"'),
    "anchor_ref": (_is_anchor_ref, '"REF" or "REF.PIN"'),
    "route_style": (lambda v: _in(v, _VALID_ROUTE_STYLES),
                    'one of "auto", "hv", "vh", "z"'),
}


# Fields renamed by protocol 2, PER OP: a v1 document keeps the old spelling
# and would otherwise get a bare "unknown field" (difflib has no chance on
# ``from`` -> ``old_name``). Scoped by op on purpose — ``route_net`` and
# ``connect_and_label`` still take ``from``/``to`` (pin refs), and
# ``connect_and_label`` still takes ``orientation``; only the ops whose field
# actually moved may claim the rename.
_RENAMED_IN_V2: dict[str, dict[str, str]] = {
    "add_net_label": {"orientation": "rotation"},
    "set_component_parameters": {
        "reference": "new_designator",
        "instance_references": "instance_designators",
    },
    "rename_net": {"from": "old_name", "to": "new_name"},
}


def _unknown_field(idx: int, name: str, field: str, known,
                   out: list[OpError]) -> None:
    renamed = _RENAMED_IN_V2.get(name, {}).get(field)
    if renamed is not None:
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           f"{name}: field {field!r} was renamed to "
                           f"{renamed!r} in protocol {PROTOCOL_VERSION}"))
        return
    known_list = list(known)
    hint = difflib.get_close_matches(field, known_list, n=1)
    suggestion = f" (did you mean {hint[0]!r}?)" if hint else ""
    if not suggestion and field == "at" and "x_mil" in known_list:
        # the most common coordinate-field mixup: at:[x,y] ops vs x_mil/y_mil
        suggestion = f" ({name} takes \"x_mil\"/\"y_mil\", not \"at\")"
    out.append(OpError(idx, name, "OP_UNSUPPORTED",
                       f"{name}: unknown field {field!r}{suggestion}"))


def _check_match(idx: int, name: str, m: object, out: list[OpError]) -> None:
    """Validate a delete_object ``match`` selector {kind, name?, at?}."""
    if not isinstance(m, dict):
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           f"{name}.match must be an object {{kind, name?, at?}}"))
        return
    kind = m.get("kind")
    if not isinstance(kind, str) or kind not in _MATCH_KINDS:
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           f"{name}.match.kind must be one of "
                           f"{{{', '.join(sorted(_MATCH_KINDS))}}}"))
    if "name" in m and not isinstance(m["name"], str):
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           f"{name}.match.name must be a string"))
    if "at" in m and not _is_point(m["at"]):
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           f"{name}.match.at must be an [x, y] point"))
    for k in m:
        if k not in ("kind", "name", "at") and not k.startswith("_"):
            hint = difflib.get_close_matches(k, ["kind", "name", "at"], n=1)
            suggestion = f" (did you mean {hint[0]!r}?)" if hint else ""
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name}: unknown field 'match.{k}'{suggestion}"))


def _check_sheet_pins(idx: int, name: str, pins: object, out: list[OpError]) -> None:
    """Validate an add_sheet ``pins`` array of {name, type, side, offset_mil}."""
    if not isinstance(pins, list):
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           f"{name}.pins must be an array of pin objects"))
        return
    for i, pin in enumerate(pins):
        if not isinstance(pin, dict):
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name}.pins[{i}] must be an object "
                               "{name, type, side, offset_mil}"))
            continue
        for req in ("name", "type", "side", "offset_mil"):
            if req not in pin:
                out.append(OpError(idx, name, "OP_UNSUPPORTED",
                                   f"{name}.pins[{i}] missing required field {req!r}"))
        if "name" in pin and not isinstance(pin["name"], str):
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name}.pins[{i}].name must be a string"))
        if "type" in pin and not _in(pin["type"], _SHEET_PIN_TYPES):
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name}.pins[{i}].type {pin['type']!r} not in "
                               f"{{{', '.join(sorted(_SHEET_PIN_TYPES))}}}"))
        if "side" in pin and not _in(pin["side"], _SHEET_PIN_SIDES):
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name}.pins[{i}].side {pin['side']!r} not in "
                               f"{{left, right, top, bottom}}"))
        off = pin.get("offset_mil")
        if "offset_mil" in pin and (not isinstance(off, (int, float))
                                    or isinstance(off, bool)):
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name}.pins[{i}].offset_mil must be a number"))
        for k in pin:
            if k not in ("name", "type", "side", "offset_mil") and not k.startswith("_"):
                _unknown_field(idx, name, f"pins[{i}].{k}",
                               ["name", "type", "side", "offset_mil"], out)


def _validate_op(idx: int, op: object) -> list[OpError]:
    out: list[OpError] = []
    if not isinstance(op, dict):
        out.append(OpError(idx, None, "OP_UNSUPPORTED", "op must be a JSON object"))
        return out
    name = op.get("op")
    # "group" and "sheet" are universally-allowed optional tags (see
    # resolve_groups / the multi-sheet envelope) — validated here once
    # instead of polluting every per-op field table.
    if "group" in op and not isinstance(op["group"], str):
        out.append(OpError(idx, name if isinstance(name, str) else None,
                           "OP_UNSUPPORTED",
                           "group must be a string (a key of the envelope "
                           "'groups' map)"))
    if "sheet" in op and not isinstance(op["sheet"], str):
        out.append(OpError(idx, name if isinstance(name, str) else None,
                           "OP_UNSUPPORTED",
                           "sheet must be a string (a key of the envelope "
                           "'sheets' map)"))
    if _in(name, MACRO_OPS):
        # macros are part of the document vocabulary: a not-yet-expanded
        # op-list must validate; plan/draw expand them before execution
        for req in MACRO_REQUIRED.get(name, []):
            if req not in op:
                out.append(OpError(idx, name, "OP_UNSUPPORTED",
                                   f"missing required field {req!r}"))
        known = set(MACRO_REQUIRED.get(name, [])) | set(MACRO_OPTIONAL.get(name, {}))
        for field in op:
            if field in ("op", "group", "sheet") or field in known or field.startswith("_"):
                continue
            _unknown_field(idx, name, field, known, out)
        _check_macro_designator_roles(idx, str(name), op, out)
        return out
    if not _in(name, OP_NAMES):
        hint = difflib.get_close_matches(
            str(name), list(OP_NAMES | MACRO_OPS), n=1) if isinstance(name, str) else []
        suggestion = f" (did you mean {hint[0]!r}?)" if hint else ""
        out.append(OpError(idx, name if isinstance(name, str) else None,
                           "OP_UNSUPPORTED", f"unknown op {name!r}{suggestion}"))
        return out

    for req in _OP_REQUIRED.get(name, []):
        if req not in op:
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name} missing required field {req!r}"))

    if name == "delete_object" and ("uuid" in op) == ("match" in op):
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           "delete_object needs exactly one of 'uuid' or 'match'"))

    if name == "set_title_block" and not any(
            f in op for f in _OP_FIELDS["set_title_block"]):
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           "set_title_block needs at least one field "
                           "(title/date/rev/company/comment1..9)"))

    if name in ("place_component", "move_component"):
        # exactly one position form: x_mil+y_mil (absolute) XOR anchor
        # (+offset_mil, world-frame offset from a "REF"/"REF.PIN" anchor)
        if "anchor" in op:
            if "x_mil" in op or "y_mil" in op:
                out.append(OpError(idx, name, "OP_UNSUPPORTED",
                                   f"{name} takes either x_mil/y_mil or "
                                   "anchor (+offset_mil), not both"))
        else:
            for req in ("x_mil", "y_mil"):
                if req not in op:
                    out.append(OpError(idx, name, "OP_UNSUPPORTED",
                                       f"{name} missing required field {req!r} "
                                       "(or place relative: anchor + offset_mil)"))
            if "offset_mil" in op:
                out.append(OpError(idx, name, "OP_UNSUPPORTED",
                                   f"{name}.offset_mil requires anchor"))

    # field-TYPE table: unknown fields (did-you-mean) + per-field kind checks.
    # Keys starting with "_" are ignored (annotation escape, schema-compatible).
    fields = _OP_FIELDS.get(name, {})
    for field, value in op.items():
        if field in ("op", "group", "sheet") or field.startswith("_"):
            continue
        kind = fields.get(field)
        if kind is None:
            _unknown_field(idx, name, field, fields, out)
        elif kind == "rotation":
            _check_rotation(idx, name, op, field, out)
        elif kind == "mirror":
            if not _in(value, _VALID_MIRRORS):
                out.append(OpError(idx, name, "OP_UNSUPPORTED",
                                   f"mirror {value!r} not in {{none,x,y}}"))
        elif kind == "scope":
            if not _in(value, _VALID_SCOPES):
                out.append(OpError(idx, name, "OP_UNSUPPORTED",
                                   f"scope {value!r} not in {{local,global,hierarchical}}"))
        elif kind == "match":
            _check_match(idx, name, value, out)
        elif kind == "sheetpins":
            _check_sheet_pins(idx, name, value, out)
        elif kind != "vertices":                    # wire block below
            check, desc = _KIND_CHECKS[kind]
            if not check(value):
                out.append(OpError(idx, name, "OP_UNSUPPORTED",
                                   f"{name}.{field} must be {desc}, got {value!r}"))

    if name in ("add_wire", "add_bus") and "vertices" in op:
        # absent vertices is already a missing-required-field error above —
        # re-reporting it here as a wire-shape problem only misleads.
        verts = op["vertices"]
        if not isinstance(verts, list):
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name}.vertices must be an array of >= 2 "
                               f"points, got {verts!r}"))
        elif len(verts) < 2:
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
    if not _in(tf, _VALID_TARGETS):
        errs.append(OpError(-1, None, "OP_UNSUPPORTED",
                            f"target_format {tf!r} not in {{kicad,altium}}"))

    groups = doc.get("groups")
    if groups is not None:
        if not isinstance(groups, dict):
            errs.append(OpError(-1, None, "OP_UNSUPPORTED",
                                "'groups' must be an object of "
                                "{name: {origin: [x_mil, y_mil], ...}}"))
        else:
            for gname, meta in groups.items():
                if not isinstance(meta, dict) or not _is_point(meta.get("origin")):
                    errs.append(OpError(-1, None, "GROUP_NO_ORIGIN",
                                        f"group {gname!r} needs an 'origin': "
                                        "[x_mil, y_mil]"))
                    continue
                if "title" in meta and not isinstance(meta["title"], str):
                    errs.append(OpError(-1, None, "OP_UNSUPPORTED",
                                        f"groups[{gname!r}].title must be a string"))
                if "frame" in meta and not isinstance(meta["frame"], bool):
                    errs.append(OpError(-1, None, "OP_UNSUPPORTED",
                                        f"groups[{gname!r}].frame must be true or false"))
                for k in meta:
                    if k not in _GROUP_META_FIELDS and not k.startswith("_"):
                        hint = difflib.get_close_matches(k, _GROUP_META_FIELDS, n=1)
                        suggestion = f" (did you mean {hint[0]!r}?)" if hint else ""
                        errs.append(OpError(-1, None, "OP_UNSUPPORTED",
                                            f"groups[{gname!r}]: unknown field "
                                            f"{k!r}{suggestion}"))

    sheets = doc.get("sheets")
    if sheets is not None:
        if not isinstance(sheets, dict):
            errs.append(OpError(-1, None, "OP_UNSUPPORTED",
                                "'sheets' must be an object of "
                                "{name: {file: '<child.kicad_sch>'}}"))
            sheets = None
        else:
            for sname, meta in sheets.items():
                if not isinstance(meta, dict) or not isinstance(
                        meta.get("file"), str) or not meta.get("file"):
                    errs.append(OpError(-1, None, "SHEET_UNKNOWN",
                                        f"sheets[{sname!r}] needs a 'file': "
                                        "'<child.kicad_sch>'"))
                    continue
                for k in meta:
                    if k != "file" and not k.startswith("_"):
                        errs.append(OpError(-1, None, "OP_UNSUPPORTED",
                                            f"sheets[{sname!r}]: unknown "
                                            f"field {k!r}"))

    ops = doc.get("ops")
    if not isinstance(ops, list):
        errs.append(OpError(-1, None, "OP_UNSUPPORTED", "ops must be an array"))
        return errs

    # Every 'sheet'-tagged op must reference a declared sheets entry (the tag
    # is validated per-op for type; the MAP membership is a document check).
    declared = set(sheets or ())
    for idx, op in enumerate(ops):
        tag = op.get("sheet") if isinstance(op, dict) else None
        if isinstance(tag, str) and tag not in declared:
            hint = difflib.get_close_matches(tag, declared, n=1) if declared else []
            suggestion = f" (did you mean {hint[0]!r}?)" if hint else ""
            errs.append(OpError(idx, op.get("op") if isinstance(op, dict) else None,
                                "SHEET_UNKNOWN",
                                f"op's sheet {tag!r} is not declared in the "
                                f"envelope 'sheets' map{suggestion}"))

    for i, op in enumerate(ops):
        errs.extend(_validate_op(i, op))

    # document-level lint: the same (designator, unit) placed twice
    for i, d, u, first in _duplicate_placements(ops):
        errs.append(OpError(i, "place_component", "OP_UNSUPPORTED",
                            f"duplicate placement of designator {d!r} unit {u} "
                            f"(first placed at op [{first}])"))

    # document-level lint: two power ports dropped on the same anchor. Their
    # symbol bodies coincide and the writer derives both node uuids from that
    # one coordinate, so the second silently owns the spot — always an authoring
    # slip (a rail needs its own anchor), never a valid intent.
    for i, opname, at, first in _duplicate_power_ports(ops):
        errs.append(OpError(i, opname, "POWER_PORT_STACKED",
                            f"duplicate power port at {at!r} (first placed at "
                            f"op [{first}]) — two power symbols stacked on one "
                            "anchor overlap and only one survives; give each "
                            "its own point/pin"))

    # document-level lint: two annotation ops sharing a stable key — the
    # writer derives the node uuid from (tag, key) alone, so the second op
    # would silently REPLACE the first's node while both report ok.
    seen_keys: dict[tuple[str, str], int] = {}
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            continue
        tag = _ANNOTATION_KEY_TAGS.get(str(op.get("op", "")))
        key = op.get("key")
        if tag is None or not isinstance(key, str) or not key:
            continue
        first_i = seen_keys.setdefault((tag, key), i)
        if first_i != i:
            errs.append(OpError(i, str(op.get("op")), "OP_UNSUPPORTED",
                                f"duplicate annotation key {key!r} for "
                                f"{op.get('op')} (first used at op "
                                f"[{first_i}]) — the second op would replace "
                                "the first node in place; use distinct keys"))
    return errs


@dataclass(frozen=True)
class PreparedOpList:
    """The result of :func:`prepare_oplist` — the document plus its verdict.

    A frozen record instead of the old ``(doc, errors)`` tuple because the
    third piece of state, "were group-local coordinates translated?", used to
    be smuggled INSIDE ``doc`` as a ``_groups_resolved`` key and leaked into
    every payload that echoed the prepared op-list. Pipeline state belongs
    beside the document, not in it.

    ``doc`` is macro-expanded and group-resolved (unchanged input when
    ``errors`` is non-empty — a document with errors is never expanded).
    """

    doc: dict
    errors: tuple[OpError, ...] = ()
    groups_resolved: bool = False

    @property
    def ok(self) -> bool:
        """True when nothing is wrong with the document."""
        return not self.errors


def prepare_oplist(doc: dict) -> PreparedOpList:
    """Validate, expand macros + resolve groups, validate again.

    THE order matters. Expanding first (what plan/draw/``ops validate`` used to
    do) means the macro branch of :func:`_validate_op` never sees a macro op —
    macros leave the document before anything looks at their fields, so a typo
    (``valu``) or a hallucinated field sailed straight through into a
    silently-different circuit. Validating the AUTHOR'S ops first closes that;
    the expansion is validated too, because a well-formed macro can still emit
    core ops that clash with hand-written ones (duplicate designators, stacked
    power ports).

    Every returned :class:`OpError` carries the author's op index — errors
    found on expanded ops are mapped back through the expansion trace, so
    ``[3]`` always means "the fourth op in YOUR file".

    Returns a :class:`PreparedOpList`. Bad macro arguments / bad group
    references still raise :class:`AkcliError` from the expansion step, exactly
    as before.
    """
    errs = validate_oplist(doc)
    if errs:
        # a macro with a bogus field must not be expanded: the expander would
        # ignore the field and manufacture a plausible-looking circuit.
        return PreparedOpList(doc, tuple(errs))
    origins: list[int] = []
    expanded = resolve_groups(expand_macros(doc, origins=origins))
    mapped: list[OpError] = []
    for e in validate_oplist(expanded):
        if 0 <= e.op_index < len(origins):
            mapped.append(OpError(origins[e.op_index], e.op, e.code, e.message))
        else:
            mapped.append(e)
    return PreparedOpList(expanded, tuple(mapped), groups_resolved=is_resolved(expanded))


def load_oplist(path: str | Path) -> dict:
    """Load and JSON-parse an op-list file. Malformed JSON -> ``OP_UNSUPPORTED``."""
    p = Path(path)
    try:
        text = p.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise
    except OSError as exc:  # a directory, mode 000, ... -> exit 4, not INTERNAL
        raise AkcliError("FILE_UNREADABLE", f"{p}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise AkcliError(
            "OP_UNSUPPORTED",
            f"op-list {p} is not UTF-8 text (byte at offset {exc.start}); "
            "an op-list is a JSON document") from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AkcliError("OP_UNSUPPORTED", f"invalid op-list JSON: {exc}") from exc
    if not isinstance(doc, dict):
        if isinstance(doc, list):
            raise AkcliError(
                "OP_UNSUPPORTED",
                "op-list root must be a JSON object, not a top-level array — "
                f'wrap your ops as {{"protocol_version": {PROTOCOL_VERSION}, '
                '"target_format": "kicad", "ops": [...]}')
        raise AkcliError("OP_UNSUPPORTED", "op-list root must be a JSON object")
    return doc
