"""Placement planner for a converted LCSC part (rank 76).

``akcli jlc add --place`` turns a successful EasyEDA->KiCad conversion into a
one-op ``place_component`` op-list.  The identifiers are READ BACK from the
produced artifacts rather than guessed: the converter names the symbol after
the component (not the C-number), and its ``Value`` property carries the real
part value where the enrichment record only knows the MPN.

Planner only — writing ``place.json`` and printing the follow-up ``akcli draw``
line stay in the CLI layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

__all__ = ["build_place_oplist", "read_symbol_name", "read_symbol_value"]


def read_symbol_name(kicad_sym_path: str) -> str | None:
    """Read the first symbol id from a produced ``.kicad_sym`` (don't guess from files)."""
    try:
        from ..readers import kicad_lib  # lazy
        lib = kicad_lib.read(kicad_sym_path)
    except Exception:
        return None
    return lib.symbols[0].name if lib.symbols else None


def read_symbol_value(kicad_sym_path: str) -> str | None:
    """Read the first symbol's ``Value`` property from a produced ``.kicad_sym``.

    The converter writes the part's actual value there (``10k``, ``100n``) when
    EasyEDA carries one, and falls back to the MPN otherwise — so this is the
    authoritative Value for a placement, ahead of the enrichment MPN/title.
    """
    try:
        from .. import sexpr  # lazy
        root = sexpr.parse(Path(kicad_sym_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    for sym in root.find_all("symbol"):
        for prop in sym.find_all("property"):
            kids = prop.children or []
            if len(kids) >= 3 and kids[1].value == "Value":
                val = (kids[2].value or "").strip()
                return val or None
    return None


def build_place_oplist(result: Any, *, lib_name: str, footprint_lib: str,
                       designator: str, at: Sequence[float],
                       value: str | None) -> dict[str, Any] | None:
    """Build a one-op ``place_component`` op-list from a successful conversion.

    ``lib_id``'s symbol name is read from the produced ``.kicad_sym`` (the converter
    names by component name, not the C-number); the footprint id comes from the
    produced ``.kicad_mod`` stem. Returns ``None`` if no symbol artifact was produced.
    """
    from ..ops import PROTOCOL_VERSION

    sym_art = next((a for a in result.artifacts if a.endswith(".kicad_sym")), None)
    if sym_art is None:
        return None
    sym_name = read_symbol_name(sym_art)
    if not sym_name:
        return None
    fp_art = next((a for a in result.artifacts if a.endswith(".kicad_mod")), None)
    fp_name = Path(fp_art).stem if fp_art else sym_name

    # The converted symbol's own Value wins: it carries the part's actual value
    # ("10k") when EasyEDA has one, where the enrichment fallback only knows the
    # MPN. Keeps the placed instance's Value consistent with the library symbol.
    value = read_symbol_value(sym_art) or value

    x_mil, y_mil = at
    op: dict[str, Any] = {
        "op": "place_component",
        "lib_id": f"{lib_name}:{sym_name}",
        "designator": designator,
        "x_mil": float(x_mil),
        "y_mil": float(y_mil),
        "footprint": f"{footprint_lib}:{fp_name}",
    }
    if value:
        op["value"] = value
    return {
        "protocol_version": PROTOCOL_VERSION,
        "target_format": "kicad",
        "ops": [op],
    }
