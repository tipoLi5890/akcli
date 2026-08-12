"""Pin table -> symbol geometry (``akcli library new-symbol``): validate + lay out.

Authoring a wide IC symbol by hand-writing S-expressions is the one part of the
job an agent reliably gets wrong: the pin *table* comes out of a datasheet
faithfully, the *coordinates* do not. A single mis-typed ``(at ...)`` puts two
pins on one point, and KiCad joins them into one node — an electrical mis-join
that no ERC run reports, because as far as the netlist is concerned the symbol
says those pins are the same node.

This module is the pure half of the fix: it takes a declarative pin table (the
``symbol-pins.schema.json`` document) and computes an unambiguous placement.
Rendering lives in :mod:`.writers.symbol_lib`; the coincidence lint that gates
the result (and audits libraries akcli did not write) lives in
:mod:`.checks.symlib`.

Design rules
------------
* **Nothing is guessed.** ``side`` and ``type`` are REQUIRED per pin. A symbol
  whose electrical types were left unset is the known source of KiCad
  ERC pin-conflict noise, so the generator refuses to invent them.
* **Everything lands on the 100-mil grid.** Pin connection points are integer
  multiples of :data:`GRID_MIL`; the body rectangle is derived from them, never
  the other way round.
* **One coordinate = one pin**, unless the spec explicitly says otherwise via
  ``stack_with`` (which additionally requires the same name and the same
  electrical type — KiCad's sanctioned stacked-pin convention). Any other
  coincidence is a bug, and :func:`layout` refuses rather than emit it.
* **Deterministic.** Identical input yields identical geometry: ordering is
  ``group`` first-appearance, then spec order, and every coordinate is integer
  mils.

Coordinates here use the KiCad **library** convention: mils, origin at the
symbol anchor, **+Y up** (the same frame :func:`akcli.readers.kicad_lib.pin_offsets`
reports). ``orientation`` is the lib-frame angle the pin points *from its
electrical tip toward the body* — the same convention as
:class:`akcli.model.Pin`.
"""

from __future__ import annotations

from . import contract

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import model
from .errors import AkcliError, fail

__all__ = [
    "PROTOCOL_VERSION",
    "SIDES",
    "PIN_TYPES",
    "GRID_MIL",
    "PIN_LENGTH_MIL",
    "PinSpec",
    "SymbolSpec",
    "PlacedPin",
    "Layout",
    "load",
    "parse_spec",
    "layout",
]

PROTOCOL_VERSION = contract.PROTOCOL_VERSIONS["symbol-spec"]

#: The four body edges a pin may be attached to. Never inferred from the name.
SIDES: tuple[str, ...] = ("left", "right", "top", "bottom")

#: Accepted ``type`` values = KiCad's own electrical-type grammar tokens (the
#: keys of :data:`akcli.model.KICAD_PINTYPE`). The token is emitted verbatim,
#: so ``free`` and ``unspecified`` stay distinguishable in the written file
#: even though the normalized model folds both onto ``PinType.UNSPECIFIED``.
PIN_TYPES: tuple[str, ...] = tuple(sorted(model.KICAD_PINTYPE))

# --- Geometry constants (mils) ---------------------------------------------
GRID_MIL = 100              # KiCad symbol pins live on the 100-mil grid
PIN_LENGTH_MIL = 100        # standard pin length
BODY_MARGIN_MIL = 100       # body edge beyond the outermost pin row/column
MIN_BODY_WIDTH_MIL = 400    # a body narrower than this reads as a glyph
NAME_OFFSET_MIL = 20        # (pin_names (offset 0.508)) — name inset from the edge
INNER_GAP_MIL = 100         # clear channel between the left and right name columns

# Stroke-font text metrics, shared with ``checks.layout``: KiCad's default
# field/pin text is 1.27 mm = 50 mil tall and advances ~0.9 * height per glyph.
TEXT_HEIGHT_MIL = 50.0
CHAR_ADVANCE_MIL = 0.9 * TEXT_HEIGHT_MIL

# side -> lib-frame angle pointing from the pin's electrical tip TOWARD the body
_SIDE_ANGLE: dict[str, int] = {"left": 0, "right": 180, "top": 270, "bottom": 90}

