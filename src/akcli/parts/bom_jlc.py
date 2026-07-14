"""BOM → JLCPCB/LCSC purchasability bridge (``akcli jlc bom``).

Resolves each BOM line to a catalog part and reports whether it can actually
be bought: **explicit LCSC C-number** parameters win (direct ``get``), then an
**MPN parameter** (searched, exact-match on the manufacturer part number),
else the line is flagged ``no-part-id`` — advisory, since identifying a bare
"10k 0402" by value alone would be guesswork, not a check.

Same eligibility rules as the offline BOM check (no ``#``-virtual parts, no
synthesized designators). Lines group by resolved identity so N decoupling
caps sharing one C-number cost one lookup and one row. Network errors raise
:class:`~.search.JlcNetworkError` — the CLI maps them to exit 7 like the rest
of the ``jlc`` family. This module is import-isolated with the other
networked code under ``akcli.parts``.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..checks.bom import _real_components
from ..model import Component, Schematic
from . import search as parts_search

__all__ = ["BomLine", "check", "collect_lines", "to_jlc_csv",
           "suggest_parts", "fix_ops", "totals"]

# Parameter names that carry an LCSC C-number (checked in order; any other
# parameter whose name mentions lcsc/jlc and whose value looks like a
# C-number is accepted as a fallback).
_LCSC_KEYS: tuple[str, ...] = (
    "LCSC Part", "LCSC Part Name", "LCSC", "LCSC#",
    "JLCPCB Part", "JLCPCB", "JLC", "JLC#",
)
# Parameter names that carry a manufacturer part number.
_MPN_KEYS: tuple[str, ...] = (
    "MPN", "Manufacturer Part", "Manufacturer Part Number", "Mfr. Part",
    "Mfr Part", "Part Number", "Supplier Part",
)

_CNUM_RE = re.compile(r"^[Cc]?(\d{2,})$")


def _clean(v: object) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


def _lcsc_of(comp: Component) -> tuple[str, str] | None:
    """``(param_key, C<digits>)`` for an explicit LCSC parameter, or None."""
    params = comp.parameters or {}
    for key in _LCSC_KEYS:
        for k, v in params.items():
            if k.casefold() != key.casefold():
                continue
            m = _CNUM_RE.match(_clean(v) or "")
            if m:
                return k, "C" + m.group(1)
    for k, v in params.items():
        kf = k.casefold()
        if "lcsc" in kf or "jlc" in kf:
            m = _CNUM_RE.match(_clean(v) or "")
            if m:
                return k, "C" + m.group(1)
    return None


def _mpn_of(comp: Component) -> str | None:
    lowered = {k.casefold(): v for k, v in (comp.parameters or {}).items()}
    for key in _MPN_KEYS:
        v = _clean(lowered.get(key.casefold()))
        if v:
            return v
    return None


@dataclass
class BomLine:
    """One purchasability row: a group of refs resolving to one part."""

    refs: list[str]
    value: str | None
    footprint: str | None
    lcsc: str | None = None            # explicit C-number, if any
    lcsc_key: str | None = None        # the parameter name that carried it
    mpn: str | None = None             # explicit MPN parameter, if any
    status: str = ""                   # ok|low-stock|out-of-stock|not-found|no-part-id
    part: parts_search.Part | None = None
    note: str = ""
    need: int = 0                      # pieces required = qty * boards
    unit_price: float | None = None    # tier price applicable at `need`
    ext_price: float | None = None     # unit_price * need
    suggestion: parts_search.Part | None = None   # catalog fix candidate
    suggestion_confidence: str | None = None      # "high" | "low" (set with suggestion)

    @property
    def qty(self) -> int:
        return len(self.refs)

    def to_dict(self) -> dict:
        return {
            "refs": self.refs, "qty": self.qty, "need": self.need,
            "value": self.value, "footprint": self.footprint,
            "lcsc": self.lcsc, "mpn": self.mpn,
            "status": self.status, "note": self.note,
            "unit_price": self.unit_price, "ext_price": self.ext_price,
            "part": self.part.to_dict() if self.part else None,
            "suggestion": self.suggestion.to_dict() if self.suggestion else None,
            "suggestion_confidence": self.suggestion_confidence,
        }


def collect_lines(sch: Schematic) -> list[BomLine]:
    """Group eligible components into BOM lines by resolved identity."""
    lines: dict[tuple, BomLine] = {}
    for comp in _real_components(sch):
        hit, mpn = _lcsc_of(comp), _mpn_of(comp)
        lcsc_key, lcsc = hit if hit else (None, None)
        key = (("lcsc", lcsc) if lcsc else
               ("mpn", mpn.casefold()) if mpn else
               ("anon", comp.value, comp.footprint, comp.library_ref))
        line = lines.get(key)
        if line is None:
            lines[key] = line = BomLine(
                refs=[], value=_clean(comp.value),
                footprint=_clean(comp.footprint),
                lcsc=lcsc, lcsc_key=lcsc_key, mpn=mpn)
        if comp.designator not in line.refs:   # multi-unit parts count once
            line.refs.append(comp.designator)
    return list(lines.values())


_collect_lines = collect_lines                 # back-compat alias (webui, older callers)


def _stock_status(part: parts_search.Part, need: int,
                  min_stock: int) -> tuple[str, str]:
    if part.stock <= 0:
        return "out-of-stock", ""
    floor = max(need, min_stock)
    if part.stock < floor:
        return "low-stock", f"stock {part.stock} < required {floor}"
    return "ok", ""


def _price_at(part: parts_search.Part, need: int) -> float | None:
    """The tier unit price applicable when buying ``need`` pieces.

    Below the lowest tier's ``qFrom`` (catalog minimums are often 20+) the
    lowest tier still applies — that IS the minimum-order price.
    """
    tiers: list[tuple[int, float]] = []
    for t in part.attributes.get("price_tiers") or []:
        try:
            tiers.append((int(t.get("qFrom") or 0), float(t["price"])))
        except (TypeError, ValueError, KeyError):
            continue
    if not tiers:
        return part.price
    tiers.sort()
    applicable = [price for q_from, price in tiers if q_from <= need]
    return applicable[-1] if applicable else tiers[0][1]


def _apply_pricing(line: BomLine, qty: int, min_stock: int) -> None:
    line.need = line.qty * qty
    part = line.part
    if part is None:
        return
    line.status, line.note = _stock_status(part, line.need, min_stock)
    line.unit_price = _price_at(part, line.need)
    if line.unit_price is not None:
        line.ext_price = round(line.unit_price * line.need, 6)


def check(
    sch: Schematic,
    *,
    min_stock: int = 1,
    qty: int = 1,
    get: Callable[[str], parts_search.Part | None] | None = None,
    find: Callable[..., list[parts_search.Part]] | None = None,
    cache_dir: str | Path | None = None,
) -> list[BomLine]:
    """Resolve every BOM line against the catalog (one lookup per identity).

    ``qty`` is the number of boards: each line needs ``qty × refs`` pieces,
    stock and tier pricing are evaluated at that quantity. ``get``/``find``
    are injectable for offline tests; the defaults resolve at call time (so
    monkeypatching ``parts.search`` works), use the CLI's on-disk cache when
    ``cache_dir`` is given, and may raise :class:`JlcNetworkError`.
    """
    if get is None:
        get = lambda lcsc: parts_search.get(lcsc, cache_dir=cache_dir)  # noqa: E731
    if find is None:
        find = lambda q, limit=10: parts_search.search(  # noqa: E731
            q, limit=limit, cache_dir=cache_dir)
    lines = collect_lines(sch)
    for line in lines:
        line.need = line.qty * qty
        if line.lcsc:
            part = get(line.lcsc)
            if part is None:
                line.status, line.note = "not-found", f"{line.lcsc} not in catalog"
                continue
            line.part = part
            _apply_pricing(line, qty, min_stock)
        elif line.mpn:
            results = find(line.mpn, limit=10)
            exact = [p for p in results
                     if p.mpn.casefold() == line.mpn.casefold()]
            if not exact:
                line.status = "not-found"
                line.note = (f"no exact MPN match"
                             + (f" (nearest: {results[0].mpn})" if results else ""))
                continue
            # prefer in-stock, then Basic, then the deepest stock
            exact.sort(key=lambda p: (p.stock > 0, p.basic, p.stock), reverse=True)
            line.part = exact[0]
            line.lcsc = exact[0].lcsc
            note = (f"{len(exact)} candidates, picked {exact[0].lcsc}"
                    if len(exact) > 1 else "")
            _apply_pricing(line, qty, min_stock)
            if note and not line.note:
                line.note = note
        else:
            line.status = "no-part-id"
            line.note = "add an LCSC / MPN parameter to check purchasability"
    return lines


_PKG_RE = re.compile(r"_(\d{4})_")            # R_0402_1005Metric -> 0402


def _pkg_of(footprint: str | None) -> str:
    m = _PKG_RE.search(footprint or "")
    return m.group(1) if m else ""


def _suggest_queries(line: BomLine) -> list[str]:
    """Catalog queries for a problem line, most specific first."""
    val = (line.value or "").strip()
    if not val:
        return []
    pkg = _pkg_of(line.footprint)
    kind = (line.refs[0][:1] if line.refs else "").upper()
    queries = []
    if kind == "C" and val[-1:] in "pnum":     # 100n -> 100nF (catalog spelling)
        queries.append(f"{val}F {pkg}".strip())
    queries.append(f"{val} {pkg}".strip())
    return queries


def _confidence(line: BomLine, cand: parts_search.Part, pkg: str) -> str:
    """Grade a suggestion: "high" only when the package matched AND the line's
    value is visible in the candidate's description/MPN; anything weaker is
    "low" and the default ``fix_ops`` gate refuses to write it."""
    if not pkg or cand.package != pkg:
        return "low"
    val = (line.value or "").strip().casefold()
    if not val:
        return "low"
    hay = f"{cand.description} {cand.mpn}".casefold()
    # substring match also covers catalog spellings ("100n" ⊂ "100nF")
    return "high" if val in hay else "low"


def suggest_parts(
    lines: list[BomLine], *,
    find: Callable[..., list[parts_search.Part]] | None = None,
    cache_dir: str | Path | None = None,
) -> int:
    """Fill ``line.suggestion`` for not-found / no-part-id lines.

    Candidates must match the footprint's package size when it is known;
    ranking prefers in-stock, then Basic, then Preferred, then depth of
    stock. Returns the number of lines that received a suggestion — every
    suggestion is a HUMAN DECISION to accept (``--fix``), verified against
    the datasheet; value+package matching is a search heuristic, not proof.
    """
    if find is None:
        find = lambda q, limit=20: parts_search.search(  # noqa: E731
            q, limit=limit, cache_dir=cache_dir)
    n = 0
    for line in lines:
        if line.status not in ("not-found", "no-part-id"):
            continue
        pkg = _pkg_of(line.footprint)
        for q in _suggest_queries(line):
            cands = [p for p in find(q, limit=20)
                     if not pkg or p.package == pkg]
            if cands:
                cands.sort(key=lambda p: (p.stock > 0, p.basic,
                                          p.preferred, p.stock), reverse=True)
                line.suggestion = cands[0]
                line.suggestion_confidence = _confidence(line, cands[0], pkg)
                n += 1
                break
    return n


_CONFIDENCE_RANK = {"low": 0, "high": 1}


def fix_ops(lines: list[BomLine], *, min_confidence: str = "high") -> list[dict]:
    """`set_component_parameters` ops writing each suggestion's C-number.

    The C-number lands in the line's existing LCSC parameter key (so a wrong
    id is corrected in place) or a new ``LCSC`` parameter; one op per ref.

    Only suggestions at or above ``min_confidence`` are written: the default
    ``"high"`` keeps ``--fix`` from committing a package-only guess to the
    schematic; pass ``"low"`` to write every suggestion (the CLI's
    ``--fix-all``). A suggestion whose confidence was never graded counts as
    ``"low"``.
    """
    floor = _CONFIDENCE_RANK.get(min_confidence, 1)
    ops: list[dict] = []
    for line in lines:
        if line.suggestion is None:
            continue
        if _CONFIDENCE_RANK.get(line.suggestion_confidence or "low", 0) < floor:
            continue
        key = line.lcsc_key or "LCSC"
        for ref in line.refs:
            ops.append({"op": "set_component_parameters", "designator": ref,
                        "parameters": {key: line.suggestion.lcsc}})
    return ops


_JLC_CSV_HEADER = ("Comment", "Designator", "Footprint", "LCSC Part #")


def _short_footprint(footprint: str | None) -> str:
    """``Library:Name`` → ``Name`` (JLCPCB wants the bare footprint name)."""
    fp = (footprint or "").strip()
    _, _, short = fp.rpartition(":")
    return short or fp


def to_jlc_csv(lines: list[BomLine]) -> str:
    """Render BOM lines as a JLCPCB "Upload BOM" CSV.

    One row per line, sorted by first designator, csv-module quoting. The
    ``Designator`` cell holds every ref comma-joined (so it is quoted whenever
    a line has more than one ref). ``LCSC Part #`` stays blank when the line
    did not resolve — a known-dead C-number must not leak into an order file.
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(_JLC_CSV_HEADER)
    for line in sorted(lines, key=lambda ln: ln.refs[0] if ln.refs else ""):
        lcsc = line.lcsc if (line.lcsc and line.status != "not-found") else ""
        w.writerow([line.value or "", ",".join(line.refs),
                    _short_footprint(line.footprint), lcsc])
    return buf.getvalue()


def totals(lines: list[BomLine]) -> dict:
    """Aggregate cost/coverage over a checked line list."""
    priced = [ln for ln in lines if ln.ext_price is not None]
    return {
        "lines": len(lines),
        "ok": sum(1 for ln in lines if ln.status == "ok"),
        "problems": sum(1 for ln in lines if ln.status in
                        ("not-found", "out-of-stock", "low-stock")),
        "no_part_id": sum(1 for ln in lines if ln.status == "no-part-id"),
        "priced_lines": len(priced),
        "est_cost": round(
            sum((ln.ext_price for ln in priced if ln.ext_price is not None), 0.0), 4),
    }
