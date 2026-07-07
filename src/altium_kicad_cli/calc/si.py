"""Engineering-notation number parsing and formatting.

Accepts the forms engineers actually type: ``4700``, ``4.7k``, ``4k7``,
``100n``, ``2M2``, ``1e-7``, ``3µ3``. Multiplier letters follow the SI
prefixes (case-sensitive where it matters: ``m`` = milli, ``M`` = mega;
``k``/``K`` both accepted). ``R``/``r`` is the IEC 60062 decimal marker
(``4R7`` = 4.7).

Reference: SI prefixes per BIPM, *The International System of Units (SI)*,
9th ed. (2019); ``R`` decimal marking per IEC 60062:2016.
"""

from __future__ import annotations

import re

__all__ = ["parse_value", "fmt_eng"]

_MULT = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6,
    "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
    "R": 1.0, "r": 1.0,
}

_RX = re.compile(r"^([0-9]*\.?[0-9]+)\s*([pnuµmkKMGRr])([0-9]*)$")


def parse_value(text: str | float | int, name: str = "value") -> float:
    """Parse ``text`` into a float, accepting engineering notation."""
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip()
    try:
        return float(s)
    except ValueError:
        pass
    m = _RX.match(s)
    if not m:
        from .registry import CalcError
        raise CalcError(f"{name}: cannot parse {text!r} "
                        "(try 4700, 4.7k, 4k7, 100n, 1e-7)")
    head, letter, tail = m.groups()
    val = float(head + ("." + tail if tail else ""))
    return val * _MULT[letter]


_STEPS = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""),
          (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")]


def fmt_eng(value: float, unit: str = "", digits: int = 4) -> str:
    """Format ``value`` with an SI prefix: ``fmt_eng(4700, "Ω") -> '4.7 kΩ'``."""
    if value == 0:
        return f"0 {unit}".strip()
    a = abs(value)
    for scale, prefix in _STEPS:
        if a >= scale:
            v = value / scale
            return f"{v:.{digits}g} {prefix}{unit}".strip()
    return f"{value:.{digits}g} {unit}".strip()