_TOP_KEYS = frozenset({"protocol_version", "name", "reference_prefix", "pins", "body"})
_PIN_KEYS = frozenset({"number", "name", "type", "side", "group", "hidden",
                       "stack_with"})
_BODY_KEYS = frozenset({"width_mil"})

# Keys that name a feature this MVP deliberately does not implement. They are
# recognized so the refusal says "not supported yet" instead of the much less
# actionable "unknown key".
_MULTI_UNIT_TOP_KEYS = ("units", "unit_count", "part_count")
_MULTI_UNIT_PIN_KEYS = ("unit", "unit_number", "owner_part_id")


# --------------------------------------------------------------------------- #
# spec model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PinSpec:
    """One row of the pin table, exactly as authored (no defaults invented)."""

    number: str
    name: str
    type: str
    side: str
    group: str = ""
    hidden: bool = False
    stack_with: str | None = None


@dataclass
class SymbolSpec:
    """A validated pin-table document."""

    name: str
    pins: list[PinSpec]
    reference_prefix: str = "U"
    body_width_mil: int | None = None
    protocol_version: int = PROTOCOL_VERSION


@dataclass(frozen=True)
class PlacedPin:
    """A :class:`PinSpec` with its computed lib-frame connection point (mils, +Y up)."""

    spec: PinSpec
    x_mil: int
    y_mil: int
    orientation: int
    length_mil: int = PIN_LENGTH_MIL

    @property
    def at(self) -> tuple[int, int]:
        return (self.x_mil, self.y_mil)


@dataclass
class Layout:
    """A fully placed symbol: body rectangle + every pin's connection point."""

    name: str
    reference_prefix: str
    pins: list[PlacedPin]
    body: tuple[int, int, int, int]          # (x0, y0, x1, y1) mils, +Y up
    body_width_overridden: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def body_width_mil(self) -> int:
        return self.body[2] - self.body[0]

    @property
    def body_height_mil(self) -> int:
        return self.body[3] - self.body[1]


# --------------------------------------------------------------------------- #
# load / validate
# --------------------------------------------------------------------------- #
def _bad(where: str, msg: str) -> None:
    fail("BAD_CONFIG", f"{where}: {msg}")


def _req_str(where: str, doc: dict, key: str, what: str) -> str:
    val = doc.get(key)
    if not isinstance(val, str) or not val.strip():
        _bad(where, f"{what} must be a non-empty string, got {val!r}")
    assert isinstance(val, str)   # _bad never returns
    return val


def _reject_multi_unit(where: str, doc: dict, keys: tuple[str, ...],
                       scope: str) -> None:
    present = [k for k in keys if k in doc]
    if present:
        _bad(where,
             f"{scope} key(s) {', '.join(present)}: multi-unit symbols are not "
             "supported yet — `library new-symbol` emits a single-unit symbol. "
             "Author one symbol per unit (or hand-edit the generated library) "
             "until multi-unit support lands")


def _parse_pin(where: str, raw: object, index: int) -> PinSpec:
    label = f"pins[{index}]"
    if not isinstance(raw, dict):
        _bad(where, f"{label} must be an object, got {type(raw).__name__}")
    assert isinstance(raw, dict)
    _reject_multi_unit(where, raw, _MULTI_UNIT_PIN_KEYS, f"{label}: per-pin")
    extra = set(raw) - _PIN_KEYS
    if extra:
        _bad(where, f"{label}: unknown key(s) {', '.join(sorted(map(str, extra)))} "
                    f"(expected {', '.join(sorted(_PIN_KEYS))})")

    number = _req_str(where, raw, "number", f"{label}.number")
    name = raw.get("name")
    if not isinstance(name, str):
        _bad(where, f"{label}.name must be a string (use \"\" for an unnamed pin), "
                    f"got {name!r}")
    assert isinstance(name, str)

    ptype = raw.get("type")
    if ptype not in PIN_TYPES:
        _bad(where, f"{label}.type {ptype!r} is not a KiCad electrical type — "
                    f"use one of: {', '.join(PIN_TYPES)}")
    assert isinstance(ptype, str)

    side = raw.get("side")
    if side not in SIDES:
        _bad(where, f"{label}.side {side!r} must be one of "
                    f"{', '.join(SIDES)} (a pin's side is never guessed)")
    assert isinstance(side, str)

    group = raw.get("group", "")
    if group is None:
        group = ""
    if not isinstance(group, str):
        _bad(where, f"{label}.group must be a string or null, got {group!r}")
    assert isinstance(group, str)

    hidden = raw.get("hidden", False)
    if not isinstance(hidden, bool):
        _bad(where, f"{label}.hidden must be true or false, got {hidden!r}")

    stack_with = raw.get("stack_with")
    if stack_with is not None and (not isinstance(stack_with, str)
                                   or not stack_with.strip()):
        _bad(where, f"{label}.stack_with must be another pin's number "
                    f"(a string) or null, got {stack_with!r}")

    return PinSpec(number=number, name=name, type=ptype, side=side,
                   group=group, hidden=bool(hidden),
                   stack_with=stack_with if isinstance(stack_with, str) else None)


