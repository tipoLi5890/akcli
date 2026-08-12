"""The op vocabulary as this package sees it — DERIVED FROM :mod:`akcli.opspec`.

Every table here used to be hand-written next to the code that reads it, which
meant the vocabulary was spelled out three times (these tables,
``schemas/ops.schema.json``, the writer registry) and needed a CI drift gate
per pair. :mod:`akcli.opspec` is now THE ONE vocabulary source: the JSON
schemas are generated from it (``tools/schemas.py``) and this module builds the
validator/template/group-resolution views of it at import time.

Nothing here decides anything — adding or changing an op means editing
``opspec.py`` only. The names and types are unchanged from the hand-written
era so the validator, the macro expanders and ``ops template`` read exactly as
they did.
"""

from __future__ import annotations

from .. import opspec
from ..opspec import UNSET

# --------------------------------------------------------------------------- #
# op name sets                        (derived from opspec — the ONE vocabulary)
# --------------------------------------------------------------------------- #
# Core ops (the schema branches) + documented sugar (place_gnd/place_vcc).
_CORE_OPS: frozenset[str] = opspec.core_names()
_SUGAR_OPS: frozenset[str] = opspec.sugar_names()
OP_NAMES: frozenset[str] = opspec.op_names()          # 22 ops total

# Macro ops — compound placements expanded to core ops BEFORE validation, so
# they never reach the JSON schema, the executors, or the live bridge (and
# therefore never touch ``protocol_version``). Connectivity uses the
# collision-proof label-on-pin pattern (``"REF.PIN"`` anchors), not wires.
MACRO_OPS: frozenset[str] = opspec.macro_names()

# --------------------------------------------------------------------------- #
# per-op field tables                 (derived from opspec — the ONE vocabulary)
# --------------------------------------------------------------------------- #
# Required fields per op, in schema `required` order.
#
# Two ops look under-specified here on purpose: delete_object needs EXACTLY ONE
# of uuid | match, and place/move_component take EITHER x_mil+y_mil OR anchor
# (+offset_mil). Both are ``Constraint("exactly_one", ...)`` in opspec and are
# enforced in _validate_op, so neither alternative can sit in `required`.
_OP_REQUIRED: dict[str, list[str]] = {
    spec.name: list(spec.required_names()) for spec in opspec.CORE
}

# Per-op field-TYPE table (the schema's property shapes; also the
# unknown-field registry). Kinds: str · num · int>=1 (posint) · bool · point ·
# endpoint (point|"REF.PIN") · anchor (endpoint|"mid(A.p,B.p)") · dict ·
# rotation/mirror/scope enums · vertices (op-specific, checked in the wire
# block) · match (selector object) · sheetpins.
_OP_FIELDS: dict[str, dict[str, str]] = {
    spec.name: {f.name: f.kind for f in spec.fields} for spec in opspec.CORE
}

# Optional per-op fields with their ``ops template`` placeholder. A field whose
# opspec ``template`` is UNSET is ACCEPTED but not taught by the skeleton (e.g.
# place_component's ``anchor``: the template teaches the canonical absolute
# form and the schema documents the alternative).
_OP_OPTIONAL: dict[str, dict] = {
    spec.name: {f.name: f.template for f in spec.fields
                if not f.required and f.template is not UNSET}
    for spec in opspec.CORE
}

# --------------------------------------------------------------------------- #
# macro field tables                  (derived from opspec — the ONE vocabulary)
# --------------------------------------------------------------------------- #
# Single-part macros take EITHER x_mil+y_mil OR anchor (+offset_mil), exactly
# like place_component — enforced at expansion time, so (as above) neither
# alternative appears in the required list.
MACRO_REQUIRED: dict[str, list[str]] = {
    spec.name: list(spec.required_names()) for spec in opspec.MACROS
}
MACRO_OPTIONAL: dict[str, dict] = {
    spec.name: {f.name: f.template for f in spec.fields
                if not f.required and f.template is not UNSET}
    for spec in opspec.MACROS
}

# Optional macro fields ACCEPTED but left out of the template skeleton even
# though they HAVE a placeholder: the template teaches the canonical absolute
# form; the anchor alternative is documented in the schema (exactly-one is
# enforced at expansion).
_MACRO_TEMPLATE_OMIT: dict[str, frozenset[str]] = {
    spec.name: frozenset(f.name for f in spec.fields if f.template_omit)
    for spec in opspec.MACROS
    if any(f.template_omit for f in spec.fields)
}

# --------------------------------------------------------------------------- #
# Macro designator ROLES (BOM-corruption guard)
#
# Every macro assigns its designators POSITIONALLY: place_led_indicator's
# ``designators`` is [R, D] — pass ["D1", "R3"] (the intuitive reading order)
# and the file gets a ``Device:R`` named D1 and a ``Device:LED`` named R3. The
# schematic is electrically identical, every akcli check stays silent, and the
# BOM/pick-and-place is quietly wrong. So each slot declares (in opspec) the
# part class it becomes and the refdes prefixes that class conventionally uses
# (IEEE 315 / KiCad defaults). Slots whose part is caller-chosen
# (terminate_unused_unit's op-amp, place_array's arbitrary lib_id) are
# role-NEUTRAL and unconstrained — they simply declare no roles.
# --------------------------------------------------------------------------- #
_ROLE_PREFIXES: dict[str, frozenset[str]] = opspec.ROLE_PREFIXES
# macro -> (field name, [role per slot]). A scalar ``designator`` field is a
# one-slot list. Only macros whose expansion PICKS the part appear here.
_MACRO_DESIGNATOR_ROLES: dict[str, tuple[str, tuple[str, ...]]] = {
    spec.name: (spec.designator_field, spec.designator_roles)
    for spec in opspec.ALL if spec.designator_roles
}

# --------------------------------------------------------------------------- #
# enum vocabularies                   (derived from opspec — the ONE vocabulary)
# --------------------------------------------------------------------------- #
_VALID_ROTATIONS: frozenset[int] = frozenset(opspec.ENUMS["rotation"])
_VALID_MIRRORS: frozenset[str] = frozenset(opspec.ENUMS["mirror"])
_VALID_SCOPES: frozenset[str] = frozenset(opspec.ENUMS["scope"])
_VALID_TARGETS: frozenset[str] = frozenset(opspec.ENUMS["target_format"])
# add_rectangle / graphic fill styles (mirror the KiCad tokens).
_VALID_FILLS: frozenset[str] = frozenset(opspec.ENUMS["fill"])
# route_net corner styles: hv = horizontal-first L, vh = vertical-first L,
# z = 3 segments, auto = hv unless its corner sits on a pin tip.
_VALID_ROUTE_STYLES: frozenset[str] = frozenset(opspec.ENUMS["route_style"])
# delete_object match selector: object kinds it can address by name/position.
# NOTE rectangle has start/end, not an (at) anchor — a rectangle matches by
# kind (count semantics) or by uuid, never by the `at` selector.
_MATCH_KINDS: frozenset[str] = frozenset(opspec.ENUMS["match_kind"])
# add_sheet pin electrical types + edge sides (mirror the KiCad tokens).
_SHEET_PIN_TYPES: frozenset[str] = frozenset(opspec.ENUMS["sheet_pin_type"])
_SHEET_PIN_SIDES: frozenset[str] = frozenset(opspec.ENUMS["sheet_pin_side"])
