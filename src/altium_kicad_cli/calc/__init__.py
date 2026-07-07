"""``akcli calc`` — offline engineering calculators for circuit design.

Zero-dependency reimplementations from the primary sources (standards,
datasheets, textbooks) cited on every calculator. KiCad's ``pcb_calculator``
(GPLv3) was used **only** as an independent numerical cross-check in the test
suite — no KiCad code or text is included here.
"""

from __future__ import annotations

# importing the modules populates the registry
from . import (  # noqa: F401
    analog,
    codes,
    convert,
    electrical,
    eseries,
    ic,
    interface,
    pcb,
    power,
    protection,
    regulator,
    rf,
)
from .registry import CALCS, CalcError, compute  # noqa: F401
from .si import fmt_eng, parse_value  # noqa: F401