def _validate_stacks(where: str, pins: list[PinSpec]) -> None:
    """Enforce the ONE sanctioned same-coordinate case (KiCad stacked pins)."""
    by_number = {p.number: p for p in pins}
    for p in pins:
        if p.stack_with is None:
            continue
        if p.stack_with == p.number:
            _bad(where, f"pin {p.number}: stack_with points at itself")
        base = by_number.get(p.stack_with)
        if base is None:
            _bad(where, f"pin {p.number}: stack_with {p.stack_with!r} names no "
                        "pin in this spec")
            return
        if base.stack_with is not None:
            _bad(where, f"pin {p.number}: stack_with {p.stack_with!r} is itself "
                        "stacked — stack every pin of a group onto the same "
                        "un-stacked base pin (chains are refused so the "
                        "coordinate has one owner)")
        if base.name != p.name or base.type != p.type:
            _bad(where,
                 f"pin {p.number} ({p.name!r}/{p.type}) cannot stack on pin "
                 f"{base.number} ({base.name!r}/{base.type}): KiCad's stacked-pin "
                 "convention requires the SAME name and the SAME electrical type "
                 "— two different pins on one coordinate is an electrical "
                 "mis-join, not a stack")
        if base.side != p.side:
            _bad(where, f"pin {p.number}: stack_with {p.stack_with!r} is on side "
                        f"{base.side!r}, not {p.side!r} — a stacked pin shares its "
                        "base's coordinate, so it must share its side")


def parse_spec(doc: object, where: str = "<spec>") -> SymbolSpec:
    """Validate an already-parsed pin-table document into a :class:`SymbolSpec`.

    Raises ``AkcliError('BAD_CONFIG', ...)`` naming the offending entry, or
    ``AkcliError('PROTOCOL_MISMATCH', ...)`` on a wrong ``protocol_version``.
    """
    if not isinstance(doc, dict):
        _bad(where, "spec root must be a JSON object")
    assert isinstance(doc, dict)

    _reject_multi_unit(where, doc, _MULTI_UNIT_TOP_KEYS, "top-level")
    extra = set(doc) - _TOP_KEYS
    if extra:
        _bad(where, f"unknown key(s) {', '.join(sorted(map(str, extra)))} "
                    f"(expected {', '.join(sorted(_TOP_KEYS))})")

    pv = doc.get("protocol_version")
    if pv != PROTOCOL_VERSION:
        fail("PROTOCOL_MISMATCH",
             f"{where}: symbol-spec protocol_version {pv!r} != {PROTOCOL_VERSION}")

    name = _req_str(where, doc, "name", "name")
    prefix = doc.get("reference_prefix", "U")
    if not isinstance(prefix, str) or not prefix.strip():
        _bad(where, f"reference_prefix must be a non-empty string, got {prefix!r}")
    assert isinstance(prefix, str)

    raw_pins = doc.get("pins")
    if not isinstance(raw_pins, list) or not raw_pins:
        _bad(where, "'pins' must be a non-empty array of pin objects — a symbol "
                    "with no pins connects nothing and is never what was meant")
    assert isinstance(raw_pins, list)

    pins = [_parse_pin(where, raw, i) for i, raw in enumerate(raw_pins)]
    seen: dict[str, int] = {}
    for i, p in enumerate(pins):
        if p.number in seen:
            _bad(where, f"duplicate pin number {p.number!r} (pins[{seen[p.number]}] "
                        f"and pins[{i}]) — pin numbers identify the package pad "
                        "and must be unique")
        seen[p.number] = i
    _validate_stacks(where, pins)

    body = doc.get("body")
    width: int | None = None
    if body is not None:
        if not isinstance(body, dict):
            _bad(where, f"'body' must be an object or null, got {body!r}")
        assert isinstance(body, dict)
        body_extra = set(body) - _BODY_KEYS
        if body_extra:
            _bad(where, f"body: unknown key(s) "
                        f"{', '.join(sorted(map(str, body_extra)))} "
                        f"(expected {', '.join(sorted(_BODY_KEYS))})")
        raw_w = body.get("width_mil")
        if raw_w is not None:
            if isinstance(raw_w, bool) or not isinstance(raw_w, (int, float)):
                _bad(where, f"body.width_mil must be a positive number or null, "
                            f"got {raw_w!r}")
            if raw_w <= 0:
                _bad(where, f"body.width_mil must be > 0, got {raw_w!r}")
            width = int(math.ceil(float(raw_w)))

    return SymbolSpec(name=name, pins=pins, reference_prefix=prefix,
                      body_width_mil=width, protocol_version=PROTOCOL_VERSION)


