"""The op vocabulary as DATA — one source of truth for every op-shaped table.

akcli's op vocabulary is currently spelled out three times: the hand-written
tables in :mod:`akcli.ops` (required/optional/field-kind/placeholder/role/
group-coordinate), ``schemas/ops.schema.json`` (+ ``ops.capabilities.json``),
and the writer handler registry. Three spellings of one contract need three
CI drift gates and still let an op ship half-declared. This module is the
declaration those three become views of: ``tools/gen_ops_schema.py`` already
regenerates both JSON schemas from it byte-for-byte, and the follow-up wiring
batch rewrites the ``ops.py`` tables as derivations.

Deliberately inert: no imports beyond ``dataclasses``/``typing``, no I/O, no
akcli imports — anything may depend on it and it depends on nothing, so it
can never introduce an import cycle (layering rank 20).

Reading the model
-----------------
``CORE`` holds the 22 executable ops (``kind`` ``"core"``, plus the two
``"sugar"`` spellings ``place_gnd``/``place_vcc`` that share a handler with
``place_power_port``); ``MACROS`` holds the 10 compound ops expanded to core
ops before validation. Both tuples are in ``schemas/ops.schema.json`` branch
order, and each :class:`OpSpec`'s ``fields`` are in that branch's property
order — the JSON generator relies on both.

Every op additionally accepts the two UNIVERSAL fields ``group`` (functional
group tag) and ``sheet`` (multi-sheet routing tag). They are not repeated in
each spec: :data:`UNIVERSAL_FIELDS` carries them once and the generator
injects them into every branch.

``Field.template`` is the ``akcli ops template`` skeleton value; :data:`UNSET`
means "accepted but not taught by the template" (e.g. ``place_component``'s
``anchor``). ``template_omit`` marks a field that HAS a placeholder yet is
still left out of the skeleton (the anchor alternative on the single-part
macros). ``Field.coord`` classifies group-local coordinate translation:
``"x"``/``"y"`` scalar halves, ``"point"`` an ``[x, y]`` slot that shifts only
when it holds a literal point (pin/``mid()`` anchors are position-independent),
``"vertices"`` a list of those. Macro fields are never classified — macros
expand before groups resolve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


class _Unset:
    """Type of :data:`UNSET` (a distinct 'no template' marker, unlike None)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final[_Unset] = _Unset()


@dataclass(frozen=True)
class Constraint:
    """A cross-field rule no single field declaration can express.

    ``kind``:

    * ``"exactly_one"`` — exactly one of the alternatives in ``groups`` may be
      present, each alternative being the tuple of fields it needs
      (``(("x_mil", "y_mil"), ("anchor",))`` = absolute XOR relative
      placement).
    * ``"at_least_one_field"`` — at least one declared field besides ``op``
      (``set_title_block``); ``groups`` is empty.

    ``schema_enforced`` says whether the JSON schema branch can state the rule
    (``delete_object``'s uuid|match becomes ``oneOf``, ``set_title_block``'s
    becomes ``minProperties: 2``). ``False`` = prose in the branch description
    only, enforced by the validator or at macro expansion — the placement
    XOR, which JSON Schema cannot express without exploding the branch.
    """

    kind: str
    groups: tuple[tuple[str, ...], ...] = ()
    schema_enforced: bool = True


@dataclass(frozen=True)
class Field:
    """One op field: its type contract, template value and coordinate role."""

    name: str
    kind: str
    """Type vocabulary shared with ``ops._OP_FIELDS`` (``str``/``num``/
    ``point``/``anchor``/...); ``tools/gen_ops_schema.py`` maps each kind to
    the JSON-schema fragment it promises."""

    item_kind: str = ""
    """Element kind for the container kinds (``vertices``: ``add_wire``
    accepts pin refs, ``add_bus`` is points-only)."""

    required: bool = False
    template: object = UNSET
    template_omit: bool = False
    coord: str | None = None
    doc: str = ""


@dataclass(frozen=True)
class OpSpec:
    """One entry of the op vocabulary — core, sugar or macro."""

    name: str
    kind: str                                  # "core" | "sugar" | "macro"
    doc: str = ""
    fields: tuple[Field, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    handler: str = ""
    """``writers.handlers`` function registered for this op (core/sugar)."""

    expander: str = ""
    """``ops._MACRO_EXPANDERS`` function for this op (macros)."""

    designator_field: str = ""
    """Field naming the refdes slots whose part class the macro PICKS."""

    designator_roles: tuple[str, ...] = ()
    """Part class per designator slot, positionally (BOM-corruption guard)."""

    kicad: bool = True
    altium: bool = False
    caps_note: str | None = None
    """Capability-matrix note; ``None`` = the row carries no ``notes`` key."""

    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def required_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.required)


