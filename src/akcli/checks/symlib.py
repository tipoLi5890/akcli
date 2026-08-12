"""Symbol-library geometry lint (``akcli library lint-symbols``).

A ``.kicad_sym`` is where a part's electrical truth lives, and two of its
failure modes are invisible everywhere downstream:

* **Two pins on one coordinate.** KiCad treats a symbol's pin connection point
  as a node: place an unused NC pin exactly where the GND pin already sits and
  every placement of that part silently shorts them. The schematic looks right,
  the netlist is wrong, and ERC has nothing to complain about — the *symbol*
  told it those pins are the same node. This is the rule that pays for the
  module: ``SYMLIB_PIN_COINCIDENT`` is an ERROR, and ``library new-symbol``
  refuses to write output that trips it.
* **Unset electrical types.** KiCad's ERC decides pin conflicts from a
  type-vs-type matrix. ``unspecified``/``free`` pins are compatible with
  everything, so an imported or hand-built symbol full of them turns ERC into
  a rubber stamp on those nets — while any *genuine* driver conflict elsewhere
  is buried under the noise the same symbols generate.

Plus the ordinary hygiene rule that keeps a symbol usable at all:

* **Off-grid pins.** KiCad's schematic editor works on a 100/50-mil grid; a pin
  whose connection point is off the 100-mil grid cannot be reached by a wire
  drawn on that grid, so it reads as connected and behaves as floating.

Multi-unit symbols are handled with :mod:`akcli.readers.kicad_lib`'s unit
semantics: a placed instance is ONE unit, exposing that unit's pins plus the
``_0_*`` pins common to every unit. Coincidence is therefore evaluated per
unit — two units' pins routinely share coordinates by design (that is what
drawing four gates on one body means) and must never be reported.

All findings are advisory here (``lint-symbols`` is a lint); the one place
``SYMLIB_PIN_COINCIDENT`` gates a write is ``library new-symbol --apply``,
which treats it as connectivity, not geometry.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import model, units
from .. import sexpr
from ..readers import kicad_lib
from ..report import Finding, Severity, anchor

SYMLIB_PIN_COINCIDENT = "SYMLIB_PIN_COINCIDENT"          # ERROR: one point, two pins
SYMLIB_PIN_TYPE_UNSPECIFIED = "SYMLIB_PIN_TYPE_UNSPECIFIED"  # NOTE: ERC-blind pin
SYMLIB_PIN_OFF_GRID = "SYMLIB_PIN_OFF_GRID"              # WARNING: unreachable by wire

#: The schematic-editor grid a symbol pin must land on.
GRID_MIL = 100
_GRID_NM = GRID_MIL * units.NM_PER_MIL

__all__ = [
    "SYMLIB_PIN_COINCIDENT",
    "SYMLIB_PIN_TYPE_UNSPECIFIED",
    "SYMLIB_PIN_OFF_GRID",
    "GRID_MIL",
    "run",
    "run_text",
    "run_library",
    "run_symbol",
]


def _fmt(x: float, y: float) -> str:
    def r(v: float) -> str:
        iv = round(v)
        return str(int(iv)) if abs(v - iv) < 0.01 else f"{v:.2f}"
    return f"({r(x)},{r(y)})"


def _pin_label(sym_name: str, pin: model.Pin) -> str:
    return f"{sym_name}.{pin.number}"


def _units_of(symdef: model.SymbolDef) -> list[int]:
    return list(range(1, max(1, symdef.part_count) + 1))


def run_symbol(symdef: model.SymbolDef) -> list[Finding]:
    """Lint ONE symbol definition; returns findings in a stable order."""
    findings: list[Finding] = []
    name = symdef.name

    # (a) coincident pins, evaluated per placed unit (see module docstring).
    reported: set[tuple[str, ...]] = set()
    for unit in _units_of(symdef):
        seats: dict[tuple[int, int], list[model.Pin]] = {}
        for pin in kicad_lib.unit_pins(symdef, unit):
            key = (units.mil_to_nm(pin.x_mil), units.mil_to_nm(pin.y_mil))
            seats.setdefault(key, []).append(pin)
        for _key, sharers in sorted(seats.items()):
            if len(sharers) < 2:
                continue
            names = {p.name or "" for p in sharers}
            types = {p.electrical_type for p in sharers}
            if len(names) == 1 and len(types) == 1:
                continue                    # KiCad's sanctioned stacked pins
            ident = tuple(sorted(p.number for p in sharers))
            if ident in reported:
                continue                    # a common (_0_*) pin repeats per unit
            reported.add(ident)
            first = sharers[0]
            detail = ", ".join(
                f"{p.number} ({p.name or '~'}/{p.electrical_type.value})"
                for p in sharers)
            findings.append(Finding(
                SYMLIB_PIN_COINCIDENT, Severity.ERROR,
                f"{name}: pins {detail} share the connection point "
                f"{_fmt(first.x_mil, first.y_mil)}"
                + (f" on unit {unit}" if symdef.part_count > 1 else "")
                + " — KiCad joins pins that share a point into ONE node, so "
                "every placement of this symbol shorts them together with no "
                "ERC violation; move one pin, or (if they really are one node) "
                "give them the same name AND the same electrical type so it "
                "reads as KiCad's stacked-pin convention",
                refs=[_pin_label(name, p) for p in sharers],
                anchors=[anchor("pin", _pin_label(name, p)) for p in sharers],
            ))

    # (b) pins whose electrical type is unset -> ERC pin-conflict blindness.
    blind = [p for p in symdef.pins
             if p.electrical_type is model.PinType.UNSPECIFIED]
    if blind:
        shown = ", ".join(p.number for p in blind[:8])
        more = f" (+{len(blind) - 8} more)" if len(blind) > 8 else ""
        findings.append(Finding(
            SYMLIB_PIN_TYPE_UNSPECIFIED, Severity.NOTE,
            f"{name}: {len(blind)} pin(s) carry the unspecified electrical type "
            f"({shown}{more}) — KiCad's ERC decides pin conflicts from a "
            "type-vs-type matrix in which 'unspecified' conflicts with nothing, "
            "so these pins are exempt from every driver/input rule and the "
            "noise they add hides real conflicts elsewhere; set each pin's real "
            "type (input/output/power_in/passive/...)",
            refs=[_pin_label(name, p) for p in blind],
            anchors=[anchor("pin", _pin_label(name, p)) for p in blind],
        ))

    # (c) off-grid pins: unreachable by a wire drawn on the editor grid.
    for pin in symdef.pins:
        nx, ny = units.mil_to_nm(pin.x_mil), units.mil_to_nm(pin.y_mil)
        if nx % _GRID_NM == 0 and ny % _GRID_NM == 0:
            continue
        findings.append(Finding(
            SYMLIB_PIN_OFF_GRID, Severity.WARNING,
            f"{name}: pin {pin.number} connects at "
            f"{_fmt(pin.x_mil, pin.y_mil)}, which is not on the "
            f"{GRID_MIL}-mil grid — a wire drawn on the editor grid can never "
            "land on it, so the pin looks wired and behaves as floating; snap "
            f"the pin to a {GRID_MIL}-mil multiple",
            refs=[_pin_label(name, pin)],
            anchors=[anchor("pin", _pin_label(name, pin))],
        ))
    return findings


def run_library(lib: model.Library) -> list[Finding]:
    """Lint every symbol of a :class:`akcli.model.Library`.

    ``(extends ...)`` stubs are skipped: their pins belong to the base symbol,
    which is linted on its own, and re-reporting them would double every
    finding of a derived part.
    """
    findings: list[Finding] = []
    for symdef in lib.symbols:
        if symdef.extends and not symdef.pins:
            continue
        findings.extend(run_symbol(symdef))
    return findings


def run_text(text: str, where: str = "<generated>") -> list[Finding]:
    """Lint ``.kicad_sym`` TEXT (used to gate ``new-symbol`` on its own output).

    A ``(kicad_symbol_lib ...)`` root's direct ``(symbol ...)`` children have
    exactly the shape :func:`kicad_lib.symbols_from_lib_symbols` reads out of an
    inline ``(lib_symbols ...)`` cache, so the reader is reused rather than
    duplicated — the lint can never disagree with how akcli parses the file.
    """
    root = sexpr.parse(text)
    lib = model.Library(
        source_path=where, source_format="kicad",
        symbols=kicad_lib.symbols_from_lib_symbols(root),
    )
    return run_library(lib)


def run(path: os.PathLike | str) -> list[Finding]:
    """Lint one ``.kicad_sym`` file; returns findings."""
    p = Path(os.fspath(path))
    if p.suffix.lower() != ".kicad_sym":
        return [Finding(
            SYMLIB_PIN_COINCIDENT, Severity.INFO,
            "symbol-library lint supports .kicad_sym only; skipped",
            refs=[str(p)],
        )]
    return run_library(kicad_lib.read(p))
