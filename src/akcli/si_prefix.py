"""The ONE SI prefix→multiplier base table (leaf, rank 10).

``calc/si.py`` (engineering-notation values) and ``parts/value_parse.py``
(BOM value normalization) each carried a hand-written copy of the same
physics; a one-keystroke divergence (``1e-12`` vs ``1e-11``) would have been
a silent physical error with no cross-check.  The MULTIPLIER VALUES now live
here once; the two consumers keep their own — deliberately different —
tolerance policies and alphabet subsets:

* ``calc/si`` uses :func:`expand_case` (both cases accepted, ``m``/``M``
  kept distinct, unicode micro signs added) — the full ladder including
  tera;
* ``parts/value_parse`` derives a restricted table (no ``T``: ``4T7`` as a
  BOM part value is absurd and would silently parse as 4.7e12; uppercase
  only for ``K``) — see :data:`akcli.parts.value_parse._MULTIPLIERS`.

``tests/test_si_layering.py`` pins both the shared agreement and the
deliberate differences.
"""

from __future__ import annotations

__all__ = ["BASE_MULT", "MICRO_SIGNS", "expand_case"]

# Canonical prefix -> multiplier. "R" doubles as the ohms decimal mark in
# IEC 60062 notation (4R7 = 4.7 Ω).
BASE_MULT: dict[str, float] = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6,
    "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
    "R": 1.0,
}

# MICRO SIGN, GREEK SMALL LETTER MU — excluded from case mirroring on
# purpose: "µ".upper() is Greek capital Mu, which would alias micro onto a
# mega-looking character.
MICRO_SIGNS: tuple[str, str] = ("µ", "μ")


def expand_case(base: dict[str, float] | None = None) -> dict[str, float]:
    """Expand ``base`` to both cases, keeping ``m``/``M`` distinct."""
    out: dict[str, float] = {}
    for ch, mult in (base or BASE_MULT).items():
        out[ch] = mult
        if ch not in ("m", "M"):
            out[ch.swapcase()] = mult
    for sign in MICRO_SIGNS:
        out[sign] = 1e-6
    return out