# Accepted on EVERY op (core, sugar and macro alike) and therefore declared
# once instead of 32 times; the schema generator injects them right after the
# `op` discriminator in each branch.
UNIVERSAL_FIELDS: Final[tuple[Field, ...]] = (
    Field(
        "group", "str",
        doc="Optional functional-group tag; coordinates become group-local "
            "(see top-level groups).",
    ),
    Field(
        "sheet", "str",
        doc="Optional sheet-routing tag: apply this op to the child file the "
            "envelope's 'sheets' map declares under this key (0.20 "
            "multi-sheet envelope; absent = the draw target itself).",
    ),
)

# Enumerated value vocabularies, keyed by the ``Field.kind`` (or nested slot)
# that uses them. ORDER IS LOAD-BEARING: ``tools/gen_ops_schema.py`` renders
# each tuple straight into a JSON-schema ``enum`` array, so a reordering is a
# schema byte change; ``akcli.ops.vocab`` derives the validator's frozensets
# from the same tuples, which is why neither side can drift.
ENUMS: Final[dict[str, tuple[object, ...]]] = {
    "rotation": (0, 90, 180, 270),
    "mirror": ("none", "x", "y"),
    "scope": ("local", "global", "hierarchical"),
    "fill": ("none", "outline", "background"),
    "route_style": ("auto", "hv", "vh", "z"),
    "array_direction": ("right", "down", "left", "up"),
    # envelope-level, not a field kind: the op-list's ``target_format``.
    "target_format": ("kicad", "altium"),
    # nested slots of the composite kinds
    "match_kind": ("wire", "bus", "label", "global_label",
                   "hierarchical_label", "junction", "no_connect", "text",
                   "bus_entry", "rectangle", "text_box"),
    "sheet_pin_type": ("input", "output", "bidirectional", "tri_state",
                       "passive"),
    "sheet_pin_side": ("left", "right", "top", "bottom"),
}


def enum(name: str) -> list[object]:
    """One :data:`ENUMS` vocabulary as a fresh JSON-schema ``enum`` list."""
    return list(ENUMS[name])


# Refdes prefixes each designator ROLE conventionally uses (IEEE 315 / KiCad
# defaults) — the lookup behind ``OpSpec.designator_roles``.
ROLE_PREFIXES: Final[dict[str, frozenset[str]]] = {
    "resistor": frozenset({"R"}),
    "capacitor": frozenset({"C"}),
    "LED": frozenset({"D", "LED"}),
    "crystal": frozenset({"X", "Y"}),
}


