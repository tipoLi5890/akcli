"""``symbol_spec.Layout`` -> ``.kicad_sym`` text (the ``library new-symbol`` target).

A string-template renderer in the :mod:`.footprint_mod` mould: it serializes an
already-computed placement and invents nothing. Every coordinate arrives in
integer mils and is stringified through :func:`akcli.units.nm_to_mm_str`
(integer-nanometre math), so the same :class:`~akcli.symbol_spec.Layout` always
produces the same bytes.

Grammar provenance (fixture-first rule, `tests/fixtures/kicad/symlib/`)
----------------------------------------------------------------------
Verified against a real ``kicad-cli`` 10.0.4 on the shapes emitted here:

* ``(version 20231120)`` — the KiCad 8 library format token. KiCad 8, 9 and 10
  all read it; emitting KiCad 10's ``20251024`` would lock the output out of
  KiCad 8/9 for no gain. ``kicad-cli sym upgrade`` rewrites it to 20251024
  with the pin set, types, positions and hidden flags unchanged.
* ``(hide yes)`` as a direct child of ``(pin ...)`` after ``(length ...)`` —
  confirmed semantically, not just syntactically: ``sym export svg`` omits such
  a pin and ``sym export svg --include-hidden-pins`` draws it.
* Property block: ``Reference`` / ``Value`` / ``Footprint`` / ``Datasheet``, with
  the KiCad 8 hide form ``(effects ... (hide yes))``.
* Pins live in a ``"<name>_1_1"`` sub-symbol and the body graphics in
  ``"<name>_0_1"`` — the unit/body-style split every KiCad library uses and
  :func:`akcli.readers.kicad_lib._collect_pins` reads back.
"""

from __future__ import annotations

from .. import units
from ..kicad_escape import escape_string
from ..symbol_spec import Layout, PlacedPin
from .sexpr_writer import quote

__all__ = ["to_kicad_sym", "FORMAT_VERSION"]

#: KiCad 8 symbol-library format token (see the module docstring for why).
FORMAT_VERSION = "20231120"
_GENERATOR_VERSION = "8.0"

_FONT_SIZE_MM = "1.27"
_PIN_NAME_OFFSET_MM = "0.508"       # 20 mil, the KLC inset for IC bodies
_BODY_STROKE_MM = "0.254"
_LABEL_GAP_MIL = 50                 # Reference/Value clear of the body edge


def _mm(mil: float) -> str:
    """Mils -> a KiCad mm literal, via integer nanometres (no float drift)."""
    return units.nm_to_mm_str(units.mil_to_nm(mil))


def _effects(indent: str, hidden: bool = False, justify: str = "") -> list[str]:
    out = [f"{indent}(effects",
           f"{indent}\t(font",
           f"{indent}\t\t(size {_FONT_SIZE_MM} {_FONT_SIZE_MM})",
           f"{indent}\t)"]
    if justify:
        out.append(f"{indent}\t(justify {justify})")
    if hidden:
        out.append(f"{indent}\t(hide yes)")
    out.append(f"{indent})")
    return out


def _property(name: str, value: str, x_mil: float, y_mil: float, *,
              hidden: bool = False, justify: str = "") -> list[str]:
    out = [f"\t\t(property {quote(name)} {quote(value)}",
           f"\t\t\t(at {_mm(x_mil)} {_mm(y_mil)} 0)"]
    out.extend(_effects("\t\t\t", hidden=hidden, justify=justify))
    out.append("\t\t)")
    return out


def _pin(pp: PlacedPin) -> list[str]:
    out = [f"\t\t\t(pin {pp.spec.type} line",
           f"\t\t\t\t(at {_mm(pp.x_mil)} {_mm(pp.y_mil)} {pp.orientation})",
           f"\t\t\t\t(length {_mm(pp.length_mil)})"]
    if pp.spec.hidden:
        out.append("\t\t\t\t(hide yes)")
    # KiCad draws "~" as "no name"; an empty string would render as a blank box.
    out.append(f"\t\t\t\t(name {quote(pp.spec.name or '~')}")
    out.extend(_effects("\t\t\t\t\t"))
    out.append("\t\t\t\t)")
    out.append(f"\t\t\t\t(number {quote(pp.spec.number)}")
    out.extend(_effects("\t\t\t\t\t"))
    out.append("\t\t\t\t)")
    out.append("\t\t\t)")
    return out


def to_kicad_sym(layout: Layout, *, generator: str = "akcli") -> str:
    """Render ``layout`` as a one-symbol ``.kicad_sym`` library.

    Deterministic: identical input yields identical bytes. Pins are emitted in
    spec order so the file reads like the pin table it came from.
    """
    x0, y0, x1, y1 = layout.body
    esc_name = escape_string(layout.name) or layout.name

    lines = ["(kicad_symbol_lib",
             f"\t(version {FORMAT_VERSION})",
             f"\t(generator {quote(generator)})",
             f"\t(generator_version {quote(_GENERATOR_VERSION)})",
             f"\t(symbol {quote(esc_name)}",
             "\t\t(pin_numbers",
             "\t\t\t(hide no)",
             "\t\t)",
             "\t\t(pin_names",
             f"\t\t\t(offset {_PIN_NAME_OFFSET_MM})",
             "\t\t)",
             "\t\t(exclude_from_sim no)",
             "\t\t(in_bom yes)",
             "\t\t(on_board yes)"]
    lines.extend(_property("Reference", layout.reference_prefix,
                           x0, y1 + _LABEL_GAP_MIL, justify="left bottom"))
    lines.extend(_property("Value", layout.name,
                           x0, y0 - _LABEL_GAP_MIL, justify="left top"))
    lines.extend(_property("Footprint", "", 0, 0, hidden=True))
    lines.extend(_property("Datasheet", "", 0, 0, hidden=True))

    lines.extend([
        f"\t\t(symbol {quote(esc_name + '_0_1')}",
        "\t\t\t(rectangle",
        f"\t\t\t\t(start {_mm(x0)} {_mm(y1)})",
        f"\t\t\t\t(end {_mm(x1)} {_mm(y0)})",
        "\t\t\t\t(stroke",
        f"\t\t\t\t\t(width {_BODY_STROKE_MM})",
        "\t\t\t\t\t(type default)",
        "\t\t\t\t)",
        "\t\t\t\t(fill",
        "\t\t\t\t\t(type background)",
        "\t\t\t\t)",
        "\t\t\t)",
        "\t\t)",
        f"\t\t(symbol {quote(esc_name + '_1_1')}",
    ])
    for pp in layout.pins:
        lines.extend(_pin(pp))
    lines.extend(["\t\t)", "\t)", ")"])
    return "\n".join(lines) + "\n"
