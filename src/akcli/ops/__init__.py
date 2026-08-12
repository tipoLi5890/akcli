"""Op-list vocabulary, ``PROTOCOL_VERSION`` and a zero-dependency validator.

The vocabulary itself is DATA: :mod:`akcli.opspec` declares every op, field,
constraint and enum once, ``tools/schemas.py`` generates
``schemas/ops.schema.json`` + ``ops.capabilities.json`` from it, and
:mod:`akcli.ops.vocab` builds this package's tables from the same declaration.
Adding an op therefore means editing ``opspec.py`` and writing its handler (or
expander) — nothing here needs a matching hand edit.

Coordinate/unit contract (SPEC §2.1): origin top-left, +Y down, units mils,
default 50-mil grid. Rotation is an enum ``{0,90,180,270}`` (``add_text`` may use
any ``angle``); mirror is ``{none,x,y}``; wire/port endpoints are ``[x,y]`` points
or a ``"REF.PIN"`` pin reference string. Label/power-port anchors additionally
accept ``"mid(REF.PIN,REF.PIN)"`` — the midpoint of two axis-aligned pins,
snapped to the 50-mil grid along the wire axis (the collision-proof spot for a
net name or a PWR_FLAG on a pin-to-pin wire).

Package layout (a strict DAG; ``base``/``vocab`` import no sibling):

``base``       value predicates, ``OpError``/``Op``, ``PROTOCOL_VERSION``
``vocab``      every op/field/enum table, derived from :mod:`akcli.opspec`
``lints``      document-level lints shared by the validator and the expander
``template``   ``akcli ops template`` skeletons
``macros``     macro expanders + :func:`expand_macros`
``groups``     group-local coordinate resolution
``validate``   the validator, :func:`prepare_oplist`, :func:`load_oplist`
``schema_io``  reading the published JSON schemas

This module re-exports the flat surface the rest of akcli (and the tests) have
always imported as ``akcli.ops.<name>``.
"""

from __future__ import annotations

from .base import (
    GRID_MIL,
    PROTOCOL_VERSION,
    Op,
    OpError,
    _in,
    _is_anchor,
    _is_anchor_ref,
    _is_endpoint,
    _is_pin_ref,
    _is_point,
    _MID_RE,
    parse_mid_anchor,
)
from .groups import (
    _GROUP_AT_OPS,
    _GROUP_META_FIELDS,
    _GROUP_VERTEX_OPS,
    _GROUP_XY_OPS,
    _group_origin,
    _shift_point,
    _translate_op_coords,
    resolve_groups,
)
from .lints import (
    _ANNOTATION_KEY_TAGS,
    _POWER_PORT_OPS,
    _anchor_key,
    _duplicate_placements,
    _duplicate_power_ports,
    _is_pwr_flag_op,
)
from .macros import (
    _ARRAY_DIRECTIONS,
    _ARRAY_MAX_COUNT,
    _MACRO_EXPANDERS,
    _MID_TOL_MIL,
    _macro_fail,
    _macro_num,
    _macro_str,
    _mid_point_mils,
    _snap_clamp_mil,
    expand_macros,
)
from .schema_io import _schema_text, _schemas_dir, load_capabilities, load_schema
from .template import _placeholder, op_template
from .validate import (
    PreparedOpList,
    _KIND_CHECKS,
    _check_macro_designator_roles,
    _check_match,
    _check_rotation,
    _check_sheet_pins,
    _macro_role_slots,
    _refdes_prefix,
    _unknown_field,
    _validate_op,
    load_oplist,
    prepare_oplist,
    validate_oplist,
)
from .vocab import (
    _CORE_OPS,
    _MACRO_DESIGNATOR_ROLES,
    _MACRO_TEMPLATE_OMIT,
    _MATCH_KINDS,
    _OP_FIELDS,
    _OP_OPTIONAL,
    _OP_REQUIRED,
    _ROLE_PREFIXES,
    _SHEET_PIN_SIDES,
    _SHEET_PIN_TYPES,
    _SUGAR_OPS,
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

__all__ = [
    "GRID_MIL",
    "MACRO_OPS",
    "MACRO_OPTIONAL",
    "MACRO_REQUIRED",
    "OP_NAMES",
    "Op",
    "OpError",
    "PROTOCOL_VERSION",
    "PreparedOpList",
    "expand_macros",
    "load_capabilities",
    "load_oplist",
    "load_schema",
    "op_template",
    "parse_mid_anchor",
    "prepare_oplist",
    "resolve_groups",
    "validate_oplist",
]