CORE: Final[tuple[OpSpec, ...]] = (
    OpSpec(
        name="place_component",
        kind="core",
        doc=(
            "Position is exactly one of x_mil+y_mil (absolute) or anchor "
            "(+offset_mil, relative to a placed pin/component)."
        ),
        fields=(
            Field(
                "lib_id", "str",
                required=True,
                template="<Lib:Symbol>",
            ),
            Field(
                "designator", "str",
                required=True,
                template="<REF>",
            ),
            Field(
                "x_mil", "num",
                template=0,
                coord="x",
            ),
            Field(
                "y_mil", "num",
                template=0,
                coord="y",
            ),
            Field(
                "anchor", "anchor_ref",
            ),
            Field(
                "offset_mil", "point",
            ),
            Field(
                "rotation", "rotation",
                template=0,
            ),
            Field(
                "mirror", "mirror",
                template="none",
            ),
            Field(
                "unit", "posint",
                template=1,
                doc=(
                    "Unit of a multi-unit part to place (default 1); each "
                    "unit is its own instance sharing the designator."
                ),
            ),
            Field(
                "value", "str",
                template="<value>",
            ),
            Field(
                "footprint", "str",
                template="<Lib:Footprint>",
            ),
            Field(
                "symbol_source", "str",
                template="<extra.kicad_sym>",
            ),
            Field(
                "instance_designators", "dict",
                template={"<instance path>": "<REF>"},
                doc=(
                    "Per-instance references for a sheet file instantiated "
                    "more than once: {\"<instance path>\": \"<REF>\"} — "
                    "pairwise-distinct values; 'designator' must be one of "
                    "them (it seeds the uuid and is the displayed "
                    "Reference). Required (INSTANCE_REFS_REQUIRED) when the "
                    "target sheet has several instance paths."
                ),
            ),
        ),
        constraints=(
            Constraint(
                "exactly_one", (("x_mil", "y_mil"), ("anchor",),),
                schema_enforced=False,
            ),
        ),
        handler="_op_place_component",
        altium=True,
        caps_note="",
    ),
    OpSpec(
        name="delete_component",
        kind="core",
        doc="",
        fields=(
            Field(
                "designator", "str",
                required=True,
                template="<REF>",
            ),
            Field(
                "cascade", "bool",
                template=False,
                doc=(
                    "Also delete wires ending on a deleted pin plus "
                    "labels/no_connects/junctions anchored there (cascaded "
                    "uuids are reported)."
                ),
            ),
        ),
        handler="_op_delete_component",
    ),
    OpSpec(
        name="delete_object",
        kind="core",
        doc="",
        fields=(
            Field(
                "uuid", "str",
                template="<object-uuid>",
            ),
            Field(
                "match", "match",
                doc=(
                    "Selector alternative to uuid; must match exactly one "
                    "object (0 = replay-safe note, >1 = error listing "
                    "candidates)."
                ),
            ),
        ),
        constraints=(
            Constraint("exactly_one", (("uuid",), ("match",),)),
        ),
        handler="_op_delete_object",
    ),
    OpSpec(
        name="move_component",
        kind="core",
        doc=(
            "Destination is exactly one of x_mil+y_mil (absolute) or anchor "
            "(+offset_mil)."
        ),
        fields=(
            Field(
                "designator", "str",
                required=True,
                template="<REF>",
            ),
            Field(
                "x_mil", "num",
                template=0,
                coord="x",
            ),
            Field(
                "y_mil", "num",
                template=0,
                coord="y",
            ),
            Field(
                "anchor", "anchor_ref",
            ),
            Field(
                "offset_mil", "point",
            ),
            Field(
                "unit", "posint",
                template=1,
            ),
            Field(
                "carry_labels", "bool",
                template=False,
            ),
            Field(
                "carry_wires", "bool",
                template=False,
            ),
        ),
        constraints=(
            Constraint(
                "exactly_one", (("x_mil", "y_mil"), ("anchor",),),
                schema_enforced=False,
            ),
        ),
        handler="_op_move_component",
    ),
    OpSpec(
        name="set_component_transform",
        kind="core",
        doc="",
        fields=(
            Field(
                "designator", "str",
                required=True,
                template="<REF>",
            ),
            Field(
                "rotation", "rotation",
                template=0,
            ),
            Field(
                "mirror", "mirror",
                template="none",
            ),
        ),
        handler="_op_set_component_transform",
        altium=True,
        caps_note="rotate/mirror existing component",
    ),
    OpSpec(
        name="set_component_parameters",
        kind="core",
        doc="",
        fields=(
            Field(
                "designator", "str",
                required=True,
                template="<REF>",
            ),
            Field(
                "new_designator", "str",
                template="<REF>",
                doc="Rename the component: the NEW reference designator.",
            ),
            Field(
                "value", "str",
                template="<value>",
            ),
            Field(
                "footprint", "str",
                template="<Lib:Footprint>",
            ),
            Field(
                "parameters", "dict",
                template={"<KEY>": "<VALUE>"},
            ),
            Field(
                "instance_designators", "dict",
                template={"<instance path>": "<REF>"},
                doc=(
                    "Per-instance rename for a symbol on a "
                    "multiply-instantiated sheet: {\"<instance path>\": "
                    "\"<REF>\"} (a scalar 'new_designator' on such a symbol "
                    "refuses with INSTANCE_REFS_REQUIRED — it would smash "
                    "the per-path references into one duplicate refdes)."
                ),
            ),
        ),
        handler="_op_set_component_parameters",
        altium=True,
        caps_note="ref/value/footprint/params",
    ),
    OpSpec(
        name="add_wire",
        kind="core",
        doc="",
        fields=(
            Field(
                "vertices", "vertices",
                item_kind="endpoint",
                required=True,
                template=["<REF.PIN>", [0, 0]],
                coord="vertices",
            ),
        ),
        handler="_op_add_wire",
        altium=True,
        caps_note="orthogonal; may emit many segments",
    ),
    OpSpec(
        name="route_net",
        kind="core",
        doc=(
            "Deterministic orthogonal auto-route between two endpoints: "
            "coaxial -> straight wire; otherwise an L/Z whose corner "
            "provably avoids every placed pin tip (a coincident corner would "
            "silently merge nets)."
        ),
        fields=(
            Field(
                "from", "endpoint",
                required=True,
                template="<REF.PIN>",
                coord="point",
            ),
            Field(
                "to", "endpoint",
                required=True,
                template="<REF.PIN>",
                coord="point",
            ),
            Field(
                "style", "route_style",
                template="auto",
                doc=(
                    "Corner style: hv = horizontal-first L, vh = "
                    "vertical-first L, z = 3 segments; auto (default) picks "
                    "hv unless its corner lands on a pin tip, then vh, then "
                    "z."
                ),
            ),
            Field(
                "label", "str",
                template="<NET_NAME>",
                doc=(
                    "Optional net name placed ONCE at the longest segment's "
                    "midpoint (the wire_net pattern for non-coaxial pins)."
                ),
            ),
            Field(
                "scope", "scope",
                template="local",
            ),
        ),
        handler="_op_route_net",
        caps_note=(
            "deterministic L/Z auto-route, corner avoids pin tips; optional "
            "mid-wire label"
        ),
    ),
    OpSpec(
        name="add_junction",
        kind="core",
        doc="",
        fields=(
            Field(
                "at", "point",
                required=True,
                template=[0, 0],
                coord="point",
            ),
        ),
        handler="_op_add_junction",
        altium=True,
        caps_note="",
    ),
    OpSpec(
        name="add_no_connect",
        kind="core",
        doc="",
        fields=(
            Field(
                "pin", "endpoint",
                required=True,
                template="<REF.PIN>",
                coord="point",
            ),
        ),
        handler="_op_add_no_connect",
        altium=True,
        caps_note="",
    ),
    OpSpec(
        name="add_net_label",
        kind="core",
        doc="",
        fields=(
            Field(
                "name", "str",
                required=True,
                template="<NET_NAME>",
            ),
            Field(
                "at", "anchor",
                required=True,
                template=[0, 0],
                coord="point",
            ),
            Field(
                "scope", "scope",
                template="local",
            ),
            Field(
                "rotation", "rotation",
                template=0,
            ),
        ),
        handler="_op_add_net_label",
        altium=True,
        caps_note="scope local|global|hierarchical",
    ),
    OpSpec(
        name="place_power_port",
        kind="core",
        doc="",
        fields=(
            Field(
                "lib_id", "str",
                required=True,
                template="<Lib:Symbol>",
            ),
            Field(
                "net_name", "str",
                required=True,
                template="<NET_NAME>",
            ),
            Field(
                "at", "anchor",
                required=True,
                template=[0, 0],
                coord="point",
            ),
            Field(
                "rotation", "rotation",
                template=0,
            ),
        ),
        handler="_op_place_power_port",
        altium=True,
        caps_note="",
    ),
    OpSpec(
        name="place_gnd",
        kind="sugar",
        doc="",
        fields=(
            Field(
                "lib_id", "str",
                template="power:GND",
            ),
            Field(
                "net_name", "str",
                template="GND",
            ),
            Field(
                "at", "anchor",
                required=True,
                template=[0, 0],
                coord="point",
            ),
            Field(
                "rotation", "rotation",
                template=0,
            ),
        ),
        handler="_op_place_power_port",
        altium=True,
        caps_note="sugar over place_power_port",
    ),
    OpSpec(
        name="place_vcc",
        kind="sugar",
        doc="",
        fields=(
            Field(
                "lib_id", "str",
                template="power:VCC",
            ),
            Field(
                "net_name", "str",
                template="VCC",
            ),
            Field(
                "at", "anchor",
                required=True,
                template=[0, 0],
                coord="point",
            ),
            Field(
                "rotation", "rotation",
                template=0,
            ),
        ),
        handler="_op_place_power_port",
        altium=True,
        caps_note="sugar over place_power_port",
    ),
    OpSpec(
        name="rename_net",
        kind="core",
        doc="",
        fields=(
            Field(
                "old_name", "str",
                required=True,
                template="<OLD_NET>",
            ),
            Field(
                "new_name", "str",
                required=True,
                template="<NEW_NET>",
            ),
            Field(
                "scope", "scope",
                template="local",
                doc=(
                    "Restrict the rewrite to one label kind; omitted = all "
                    "label kinds + power-port net Values."
                ),
            ),
        ),
        handler="_op_rename_net",
        caps_note=(
            "rewrites label texts + power-port net Values; 0 matches = "
            "replay-safe note"
        ),
    ),
    OpSpec(
        name="add_bus",
        kind="core",
        doc="",
        fields=(
            Field(
                "vertices", "vertices",
                item_kind="point",
                required=True,
                template=[[0, 0], [100, 0]],
                coord="vertices",
            ),
        ),
        handler="_op_add_bus",
        caps_note="OP_UNSUPPORTED on Altium live v1",
    ),
    OpSpec(
        name="add_bus_entry",
        kind="core",
        doc="",
        fields=(
            Field(
                "at", "point",
                required=True,
                template=[0, 0],
                coord="point",
            ),
            Field(
                "size", "point",
                template=[100, 100],
            ),
        ),
        handler="_op_add_bus_entry",
        caps_note="OP_UNSUPPORTED on Altium live v1",
    ),
    OpSpec(
        name="add_text",
        kind="core",
        doc="",
        fields=(
            Field(
                "text", "str",
                required=True,
                template="<free text>",
            ),
            Field(
                "at", "point",
                required=True,
                template=[0, 0],
                coord="point",
            ),
            Field(
                "angle", "num",
                template=0,
            ),
            Field(
                "key", "str",
                template="<stable-key>",
                doc=(
                    "Stable replacement handle: re-emitting the same key "
                    "replaces this node in place regardless of coordinates "
                    "or op-list position (group titles use "
                    "group_title:<name>)."
                ),
            ),
        ),
        handler="_op_add_text",
        altium=True,
        caps_note="free angle allowed",
    ),
    OpSpec(
        name="add_rectangle",
        kind="core",
        doc=(
            "Top-level graphic rectangle (pure annotation, "
            "connectivity-neutral): module borders / group frames. Delete by "
            "uuid or by kind (it has no `at` anchor, so the match `at` "
            "selector never hits a rectangle)."
        ),
        fields=(
            Field(
                "start", "point",
                required=True,
                template=[0, 0],
                coord="point",
                doc="One corner [x, y] mil of the box.",
            ),
            Field(
                "end", "point",
                required=True,
                template=[1000, 800],
                coord="point",
                doc="The opposite corner [x, y] mil.",
            ),
            Field(
                "stroke_width_mil", "num",
                template=0,
                doc="Border line width in mils (0 = KiCad default).",
            ),
            Field(
                "fill", "fill",
                template="none",
            ),
            Field(
                "key", "str",
                template="<stable-key>",
                doc=(
                    "Stable replacement handle: re-emitting the same key "
                    "replaces this node in place regardless of coordinates "
                    "or op-list position (group frames use "
                    "group_frame:<name>)."
                ),
            ),
        ),
        handler="_op_add_rectangle",
        caps_note="top-level graphic (group frames); connectivity-neutral",
    ),
    OpSpec(
        name="add_text_box",
        kind="core",
        doc=(
            "Bordered multi-line note box (module notes / design rationale). "
            "Pure annotation, connectivity-neutral; grammar fixture-verified "
            "against a real KiCad."
        ),
        fields=(
            Field(
                "text", "str",
                required=True,
                template="<free text>",
                doc="Note content; \\n for line breaks.",
            ),
            Field(
                "at", "point",
                required=True,
                template=[0, 0],
                coord="point",
                doc="TOP-LEFT corner [x, y] mil of the box.",
            ),
            Field(
                "size", "point",
                required=True,
                template=[1000, 600],
                doc="[width, height] mil.",
            ),
            Field(
                "angle", "num",
                template=0,
            ),
            Field(
                "key", "str",
                template="<stable-key>",
                doc="Stable replacement handle (see add_rectangle.key).",
            ),
        ),
        handler="_op_add_text_box",
        caps_note="bordered multi-line note box; connectivity-neutral",
    ),
    OpSpec(
        name="add_sheet",
        kind="core",
        doc=(
            "Emit a hierarchical (sheet ...) node with Sheetname/Sheetfile, "
            "sheet pins at computed edge coordinates and the (instances) "
            "block; the child file must already exist unless create is true, "
            "in which case the transaction writes a blank one."
        ),
        fields=(
            Field(
                "name", "str",
                required=True,
                template="<sheet-name>",
                doc="Sheetname property value.",
            ),
            Field(
                "file", "str",
                required=True,
                template="<child.kicad_sch>",
                doc=(
                    "Sheetfile property value; the referenced child "
                    ".kicad_sch must already exist unless create is true."
                ),
            ),
            Field(
                "at", "point",
                required=True,
                template=[0, 0],
                coord="point",
                doc="Top-left corner [x, y] mil of the sheet box.",
            ),
            Field(
                "size", "point",
                required=True,
                template=[1000, 800],
                doc="[width, height] mil of the sheet box.",
            ),
            Field(
                "pins", "sheetpins",
                template=[
                    {
                        "name": "<pin>",
                        "type": "input",
                        "side": "left",
                        "offset_mil": 0,
                    },
                ],
                doc=(
                    "Sheet pins on the box edges; each pin's edge coordinate "
                    "is at + offset_mil along its side, wire endpoints "
                    "attach to that coordinate."
                ),
            ),
            Field(
                "create", "bool",
                template=False,
                doc=(
                    "Create the child .kicad_sch as a blank sheet when it "
                    "does not exist yet, inside the same transaction; an "
                    "existing child is never touched. Default false keeps "
                    "the SHEET_FILE_MISSING refusal."
                ),
            ),
        ),
        handler="_op_add_sheet",
        caps_note=(
            "hierarchical (sheet ...) node + sheet pins; child file authored "
            "separately (or created in-transaction with create: true). "
            "OP_UNSUPPORTED on Altium live v1"
        ),
    ),
    OpSpec(
        name="set_title_block",
        kind="core",
        doc=(
            "Set/replace title-block fields (at least one required). "
            "Find-or-create keeps the node where eeschema expects it (after "
            "paper, before lib_symbols); connectivity-neutral; unchanged "
            "values replay as a no-op note."
        ),
        fields=(
            Field(
                "title", "str",
                template="<title>",
            ),
            Field(
                "date", "str",
                template="<YYYY-MM-DD>",
            ),
            Field(
                "rev", "str",
                template="<rev>",
            ),
            Field(
                "company", "str",
                template="<company>",
            ),
            Field(
                "comment1", "str",
                template="<comment>",
            ),
            Field(
                "comment2", "str",
            ),
            Field(
                "comment3", "str",
            ),
            Field(
                "comment4", "str",
            ),
            Field(
                "comment5", "str",
            ),
            Field(
                "comment6", "str",
            ),
            Field(
                "comment7", "str",
            ),
            Field(
                "comment8", "str",
            ),
            Field(
                "comment9", "str",
            ),
        ),
        constraints=(
            Constraint("at_least_one_field", ()),
        ),
        handler="_op_set_title_block",
        caps_note=(
            "title/date/rev/company/comment1..9; >=1 field; "
            "connectivity-neutral"
        ),
    ),
)


