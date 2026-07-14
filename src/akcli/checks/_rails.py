"""Shared rail-name heuristics for the ERC and power checks.

Kept in ONE place so the voltage-inference rules can't drift between ``erc.py`` and
``power.py`` (they used to carry identical copies — and an identical bug).

Boundary note: the trailing assertions use ``(?![0-9A-Za-z])`` rather than ``\\b``,
because ``_`` is a regex *word* character, so ``\\b`` does **not** treat ``V3V3_AUX``'s
trailing ``_`` as a boundary — that made underscore-suffixed rails (``V3V3_AUX``,
``V3V3_IO``) fail to register as power, producing false ``ERC_NO_POWER``. The
digit-``V``-digit pattern deliberately has *no* leading assertion: in ``V3V3`` the
matched ``3`` is preceded by ``V``, so a leading boundary would wrongly reject it.
"""

from __future__ import annotations

import re

_DIGIT_V_DIGIT = re.compile(r"(\d+)V(\d+)(?![0-9A-Za-z])")          # 3V3 / 1V8 / V3V3
_TRAILING_V = re.compile(r"(\d+(?:\.\d+)?)V(?![0-9A-Za-z])")        # 5V / 3.3V / 12V
_LEADING_V = re.compile(r"(?<![0-9A-Za-z])V(\d+(?:\.\d+)?)(?![0-9A-Za-z])")  # V5 / V3.3


def norm(name: str) -> str:
    """Normalize a net/rail name for matching: upper-case, drop a leading ``+``."""
    return name.upper().lstrip("+").strip()


def implied_voltage(name: str | None) -> float | None:
    """Best-effort voltage implied by a rail name (``+3V3`` -> 3.3, ``5V`` -> 5.0)."""
    if not name:
        return None
    s = norm(name)
    m = _DIGIT_V_DIGIT.search(s)
    if m:
        try:
            return float(f"{m.group(1)}.{m.group(2)}")
        except ValueError:
            return None
    m = _TRAILING_V.search(s)
    if m:
        return float(m.group(1))
    m = _LEADING_V.search(s)
    if m:
        return float(m.group(1))
    return None


def rail_matches(net_name: str, cfg_rail_names: set[str]) -> bool:
    """True if ``net_name`` is a configured rail, exactly or as a ``<rail>_suffix``.

    So a config ``[[rail]] name = "V3V3"`` covers ``V3V3``, ``V3V3_AUX``, ``V3V3-IO``
    (rail name followed by a ``_``/``-`` separator), not arbitrary substrings.
    """
    n = norm(net_name)
    if n in cfg_rail_names:
        return True
    return any(n.startswith(r + "_") or n.startswith(r + "-") for r in cfg_rail_names)
