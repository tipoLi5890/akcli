"""Single source of truth for ALL coordinate/unit conversions and grid/tolerance.

Canonical internal model: origin top-left, +Y down, unit **mils**, default 50-mil grid.
The KiCad writer keeps geometry in integer nanometres and only stringifies at
serialize time (see :func:`nm_to_mm_str`).

LOCKED decision (SPEC §1.2): 1 Altium schematic integer Location unit = 10 mil,
with a companion ``*_Frac`` field in 1/100000 of that unit:
``mil = (intval + frac/100000.0) * 10.0``.
"""

from __future__ import annotations

# --- LOCKED unit constants (SPEC §3.1) --------------------------------------
ALTIUM_SCH_MIL_PER_UNIT: float = 10.0          # 1 Altium SCH int unit = 10 mil
MIL_PER_MM: float = 1.0 / 0.0254               # 39.3700787... mil per mm
NM_PER_MIL: int = 25_400                       # 1 mil = 25400 nm (exact)
NM_PER_MM: int = 1_000_000                     # 1 mm = 1e6 nm (exact)

ALTIUM_FRAC_DIVISOR: float = 100_000.0         # *_Frac sub-unit divisor


def altium_to_mil(i: int, frac: int = 0) -> float:
    """Convert an Altium schematic integer Location (+ optional ``_Frac``) to mils."""
    return (i + frac / ALTIUM_FRAC_DIVISOR) * ALTIUM_SCH_MIL_PER_UNIT


def mil_to_nm(m: float) -> int:
    """Convert mils to integer nanometres (rounded)."""
    return int(round(m * NM_PER_MIL))


def mm_to_nm(mm: float) -> int:
    """Convert millimetres to integer nanometres (rounded)."""
    return int(round(mm * NM_PER_MM))


def nm_to_mil(nm: int) -> float:
    """Convert integer nanometres to mils."""
    return nm / NM_PER_MIL


def nm_to_mm_str(nm: int) -> str:
    """Render integer nanometres as a KiCad-style mm float string.

    Strips trailing zeros and a trailing dot (``1000000`` -> ``"1"``,
    ``1270000`` -> ``"1.27"``, ``0`` -> ``"0"``). Integer-nm math only.
    """
    nm = int(nm)
    neg = nm < 0
    nm = -nm if neg else nm
    whole, frac = divmod(nm, NM_PER_MM)
    s = f"{whole}.{frac:06d}".rstrip("0").rstrip(".")
    if neg and s != "0":
        s = "-" + s
    return s


def snap_mil(m: float, grid: float = 50) -> float:
    """Snap a mil value to the nearest grid multiple (default 50-mil grid)."""
    if grid <= 0:
        return float(m)
    return round(m / grid) * grid


def approx_eq(a: float, b: float, tol_nm: int) -> bool:
    """True when ``a`` and ``b`` (both in nanometres) are within ``tol_nm``."""
    return abs(a - b) <= tol_nm