MACROS: Final[tuple[OpSpec, ...]] = (
    OpSpec(
        name="place_divider",
        kind="macro",
        doc="",
        fields=(
            Field(
                "x_mil", "num",
                required=True,
                template=0,
            ),
            Field(
                "y_mil", "num",
                required=True,
                template=0,
            ),
            Field(
                "top_net", "str",
                required=True,
                template="<top_net>",
            ),
            Field(
                "mid_net", "str",
                required=True,
                template="<mid_net>",
            ),
            Field(
                "bottom_net", "str",
                required=True,
                template="<bottom_net>",
            ),
            Field(
                "designators", "strpair",
                template=["R1", "R2"],
            ),
            Field(
                "values", "strpair",
                template=["10k", "10k"],
            ),
            Field(
                "spacing_mil", "num",
                template=400,
            ),
            Field(
                "lib_id", "str",
                template="Device:R",
            ),
        ),
        expander="_expand_divider",
        designator_field="designators",
        designator_roles=("resistor", "resistor"),
    ),
    OpSpec(
        name="place_decoupling",
        kind="macro",
        doc=(
            "Macro: one bypass cap + rail/gnd labels on its pins. Position "
            "is exactly one of x_mil+y_mil or anchor (+offset_mil) — e.g. "
            "anchor 'U1.VCC' drops the cap next to the pin it decouples."
        ),
        fields=(
            Field(
                "x_mil", "num",
                template=0,
            ),
            Field(
                "y_mil", "num",
                template=0,
            ),
            Field(
                "anchor", "anchor_ref",
                template="<REF.PIN>",
                template_omit=True,
            ),
            Field(
                "offset_mil", "point",
                template=[0, 0],
                template_omit=True,
            ),
            Field(
                "power_net", "str",
                required=True,
                template="<power_net>",
            ),
            Field(
                "designator", "str",
                template="C1",
            ),
            Field(
                "value", "str",
                template="100n",
            ),
            Field(
                "gnd_net", "str",
                template="GND",
            ),
            Field(
                "lib_id", "str",
                template="Device:C",
            ),
        ),
        constraints=(
            Constraint(
                "exactly_one", (("x_mil", "y_mil"), ("anchor",),),
                schema_enforced=False,
            ),
        ),
        expander="_expand_decoupling",
        designator_field="designator",
        designator_roles=("capacitor",),
    ),
    OpSpec(
        name="place_pullup",
        kind="macro",
        doc=(
            "Macro: one pull-up/-down resistor + labels. Position is exactly "
            "one of x_mil+y_mil or anchor (+offset_mil)."
        ),
        fields=(
            Field(
                "x_mil", "num",
                template=0,
            ),
            Field(
                "y_mil", "num",
                template=0,
            ),
            Field(
                "anchor", "anchor_ref",
                template="<REF.PIN>",
                template_omit=True,
            ),
            Field(
                "offset_mil", "point",
                template=[0, 0],
                template_omit=True,
            ),
            Field(
                "net", "str",
                required=True,
                template="<NET_NAME>",
            ),
            Field(
                "rail_net", "str",
                required=True,
                template="<rail_net>",
            ),
            Field(
                "designator", "str",
                template="R1",
            ),
            Field(
                "value", "str",
                template="10k",
            ),
            Field(
                "lib_id", "str",
                template="Device:R",
            ),
        ),
        constraints=(
            Constraint(
                "exactly_one", (("x_mil", "y_mil"), ("anchor",),),
                schema_enforced=False,
            ),
        ),
        expander="_expand_pullup",
        designator_field="designator",
        designator_roles=("resistor",),
    ),
    OpSpec(
        name="place_led_indicator",
        kind="macro",
        doc="",
        fields=(
            Field(
                "x_mil", "num",
                required=True,
                template=0,
            ),
            Field(
                "y_mil", "num",
                required=True,
                template=0,
            ),
            Field(
                "net", "str",
                required=True,
                template="<NET_NAME>",
            ),
            Field(
                "designators", "strpair",
                template=["R1", "D1"],
            ),
            Field(
                "r_value", "str",
                template="330",
            ),
            Field(
                "gnd_net", "str",
                template="GND",
            ),
            Field(
                "mid_net", "str",
                template="",
            ),
            Field(
                "spacing_mil", "num",
                template=400,
            ),
        ),
        expander="_expand_led_indicator",
        designator_field="designators",
        designator_roles=("resistor", "LED"),
    ),
    OpSpec(
        name="place_rc_filter",
        kind="macro",
        doc="",
        fields=(
            Field(
                "x_mil", "num",
                required=True,
                template=0,
            ),
            Field(
                "y_mil", "num",
                required=True,
                template=0,
            ),
            Field(
                "in_net", "str",
                required=True,
                template="<in_net>",
            ),
            Field(
                "out_net", "str",
                required=True,
                template="<out_net>",
            ),
            Field(
                "designators", "strpair",
                template=["R1", "C1"],
            ),
            Field(
                "r_value", "str",
                template="1k",
            ),
            Field(
                "c_value", "str",
                template="100n",
            ),
            Field(
                "gnd_net", "str",
                template="GND",
            ),
            Field(
                "spacing_mil", "num",
                template=400,
            ),
        ),
        expander="_expand_rc_filter",
        designator_field="designators",
        designator_roles=("resistor", "capacitor"),
    ),
    OpSpec(
        name="place_crystal",
        kind="macro",
        doc="",
        fields=(
            Field(
                "x_mil", "num",
                required=True,
                template=0,
            ),
            Field(
                "y_mil", "num",
                required=True,
                template=0,
            ),
            Field(
                "in_net", "str",
                required=True,
                template="<in_net>",
            ),
            Field(
                "out_net", "str",
                required=True,
                template="<out_net>",
            ),
            Field(
                "designators", "strtriple",
                template=["Y1", "C1", "C2"],
            ),
            Field(
                "value", "str",
                template="",
            ),
            Field(
                "load_c", "str",
                template="22p",
            ),
            Field(
                "gnd_net", "str",
                template="GND",
            ),
            Field(
                "spacing_mil", "num",
                template=400,
            ),
        ),
        expander="_expand_crystal",
        designator_field="designators",
        designator_roles=("crystal", "capacitor", "capacitor"),
    ),
    OpSpec(
        name="place_array",
        kind="macro",
        doc=(
            "Macro: N identical parts in a row/column at a fixed pitch, "
            "named <prefix><start_index>.. — placement sugar; labels/wiring "
            "stay explicit."
        ),
        fields=(
            Field(
                "lib_id", "str",
                required=True,
                template="<Lib:Symbol>",
            ),
            Field(
                "designator_prefix", "str",
                required=True,
                template="R",
            ),
            Field(
                "count", "array_count",
                required=True,
                template=4,
            ),
            Field(
                "x_mil", "num",
                required=True,
                template=0,
            ),
            Field(
                "y_mil", "num",
                required=True,
                template=0,
            ),
            Field(
                "start_index", "nonneg_int",
                template=1,
            ),
            Field(
                "pitch_mil", "num",
                template=400,
            ),
            Field(
                "direction", "array_direction",
                template="right",
            ),
            Field(
                "value", "str",
                template="<value>",
            ),
            Field(
                "values", "strlist",
                template=["<v1>", "<v2>"],
                doc=(
                    "Per-element values; length must equal count (checked at "
                    "expansion)."
                ),
            ),
            Field(
                "rotation", "rotation",
                template=0,
            ),
            Field(
                "footprint", "str",
                template="<Lib:Footprint>",
            ),
        ),
        expander="_expand_array",
    ),
    OpSpec(
        name="connect_and_label",
        kind="macro",
        doc=(
            "Macro: pin-to-pin wire + ONE net label mid-wire (the fix for "
            "facing-pin label collisions)."
        ),
        fields=(
            Field(
                "from", "endpoint",
                required=True,
                template="<REF.PIN>",
            ),
            Field(
                "to", "endpoint",
                required=True,
                template="<REF.PIN>",
            ),
            Field(
                "net", "str",
                required=True,
                template="<NET_NAME>",
            ),
            Field(
                "orientation", "rotation",
                template=0,
            ),
            Field(
                "scope", "scope",
                template="local",
            ),
        ),
        expander="_expand_connect_and_label",
    ),
    OpSpec(
        name="place_pwr_flag",
        kind="macro",
        doc=(
            "Macro: power:PWR_FLAG placed MID-WIRE (on-pin placement "
            "overlaps symbol bodies); default rotation 90 turns the flag "
            "body off the wire axis."
        ),
        fields=(
            Field(
                "at", "anchor",
                required=True,
                template="mid(<REF.PIN>,<REF.PIN>)",
            ),
            Field(
                "rotation", "rotation",
                template=90,
            ),
        ),
        expander="_expand_pwr_flag",
    ),
    OpSpec(
        name="terminate_unused_unit",
        kind="macro",
        doc=(
            "Macro: place an unused op-amp/comparator unit and terminate it "
            "(gnd on in_plus, vcc on in_minus, no-connect on out)."
        ),
        fields=(
            Field(
                "designator", "str",
                required=True,
                template="<REF>",
            ),
            Field(
                "lib_id", "str",
                required=True,
                template="<Lib:Symbol>",
            ),
            Field(
                "unit", "posint",
                required=True,
                template=2,
            ),
            Field(
                "at", "point",
                required=True,
                template=[0, 0],
            ),
            Field(
                "in_plus", "str",
                required=True,
                template="<PIN>",
            ),
            Field(
                "in_minus", "str",
                required=True,
                template="<PIN>",
            ),
            Field(
                "out", "str",
                required=True,
                template="<PIN>",
            ),
            Field(
                "vcc", "str",
                template="VCC",
            ),
            Field(
                "gnd", "str",
                template="GND",
            ),
        ),
        expander="_expand_terminate_unused_unit",
    ),
)


ALL: Final[tuple[OpSpec, ...]] = CORE + MACROS
_BY_NAME: Final[dict[str, OpSpec]] = {s.name: s for s in ALL}


def core_names() -> frozenset[str]:
    """The 20 executable core op names (sugar excluded)."""
    return frozenset(s.name for s in CORE if s.kind == "core")


def sugar_names() -> frozenset[str]:
    """Sugar spellings that share a writer handler with a core op."""
    return frozenset(s.name for s in CORE if s.kind == "sugar")


def macro_names() -> frozenset[str]:
    """Compound ops expanded to core ops before validation."""
    return frozenset(s.name for s in MACROS)


def op_names() -> frozenset[str]:
    """Everything the writer executes: core + sugar (``ops.OP_NAMES``)."""
    return core_names() | sugar_names()


def by_name(name: str) -> OpSpec:
    """The spec for one op or macro; ``KeyError`` if the name is unknown."""
    return _BY_NAME[name]
