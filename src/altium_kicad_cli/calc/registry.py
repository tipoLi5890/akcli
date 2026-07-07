"""Calculator registry: one entry per calculator, self-describing.

Every calculator carries a **formal reference** (standard, datasheet, or
textbook citation) that is printed with every result — a number without its
source is not an engineering answer. Registration happens at import time via
:func:`register`; the CLI (`akcli calc`) and the docs render from this table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

__all__ = ["Param", "Result", "Calc", "CALCS", "register", "compute"]


@dataclass(frozen=True)
class Param:
    """One calculator input. ``default=None`` means required."""
    name: str
    unit: str                  # display unit: "Ω", "F", "V", "A", "Hz", "m", ...
    help: str
    default: object = None     # float | str | None
    choices: tuple = ()        # non-empty -> enumerated string param
    text: bool = False         # True -> pass the raw string through


@dataclass(frozen=True)
class Result:
    """One output row. ``value`` may be a number, string, or list of dicts."""
    name: str
    value: object
    unit: str = ""
    note: str = ""


@dataclass(frozen=True)
class Calc:
    name: str
    title: str
    group: str
    reference: str             # formal citation (standard / datasheet / book)
    params: tuple[Param, ...]
    func: Callable[..., list[Result]]
    notes: str = ""


CALCS: dict[str, Calc] = {}

_RESERVED = {"list", "info"}


def register(name: str, title: str, group: str, reference: str,
             params: tuple[Param, ...], notes: str = ""):
    """Decorator: add ``func`` to the registry under ``name``."""
    if name in _RESERVED:
        raise ValueError(f"calculator name {name!r} is reserved")

    def deco(func):
        if name in CALCS:
            raise ValueError(f"duplicate calculator {name!r}")
        CALCS[name] = Calc(name=name, title=title, group=group,
                           reference=reference, params=params, func=func,
                           notes=notes)
        return func
    return deco


class CalcError(ValueError):
    """Bad input to a calculator (maps to exit 2 at the CLI layer)."""


def compute(name: str, raw: dict[str, str]) -> dict:
    """Run calculator ``name`` with string inputs; returns the full envelope."""
    from .si import parse_value

    calc = CALCS.get(name)
    if calc is None:
        raise KeyError(name)
    known = {p.name: p for p in calc.params}
    unknown = set(raw) - set(known)
    if unknown:
        raise CalcError(f"unknown parameter(s) {sorted(unknown)!r}; "
                        f"expected {sorted(known)}")
    kwargs: dict[str, object] = {}
    for p in calc.params:
        if p.name in raw:
            if p.choices:
                if raw[p.name] not in p.choices:
                    raise CalcError(
                        f"{p.name}: must be one of {list(p.choices)}")
                kwargs[p.name] = raw[p.name]
            elif p.text:
                kwargs[p.name] = raw[p.name]
            else:
                kwargs[p.name] = parse_value(raw[p.name], p.name)
        elif p.default is not None:
            kwargs[p.name] = p.default
        else:
            raise CalcError(f"missing required parameter {p.name!r} "
                            f"({p.help}, unit: {p.unit or '-'})")
    results = calc.func(**kwargs)
    return {
        "calc": calc.name,
        "title": calc.title,
        "inputs": {p.name: kwargs[p.name] for p in calc.params if p.name in kwargs},
        "results": {
            r.name: ({"value": r.value, "unit": r.unit, **({"note": r.note} if r.note else {})})
            for r in results
        },
        "reference": calc.reference,
    }
