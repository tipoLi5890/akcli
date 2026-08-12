"""Engineering-notation number parsing and formatting.

Accepts the forms engineers actually type: ``4700``, ``4.7k``, ``4k7``,
``100n``, ``2M2``, ``1e-7``, ``3µ3``. Multiplier letters follow the SI
prefixes and are **case-insensitive except for the one pair where case is the
only information there is**: ``m`` = milli, ``M`` = mega. Everything else
accepts either case (``100N`` = ``100n``, ``10U`` = ``10u``, ``1T`` = ``1t``),
because SPICE is case-insensitive and Altium/KiCad libraries routinely carry
upper-cased values — rejecting ``10U`` only ever meant refusing a value the
simulator itself understands. Both micro signs are accepted: U+00B5 MICRO SIGN
(``µ``) and U+03BC GREEK SMALL LETTER MU (``μ``). ``R``/``r`` is the IEC 60062
decimal marker (``4R7`` = 4.7).

Parsing is deliberately *strict* about what a plain number may be: Python's
``float()`` also accepts ``inf``, ``nan``, digit separators (``1_000`` -> 1000)
and silently overflows ``1e400`` to infinity. None of those are values a
schematic or a SPICE deck can mean, and letting them through produced confident
wrong answers downstream (ngspice reads ``1_000`` as *1*), so they are rejected
here rather than parsed.

Reference: SI prefixes per BIPM, *The International System of Units (SI)*,
9th ed. (2019); ``R`` decimal marking per IEC 60062:2016.
"""

from __future__ import annotations

import math
import re

from .. import si_prefix

__all__ = ["parse_value", "parse_plain", "fmt_eng"]

# Canonical prefix meanings. ``m``/``M`` are the *only* case-significant entry
# (milli vs mega); every other ASCII letter below is mirrored to its other case
# by ``_build_mult``. The micro signs are excluded from that mirroring on
# purpose: "µ".upper() is Greek capital Mu, which would alias micro onto a
# mega-looking character.
_BASE_MULT = si_prefix.BASE_MULT          # the ONE prefix->multiplier table
_MULT = si_prefix.expand_case()

_RX = re.compile(r"^([0-9]*\.?[0-9]+)\s*(["
                 + re.escape("".join(sorted(_MULT)))
                 + r"])([0-9]*)$")

# A plain decimal/scientific number — and nothing else. Notably excludes
# ``inf``/``nan``/``infinity`` and the ``1_000`` digit-separator form that
# ``float()`` would happily accept.
_PLAIN_RX = re.compile(r"^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")


def parse_plain(s: str) -> float | None:
    """Parse ``s`` as a plain finite number, or ``None`` if it is not one."""
    if not _PLAIN_RX.match(s):
        return None
    try:
        val = float(s)
    except ValueError:  # pragma: no cover - regex already guarantees this
        return None
    return val if math.isfinite(val) else None  # 1e400 overflows to inf


def _finite(value: float, name: str, original: object) -> float:
    """Reject ``nan``/``inf`` inputs at the boundary.

    ``float("nan")`` and ``float("inf")`` parse happily, so ``calc rc r=nan``
    used to run the formula and put a bare ``NaN`` literal — which RFC 8259
    does not allow — into the ``--json`` envelope. There is no physical
    quantity these denote, so the honest answer is a refusal at the input, not
    a non-finite number downstream.
    """
    if not math.isfinite(value):
        from .registry import CalcError
        raise CalcError(f"{name}: {original!r} is not a finite number "
                        "(nan/inf are not physical quantities)")
    return value


def parse_value(text: str | float | int, name: str = "value",
                unit: str = "") -> float:
    """Parse ``text`` into a float, accepting engineering notation.

    ``unit`` is the calculator's *declared* unit for this field (e.g.
    ``"mAh"``, ``"mA"``). When that unit is itself milli-denominated, a bare
    ``m`` suffix is rejected rather than silently applying a second,
    compounding milli — ``capacity=2000m`` on a field already in mAh would
    otherwise mean 2 mAh, a 1000x error from what was almost certainly meant.
    """
    if isinstance(text, (int, float)):
        return _finite(float(text), name, text)
    s = str(text).strip()
    plain = parse_plain(s)
    if plain is not None:
        return plain
    m = _RX.match(s)
    if not m:
        from .registry import CalcError
        raise CalcError(
            f"{name}: cannot parse {text!r} (accepted: a plain number "
            "'4700'/'1e-7', an SI prefix 'p n u µ m k M G T' — case-insensitive "
            "except m=milli vs M=mega — as '4.7k'/'100N'/'10U', an embedded "
            "decimal '4k7'/'2M2', or the IEC 60062 R marker '4R7')")
    head, letter, tail = m.groups()
    if letter == "m" and unit.startswith("m") and unit not in ("", "m"):
        from .registry import CalcError
        base_unit = unit[1:]  # e.g. "mAh" -> "Ah"
        raise CalcError(
            f"{name} is already in {unit} — write {name}={head}{tail}, "
            f"or {base_unit}-style values are not supported")
    val = float(head + ("." + tail if tail else ""))
    # e.g. 400 digits followed by 'T' scales past float range
    return _finite(val * _MULT[letter], name, text)


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