def load(path: os.PathLike | str) -> SymbolSpec:
    """Load and validate a pin-table JSON file (see :func:`parse_spec`).

    ``FileNotFoundError`` propagates (the CLI maps it to exit 4).
    """
    p = Path(os.fspath(path))
    text = p.read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AkcliError(
            "BAD_CONFIG", f"invalid symbol-spec JSON in {p}: {exc}") from exc
    return parse_spec(doc, str(p))


# --------------------------------------------------------------------------- #
# layout
# --------------------------------------------------------------------------- #
def _round_up(value: float, step: int) -> int:
    return int(math.ceil(value / step)) * step


def _text_width_mil(name: str) -> float:
    """Estimated drawn width of a pin name (same stroke-font metric as ``checks.layout``)."""
    return len(name) * CHAR_ADVANCE_MIL


def _ordered_rows(spec: SymbolSpec) -> dict[str, list[PinSpec]]:
    """Row/column order per side: by ``group`` first appearance, then spec order.

    Stacked pins do not occupy a row of their own — they inherit their base
    pin's coordinate — so only un-stacked pins are laid out here.
    """
    group_rank: dict[str, int] = {}
    for p in spec.pins:
        group_rank.setdefault(p.group, len(group_rank))
    spec_rank = {p.number: i for i, p in enumerate(spec.pins)}

    rows: dict[str, list[PinSpec]] = {s: [] for s in SIDES}
    for p in spec.pins:
        if p.stack_with is None:
            rows[p.side].append(p)
    for side in SIDES:
        rows[side].sort(key=lambda p: (group_rank[p.group], spec_rank[p.number]))
    return rows


