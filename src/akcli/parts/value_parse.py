"""Normalized numeric parsing of R/C/L value strings (SPEC §3.6, BOM P0-2).

The BOM suggestion engine used to grade candidates by SUBSTRING match
("1k" ⊂ "5.1k", "2.2u" visible inside a 22 µF description), which produced
real-world wrong-part incidents (5.1k -> 1k resistor, 2.2 µF -> 22 µF cap).
This module replaces string containment with physics: every spelling of a
value — ``5.1k`` / ``5k1`` / ``5.1kΩ`` / ``5100``, ``2.2u`` / ``2u2`` /
``2.2µF`` / ``2200nF``, ``8p C0G`` — parses to a canonical float in base
units (Ω, F, H) plus a unit class and qualifiers (tolerance / voltage /
dielectric), and equality is strict numeric equality (cross-unit spellings of
the same value compare equal; everything else does not).

Parsing is deliberately conservative: a string this module cannot understand
returns ``None``, and callers must treat ``None`` as "cannot verify", never
as a match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["ParsedValue", "parse", "extract_values", "same_value",
           "unit_class_of_ref"]

# Decimal multiplier per prefix letter. "R" doubles as the ohms decimal mark
# in IEC 60062 notation (4R7 = 4.7 Ω); µ/μ are the unicode micro spellings.
_MULTIPLIERS: dict[str, float] = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3, "R": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
}

# Unit suffix -> unit class. Bare "R"/ohm spellings pin the class to R the
# same way F pins C and H pins L.
_UNIT_CLASS: dict[str, str] = {
    "Ω": "R", "Ω".casefold(): "R", "ohm": "R", "ohms": "R", "r": "R",
    "f": "C", "h": "L",
}

_DIELECTRICS: frozenset[str] = frozenset({
    "C0G", "NP0", "X5R", "X7R", "X7S", "X6S", "Y5V", "Z5U",
})

# value token, suffix style:  5.1k · 5.1kΩ · 100nF · 8p · 5100 · 0.1uF
_SUFFIX_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*([pnuµμmRkKMG])?\s*(Ω|ohms?|[FfHh])?(?![\w.])",
    re.UNICODE)
# value token, IEC infix style:  5k1 · 2u2 · 4R7 · 8p2 · 4n7H
_INFIX_RE = re.compile(
    r"(?<![\w.])(\d+)([pnuµμmRkKMG])(\d+)\s*(Ω|ohms?|[FfHh])?(?![\w.])",
    re.UNICODE)

_TOLERANCE_RE = re.compile(r"±?\s*(\d+(?:\.\d+)?)\s*%")
_VOLTAGE_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*[Vv](?![\w.])")


@dataclass(frozen=True)
class ParsedValue:
    """One R/C/L value in canonical base units (Ω / F / H)."""

    value: float
    unit_class: str                 # "R" | "C" | "L" | "" (unknown)
    tolerance: str | None = None    # e.g. "1%"
    voltage: str | None = None      # e.g. "25V"
    dielectric: str | None = None   # e.g. "C0G"


def unit_class_of_ref(ref: str | None) -> str:
    """The unit class a refdes prefix implies (``R7`` -> R, ``C3`` -> C)."""
    prefix = re.match(r"[A-Za-z]+", (ref or "").strip())
    head = (prefix.group(0) if prefix else "").upper()
    if head in ("R", "RN", "RV"):
        return "R"
    if head == "C":
        return "C"
    if head == "L" or head == "FB":
        return "L"
    return ""


def _class_of_multiplier(mult: str, hint: str) -> str:
    # The "R" decimal mark is itself an ohms claim, and k/M/G only exist for
    # resistance (there are no kilofarads/kilohenries on a BOM); the small
    # prefixes (p/n/u/m) say nothing about the unit class.
    if mult == "R":
        return "R"
    if mult in ("k", "K", "M", "G"):
        return "R"
    return hint


def _qualifiers(text: str) -> tuple[str | None, str | None, str | None]:
    tol = _TOLERANCE_RE.search(text)
    volt = _VOLTAGE_RE.search(text)
    diel: str | None = None
    for token in re.split(r"[\s,/;]+", text):
        if token.upper() in _DIELECTRICS:
            diel = "NP0" if token.upper() == "NP0" else token.upper()
            # C0G and NP0 are the same dielectric; canonicalize to C0G.
            if diel == "NP0":
                diel = "C0G"
            break
    return (
        f"{tol.group(1)}%" if tol else None,
        f"{volt.group(1)}V" if volt else None,
        diel,
    )


def parse(text: str | None, *, hint: str = "") -> ParsedValue | None:
    """Parse ONE value string (a schematic ``Value`` field) or return ``None``.

    ``hint`` is the unit class implied by the refdes prefix ("R"/"C"/"L");
    it resolves spellings that omit the unit ("5.1k" on a resistor,
    "100n" on a cap) and lets a bare number ("5100") parse as ohms — bare
    numbers WITHOUT an R hint stay unparseable (a bare "2200" on a cap is
    ambiguous between pF conventions; guessing is how wrong parts happen).
    """
    s = (text or "").strip()
    if not s:
        return None
    tol, volt, diel = _qualifiers(s)

    m = _INFIX_RE.search(s)
    if m:
        whole, mult, frac, unit = m.groups()
        value = float(f"{whole}.{frac}") * _MULTIPLIERS[mult]
        klass = (_UNIT_CLASS.get(unit.casefold(), hint) if unit
                 else _class_of_multiplier(mult, hint))
        return ParsedValue(value, klass, tol, volt, diel)

    best: ParsedValue | None = None
    for m in _SUFFIX_RE.finditer(s):
        num, mult, unit = m.group(1), m.group(2), m.group(3)
        if unit is None and mult is None and best is not None:
            continue                     # plain trailing numbers lose to a real hit
        klass = hint
        if unit is not None:
            klass = _UNIT_CLASS.get(unit.casefold(), hint)
        if mult is not None:
            klass = klass or _class_of_multiplier(mult, hint)
            if mult == "R":
                klass = "R"
        value = float(num) * (_MULTIPLIERS[mult] if mult else 1.0)
        if unit is None and mult is None:
            # bare number: only safe with an explicit R hint (plain ohms)
            if hint != "R":
                continue
            klass = "R"
        cand = ParsedValue(value, klass, tol, volt, diel)
        # prefer the token that actually names a unit over a bare number
        if best is None or (unit is not None and best.unit_class == ""):
            best = cand
        if unit is not None or mult is not None:
            return cand if best is None else best if best.unit_class else cand
    return best


def extract_values(text: str | None, *, unit_class: str) -> list[ParsedValue]:
    """Every value of ``unit_class`` found in free text (catalog descriptions).

    Catalog descriptions carry many numbers ("22uF ±20% 25V X5R 0805");
    only tokens whose spelling proves the requested unit class count —
    a bare "25" or the "0805" package never becomes a candidate value.
    """
    s = (text or "").strip()
    if not s:
        return []
    tol, volt, diel = _qualifiers(s)
    out: list[ParsedValue] = []
    for m in _INFIX_RE.finditer(s):
        whole, mult, frac, unit = m.groups()
        klass = (_UNIT_CLASS.get(unit.casefold(), "") if unit
                 else _class_of_multiplier(mult, ""))
        value = float(f"{whole}.{frac}") * _MULTIPLIERS[mult]
        if klass == unit_class or (klass == "" and unit_class in ("C", "L")):
            out.append(ParsedValue(value, unit_class, tol, volt, diel))
    for m in _SUFFIX_RE.finditer(s):
        num, mult, unit = m.group(1), m.group(2), m.group(3)
        if unit is None:
            continue                     # free text: require an explicit unit
        klass = _UNIT_CLASS.get(unit.casefold(), "")
        if klass != unit_class:
            continue
        value = float(num) * (_MULTIPLIERS[mult] if mult else 1.0)
        out.append(ParsedValue(value, klass, tol, volt, diel))
    return out


def same_value(a: float, b: float, *, rel_tol: float = 1e-3) -> bool:
    """Strict numeric equality with float-rounding slack (2200nF == 2.2µF)."""
    if a == b:
        return True
    if a == 0.0 or b == 0.0:
        return False
    return abs(a - b) / max(abs(a), abs(b)) <= rel_tol
