"""Calculator registry: one entry per calculator, self-describing.

Every calculator carries a **formal reference** (standard, datasheet, or
textbook citation) that is printed with every result — a number without its
source is not an engineering answer. Registration happens at import time via
:func:`register`; the CLI (`akcli calc`) and the docs render from this table.
"""

from __future__ import annotations

import math
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
             params: tuple[Param, ...], notes: str = ""
             ) -> Callable[[Callable[..., list[Result]]], Callable[..., list[Result]]]:
    """Decorator: add ``func`` to the registry under ``name``."""
    if name in _RESERVED:
        raise ValueError(f"calculator name {name!r} is reserved")

    def deco(func: Callable[..., list[Result]]) -> Callable[..., list[Result]]:
        if name in CALCS:
            raise ValueError(f"duplicate calculator {name!r}")
        CALCS[name] = Calc(name=name, title=title, group=group,
                           reference=reference, params=params, func=func,
                           notes=notes)
        return func
    return deco


class CalcError(ValueError):
    """Bad input to a calculator (maps to exit 2 at the CLI layer)."""


def _run(calc: Calc, kwargs: dict[str, object]) -> list[Result]:
    """Evaluate one calculator, converting arithmetic blow-ups into ``CalcError``.

    ONE guard for the whole registry instead of a per-calculator zero check.
    Every calculator is a closed-form formula over user numbers, so a degenerate
    input reaches Python as an arithmetic exception — ``calc rc r=0 c=0`` divides
    by ``2πRC``, and that ``ZeroDivisionError`` used to escape as ``INTERNAL``
    exit 3 plus a traceback. These are all BAD INPUT (exit 2), not akcli bugs:

    * ``ZeroDivisionError`` — a denominator the caller drove to zero;
    * ``OverflowError`` — ``math.exp``/``**`` past the float range;
    * ``ValueError`` — ``math.log``/``sqrt`` outside their domain (``CalcError``
      is a ``ValueError`` subclass, so a calculator's OWN refusal passes
      through first, keeping its specific message).

    Non-finite RESULTS are refused for the same reason ``allow_nan=False``
    exists on the JSON exit: a bare ``NaN``/``Infinity`` is not valid JSON, so
    it must never reach the envelope.
    """
    try:
        results = calc.func(**kwargs)
    except CalcError:
        raise
    except ZeroDivisionError:
        raise CalcError(
            f"{calc.name}: division by zero — one of the inputs "
            f"({', '.join(sorted(kwargs))}) is zero where a nonzero value is "
            f"required (`akcli calc info {calc.name}` describes each)")
    except OverflowError:
        raise CalcError(f"{calc.name}: the result overflows a float; "
                        "check the input magnitudes")
    except ValueError as exc:
        raise CalcError(f"{calc.name}: {exc}")
    for r in results:
        if isinstance(r.value, float) and not math.isfinite(r.value):
            raise CalcError(
                f"{calc.name}: result {r.name!r} is not finite ({r.value}); "
                "the inputs are degenerate for this formula")
    return results


def compute(name: str, raw: dict[str, str]) -> dict[str, object]:
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
                kwargs[p.name] = parse_value(raw[p.name], p.name, p.unit)
        elif p.default is not None:
            kwargs[p.name] = p.default
        else:
            raise CalcError(f"missing required parameter {p.name!r} "
                            f"({p.help}, unit: {p.unit or '-'})")
    results = _run(calc, kwargs)
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