def _centered_start(count: int) -> int:
    """First coordinate of ``count`` grid slots centered on the origin.

    For an odd count the run is exactly centered; for an even one it is biased
    by half a pitch toward +, because keeping every pin on the 100-mil grid
    outranks a 50-mil symmetry (KiCad's own libraries make the same trade —
    e.g. a 4-pin side runs +100, 0, -100, -200).
    """
    return ((count - 1) // 2) * GRID_MIL if count else 0


def layout(spec: SymbolSpec) -> Layout:
    """Place every pin of ``spec`` and size the body rectangle around them.

    Raises ``AkcliError('INTERNAL', ...)`` if the placement it computed puts two
    non-stacked pins on one coordinate — that would be a bug in this function,
    and shipping it would be the exact electrical mis-join the generator exists
    to prevent.
    """
    rows = _ordered_rows(spec)
    n_left, n_right = len(rows["left"]), len(rows["right"])
    n_top, n_bottom = len(rows["top"]), len(rows["bottom"])
    n_vert = max(n_left, n_right)
    n_horiz = max(n_top, n_bottom)

    # --- vertical: rows run downward from a centered start -------------------
    y_first = _centered_start(n_vert)
    y_last = y_first - (n_vert - 1) * GRID_MIL if n_vert else 0
    body_y1 = y_first + BODY_MARGIN_MIL
    body_y0 = y_last - BODY_MARGIN_MIL

    # --- horizontal: wide enough for both name columns AND the top/bottom run -
    def side_name_width(side: str) -> float:
        return max((_text_width_mil(p.name) for p in rows[side]), default=0.0)

    needed_names = (2 * NAME_OFFSET_MIL + INNER_GAP_MIL
                    + side_name_width("left") + side_name_width("right"))
    needed_cols = ((n_horiz - 1) * GRID_MIL + 2 * BODY_MARGIN_MIL
                   if n_horiz else 0)
    width = _round_up(max(MIN_BODY_WIDTH_MIL, needed_names, needed_cols),
                      2 * GRID_MIL)
    overridden = False
    if spec.body_width_mil is not None:
        wanted = _round_up(spec.body_width_mil, 2 * GRID_MIL)
        overridden = wanted > width
        # An override may only WIDEN: shrinking below the geometric minimum
        # would push top/bottom pins onto the left/right pin columns, i.e.
        # manufacture the coincidence this module exists to make impossible.
        width = max(width, wanted)
    half_w = width // 2

    x_first = -_centered_start(n_horiz)
    body_x0, body_x1 = -half_w, half_w

    placed: dict[str, tuple[int, int, int]] = {}
    for k, p in enumerate(rows["left"]):
        placed[p.number] = (body_x0 - PIN_LENGTH_MIL, y_first - k * GRID_MIL,
                            _SIDE_ANGLE["left"])
    for k, p in enumerate(rows["right"]):
        placed[p.number] = (body_x1 + PIN_LENGTH_MIL, y_first - k * GRID_MIL,
                            _SIDE_ANGLE["right"])
    for j, p in enumerate(rows["top"]):
        placed[p.number] = (x_first + j * GRID_MIL, body_y1 + PIN_LENGTH_MIL,
                            _SIDE_ANGLE["top"])
    for j, p in enumerate(rows["bottom"]):
        placed[p.number] = (x_first + j * GRID_MIL, body_y0 - PIN_LENGTH_MIL,
                            _SIDE_ANGLE["bottom"])
    for p in spec.pins:                      # stacked pins inherit their base
        if p.stack_with is not None:
            placed[p.number] = placed[p.stack_with]

    pins = [PlacedPin(spec=p, x_mil=placed[p.number][0],
                      y_mil=placed[p.number][1],
                      orientation=placed[p.number][2])
            for p in spec.pins]

    out = Layout(name=spec.name, reference_prefix=spec.reference_prefix,
                 pins=pins, body=(body_x0, body_y0, body_x1, body_y1),
                 body_width_overridden=overridden)
    if spec.body_width_mil is not None and not overridden:
        out.warnings.append(
            f"body.width_mil {spec.body_width_mil} is narrower than the "
            f"{width}-mil minimum required by the pin names and pin count — "
            "kept the minimum (an override may only widen the body)")
    _assert_no_unsanctioned_coincidence(out)
    return out


def _assert_no_unsanctioned_coincidence(out: Layout) -> None:
    """Self-check: one coordinate, one pin — unless the spec stacked them."""
    seats: dict[tuple[int, int], list[PlacedPin]] = {}
    for pp in out.pins:
        seats.setdefault(pp.at, []).append(pp)
    for at, sharers in sorted(seats.items()):
        if len(sharers) < 2:
            continue
        names = {p.spec.name for p in sharers}
        types = {p.spec.type for p in sharers}
        if len(names) == 1 and len(types) == 1:
            continue                        # sanctioned stack
        numbers = ", ".join(p.spec.number for p in sharers)
        fail("INTERNAL",
             f"symbol {out.name!r}: pins {numbers} were placed on one "
             f"coordinate {at} with differing name/type — refusing to emit an "
             "electrical mis-join (this is an akcli layout bug; please report "
             "the spec)")
