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

from .. import bom_policy
from ..checks.bom import _real_components
from ..model import Component, Schematic
from . import search as parts_search
from . import value_parse

__all__ = ["BomLine", "check", "check_multi", "collect_lines", "to_jlc_csv",
           "suggest_parts", "basic_alternatives", "fix_ops", "totals",
           "make_lock", "diff_lock", "bom_diff", "to_markdown"]

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
    verified: str = ""                 # ""|ok|mismatch|unverified (explicit C-number lines)
    mismatch: list[str] = field(default_factory=list)  # BOM_LCSC_MISMATCH reasons
    bom_class: str = "fitted"          # fitted|dnp|external|no-part (bom_policy)
    sourcing_note: str = ""            # e.g. "vendor official store" for external
    alternates: list[str] = field(default_factory=list)  # LCSC_ALT second sources
    boards: dict[str, list[str]] = field(default_factory=dict)  # board -> refs

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
            "verified": self.verified,
            "mismatch": list(self.mismatch),
            "bom_class": self.bom_class,
            "sourcing_note": self.sourcing_note,
            "alternates": list(self.alternates),
            "boards": {k: list(v) for k, v in self.boards.items()},
        }


def collect_lines(
    sch: Schematic, *,
    no_part_prefixes: tuple[str, ...] = bom_policy.DEFAULT_NO_PART_PREFIXES,
) -> list[BomLine]:
    """Group eligible components into BOM lines by resolved identity + class.

    Every BOM population is collected — fitted, dnp, external, no-part — so
    downstream surfaces (table, CSV, totals) can show the whole board with
    honest per-class annotations instead of dropping rows silently. The
    class is part of the grouping key: a DNP'd twin of a fitted part must
    stay its own line (its pieces are NOT ordered).
    """
    lines: dict[tuple, BomLine] = {}
    for comp in _real_components(sch):
        klass, note = bom_policy.classify(comp, no_part_prefixes)
        hit, mpn = _lcsc_of(comp), _mpn_of(comp)
        lcsc_key, lcsc = hit if hit else (None, None)
        key = (klass,) + (("lcsc", lcsc) if lcsc else
                          ("mpn", mpn.casefold()) if mpn else
                          ("anon", comp.value, comp.footprint, comp.library_ref))
        line = lines.get(key)
        if line is None:
            lines[key] = line = BomLine(
                refs=[], value=_clean(comp.value),
                footprint=_clean(comp.footprint),
                lcsc=lcsc, lcsc_key=lcsc_key, mpn=mpn,
                bom_class=klass, sourcing_note=note,
                alternates=_alternates_of(comp))
        if comp.designator not in line.refs:   # multi-unit parts count once
            line.refs.append(comp.designator)
    return list(lines.values())


def _alternates_of(comp: Component) -> list[str]:
    """Second-source C-numbers from an ``LCSC_ALT`` parameter (semicolons)."""
    params = comp.parameters or {}
    raw = ""
    for k, v in params.items():
        if k.casefold() in ("lcsc_alt", "lcsc alt", "lcsc-alt"):
            raw = str(v or "")
            break
    out: list[str] = []
    for tok in raw.split(";"):
        m = _CNUM_RE.match(tok.strip())
        if m:
            out.append("C" + m.group(1))
    return out


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


def _part_haystack(part: parts_search.Part) -> str:
    """Every catalog text that can carry the part's electrical value."""
    bits = [part.description or "", part.mpn or ""]
    specs = part.attributes.get("specs")
    if isinstance(specs, dict):
        bits += [f"{k} {v}" for k, v in specs.items()]
    elif isinstance(specs, list):
        bits += [str(s) for s in specs]
    return " ".join(bits)


def _bare_ref(ref: str) -> str:
    """Strip a multi-board ``board:ref`` prefix back to the bare refdes."""
    return ref.split(":", 1)[1] if ":" in ref else ref


def _line_parsed_value(line: BomLine) -> value_parse.ParsedValue | None:
    hint = value_parse.unit_class_of_ref(
        _bare_ref(line.refs[0]) if line.refs else None)
    return value_parse.parse(line.value, hint=hint)


def verify_line(line: BomLine) -> None:
    """Reverse-check an explicit C-number against the schematic line (P0-1).

    A filled-in C-number used to be trusted unconditionally — only stock and
    price were checked, never whether the catalog part IS the schematic part.
    A mistyped id meant silently ordering the wrong component: the highest-
    risk cell in the whole flow. Compares the catalog part's package and
    normalized value (:mod:`.value_parse`) against the line; disagreement
    fills ``line.mismatch`` (finding code ``BOM_LCSC_MISMATCH``); nothing to
    compare on either side is honestly ``unverified``, never a silent pass.
    """
    part = line.part
    if part is None or not line.lcsc:
        return
    checked = False
    pkg = _pkg_of(line.footprint)
    if pkg and part.package:
        checked = True
        if part.package != pkg:
            line.mismatch.append(
                f"package mismatch: schematic {pkg} vs catalog "
                f"{part.package} ({line.lcsc})")
    parsed = _line_parsed_value(line)
    if parsed is not None and parsed.unit_class:
        cands = value_parse.extract_values(
            _part_haystack(part), unit_class=parsed.unit_class)
        if cands:
            checked = True
            if not any(value_parse.same_value(parsed.value, c.value)
                       for c in cands):
                catalog = ", ".join(sorted({_fmt_value(c.value, parsed.unit_class)
                                            for c in cands}))
                line.mismatch.append(
                    f"value mismatch: schematic {line.value} vs catalog "
                    f"{catalog} ({line.lcsc})")
    if line.mismatch:
        line.verified = "mismatch"
        line.note = (line.note + "; " if line.note else "") + \
            "BOM_LCSC_MISMATCH: " + "; ".join(line.mismatch)
    elif checked:
        line.verified = "ok"
    else:
        # neither package nor value comparable: cannot judge != pass
        line.verified = "unverified"
        line.note = (line.note + "; " if line.note else "") + \
            "unverified: catalog and schematic carry nothing comparable"


_UNIT_SUFFIX = {"R": "Ω", "C": "F", "L": "H"}


def _fmt_value(value: float, unit_class: str) -> str:
    """Human spelling of a canonical value (1000.0, "R") -> "1kΩ"."""
    for factor, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""),
                           (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if abs(value) >= factor or factor == 1e-12:
            scaled = value / factor
            num = f"{scaled:.6g}"
            return f"{num}{prefix}{_UNIT_SUFFIX.get(unit_class, '')}"
    return f"{value:g}{_UNIT_SUFFIX.get(unit_class, '')}"


def check(
    sch: Schematic,
    *,
    min_stock: int = 1,
    qty: int = 1,
    get: Callable[[str], parts_search.Part | None] | None = None,
    find: Callable[..., list[parts_search.Part]] | None = None,
    cache_dir: str | Path | None = None,
    no_part_prefixes: tuple[str, ...] = bom_policy.DEFAULT_NO_PART_PREFIXES,
    offline: bool = False,
) -> list[BomLine]:
    """Resolve every BOM line against the catalog (one lookup per identity).

    ``qty`` is the number of boards: each line needs ``qty × refs`` pieces,
    stock and tier pricing are evaluated at that quantity. ``get``/``find``
    are injectable for offline tests; the defaults resolve at call time (so
    monkeypatching ``parts.search`` works), use the CLI's on-disk cache when
    ``cache_dir`` is given, and may raise :class:`JlcNetworkError`.

    ``offline=True`` answers from the HTTP cache only: a cache miss degrades
    that line to ``status="unverified"`` instead of raising — the caller
    banners the whole run as degraded, but a warm cache still verifies.
    """
    if get is None:
        get = lambda lcsc: parts_search.get(  # noqa: E731
            lcsc, cache_dir=cache_dir, offline=offline)
    if find is None:
        find = lambda q, limit=10: parts_search.search(  # noqa: E731
            q, limit=limit, cache_dir=cache_dir, offline=offline)
    lines = collect_lines(sch, no_part_prefixes=no_part_prefixes)
    return resolve_lines(lines, min_stock=min_stock, qty=qty, get=get, find=find)


def resolve_lines(
    lines: list[BomLine],
    *,
    min_stock: int,
    qty: int,
    get: Callable[[str], parts_search.Part | None],
    find: Callable[..., list[parts_search.Part]],
) -> list[BomLine]:
    """The catalog-resolution loop shared by single- and multi-board checks."""
    for line in lines:
        line.need = line.qty * qty
        if line.bom_class == "no-part":
            line.status = "no-part"
            line.need = 0
            line.note = line.sourcing_note or "structural, no part expected"
            continue
        if line.bom_class == "dnp":
            line.status = "dnp"
            line.need = 0                    # nothing is bought for DNP refs
            note = "do not populate"
            if line.lcsc:
                note += (f"; carries {line.lcsc} — confirm intent "
                         "(BOM_DNP_HAS_ORDER_ID)")
            line.note = note
            continue
        if line.bom_class == "external":
            line.status = "external"
            line.note = ("sourced externally"
                         + (f": {line.sourcing_note}" if line.sourcing_note
                            else "")) + " — not checked against the catalog"
            continue
        if line.lcsc:
            try:
                part = get(line.lcsc)
            except parts_search.JlcOfflineMiss:
                line.status = "unverified"
                line.note = f"offline: {line.lcsc} not in cache"
                continue
            if part is None:
                line.status, line.note = "not-found", f"{line.lcsc} not in catalog"
            else:
                line.part = part
                _apply_pricing(line, qty, min_stock)
                verify_line(line)
            if line.status in ("not-found", "out-of-stock", "low-stock") \
                    and line.alternates:
                _try_alternates(line, qty, min_stock, get)
            continue
        elif line.mpn:
            try:
                results = find(line.mpn, limit=10)
            except parts_search.JlcOfflineMiss:
                line.status = "unverified"
                line.note = f"offline: {line.mpn!r} not in cache"
                continue
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


def _try_alternates(line: BomLine, qty: int, min_stock: int,
                    get: Callable[[str], parts_search.Part | None]) -> None:
    """Fall back to an ``LCSC_ALT`` second source when the primary is at risk.

    The first alternate that resolves to a healthy (``ok``) part replaces
    the line's resolution; the note records the fallback so the order file
    is traceable. A fallback part is reverse-verified like any explicit id.
    """
    primary, reason = line.lcsc, line.status
    for alt in line.alternates:
        try:
            part = get(alt)
        except parts_search.JlcOfflineMiss:
            continue
        if part is None:
            continue
        status, _ = _stock_status(part, line.qty * qty, min_stock)
        if status != "ok":
            continue
        line.lcsc = alt
        line.part = part
        line.mismatch = []
        line.verified = ""
        _apply_pricing(line, qty, min_stock)
        verify_line(line)
        line.note = ((line.note + "; ") if line.note else "") + \
            f"fallback from {primary} ({reason}) via LCSC_ALT"
        return
    line.note = ((line.note + "; ") if line.note else "") + \
        f"{len(line.alternates)} alternate(s) tried, none healthy"


def check_multi(
    boards: list[tuple[str, Schematic]],
    *,
    min_stock: int = 1,
    qty: int = 1,
    get: Callable[[str], parts_search.Part | None] | None = None,
    find: Callable[..., list[parts_search.Part]] | None = None,
    cache_dir: str | Path | None = None,
    no_part_prefixes: tuple[str, ...] = bom_policy.DEFAULT_NO_PART_PREFIXES,
    offline: bool = False,
) -> list[BomLine]:
    """One merged shopping cart across several boards (``jlc bom a b c``).

    Lines sharing a resolved identity (same class + C-number/MPN/anon key)
    merge across boards: needs add up, tier pricing is evaluated at the
    MERGED quantity, and each line carries a per-board refs breakdown
    (``line.boards``). Refs are prefixed ``board:ref`` so R1 on one board
    and R1 on another stay distinguishable everywhere downstream.
    """
    if get is None:
        get = lambda lcsc: parts_search.get(  # noqa: E731
            lcsc, cache_dir=cache_dir, offline=offline)
    if find is None:
        find = lambda q, limit=10: parts_search.search(  # noqa: E731
            q, limit=limit, cache_dir=cache_dir, offline=offline)
    merged: dict[tuple, BomLine] = {}
    for board, sch in boards:
        for ln in collect_lines(sch, no_part_prefixes=no_part_prefixes):
            key = (ln.bom_class,) + (
                ("lcsc", ln.lcsc) if ln.lcsc else
                ("mpn", ln.mpn.casefold()) if ln.mpn else
                ("anon", ln.value, ln.footprint))
            tgt = merged.get(key)
            if tgt is None:
                merged[key] = tgt = BomLine(
                    refs=[], value=ln.value, footprint=ln.footprint,
                    lcsc=ln.lcsc, lcsc_key=ln.lcsc_key, mpn=ln.mpn,
                    bom_class=ln.bom_class, sourcing_note=ln.sourcing_note,
                    alternates=list(ln.alternates))
            tgt.refs.extend(f"{board}:{r}" for r in ln.refs)
            tgt.boards.setdefault(board, []).extend(ln.refs)
    lines = list(merged.values())
    return resolve_lines(lines, min_stock=min_stock, qty=qty, get=get, find=find)


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
    kind = (_bare_ref(line.refs[0])[:1] if line.refs else "").upper()
    queries = []
    if kind == "C" and val[-1:] in "pnum":     # 100n -> 100nF (catalog spelling)
        queries.append(f"{val}F {pkg}".strip())
    queries.append(f"{val} {pkg}".strip())
    return queries


def _confidence(line: BomLine, cand: parts_search.Part, pkg: str) -> str:
    """Grade a suggestion: "high" needs the package to match AND the line's
    NORMALIZED value to strictly equal a value the candidate's catalog text
    claims (cross-unit spellings compare equal: 2200nF == 2.2µF); a
    dielectric the line demands that the candidate contradicts (C0G vs X7R)
    downgrades. Anything weaker — including a value this parser cannot
    understand — is "low" and the default ``fix_ops`` gate refuses to write
    it. Substring matching is gone: "1k" ⊂ "5.1k" and "2.2u" inside a 22 µF
    description are exactly the historical wrong-part incidents.
    """
    if not pkg or cand.package != pkg:
        return "low"
    parsed = _line_parsed_value(line)
    if parsed is None or not parsed.unit_class:
        return "low"
    cands = value_parse.extract_values(
        _part_haystack(cand), unit_class=parsed.unit_class)
    if not any(value_parse.same_value(parsed.value, c.value) for c in cands):
        return "low"
    if parsed.dielectric:
        stated = {c.dielectric for c in cands if c.dielectric}
        if stated and parsed.dielectric not in stated:
            return "low"
    return "high"


def suggest_parts(
    lines: list[BomLine], *,
    find: Callable[..., list[parts_search.Part]] | None = None,
    cache_dir: str | Path | None = None,
    include_risk: bool = False,
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
    wanted = {"not-found", "no-part-id"}
    if include_risk:
        # --alternates: second-source candidates for at-risk lines too
        wanted |= {"low-stock", "out-of-stock"}
    n = 0
    for line in lines:
        if line.bom_class != "fitted":
            continue                        # only fitted lines are sourced
        if line.status not in wanted:
            continue
        if line.refs and all(bom_policy.is_no_part_ref(_bare_ref(r))
                             for r in line.refs):
            # structural refs (TP/FID/MH/...) never receive a suggestion —
            # the historical TP -> SMPS-IC incident, closed for good
            line.note = "structural, no part expected"
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


def basic_alternatives(
    lines: list[BomLine], *,
    find: Callable[..., list[parts_search.Part]] | None = None,
    cache_dir: str | Path | None = None,
) -> int:
    """Advisory: note a Basic same-value/same-package swap for Extended passives.

    Every Extended line costs a feeder setup fee; a passive that exists as a
    Basic part with the SAME normalized value and package (strict P0-2
    equality, never substring) is money on the table. Adds a note only — a
    swap is a human decision, nothing is written.
    """
    if find is None:
        find = lambda q, limit=20: parts_search.search(  # noqa: E731
            q, limit=limit, cache_dir=cache_dir)
    n = 0
    for line in lines:
        if line.bom_class != "fitted" or line.part is None or line.part.basic:
            continue
        parsed = _line_parsed_value(line)
        if parsed is None or parsed.unit_class not in ("R", "C", "L"):
            continue                        # passives only
        pkg = _pkg_of(line.footprint)
        if not pkg:
            continue
        for q in _suggest_queries(line):
            cands = [p for p in find(q, limit=20)
                     if p.basic and p.stock > 0 and p.package == pkg
                     and p.lcsc != line.lcsc
                     and _confidence(line, p, pkg) == "high"]
            if cands:
                cands.sort(key=lambda p: p.stock, reverse=True)
                line.note = ((line.note + "; ") if line.note else "") + \
                    (f"Basic alternative: {cands[0].lcsc} {cands[0].mpn} "
                     "(same value+package) — saves the extended feeder fee")
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


# The 嘉立创EDA (JLC EDA) BOM template field set + our trailing Note column.
_JLC_CSV_HEADER = ("No.", "Quantity", "Comment", "Designator", "Footprint",
                   "Value", "Manufacturer Part", "Manufacturer",
                   "Supplier Part", "Supplier", "Note")


def _short_footprint(footprint: str | None) -> str:
    """``Library:Name`` → ``Name`` (JLCPCB wants the bare footprint name)."""
    fp = (footprint or "").strip()
    _, _, short = fp.rpartition(":")
    return short or fp


def _csv_note(line: BomLine) -> str:
    """The Note cell: why this row is not a plain orderable line."""
    if line.bom_class == "dnp":
        return "DNP — do not populate"
    if line.bom_class == "external":
        if line.sourcing_note == "consigned":
            return "Consigned — customer-supplied to the fab"
        return ("External" + (f": {line.sourcing_note}"
                              if line.sourcing_note else "")
                + " — sourced outside JLCPCB")
    if line.bom_class == "no-part":
        return "No part expected (structural)"
    if line.verified == "mismatch":
        return "MISMATCH — verify C-number before ordering"
    return ""


def to_jlc_csv(lines: list[BomLine]) -> str:
    """Render BOM lines as a JLC-EDA-template BOM CSV.

    Columns follow the 嘉立创EDA export (No./Quantity/Comment/Designator/
    Footprint/Value/Manufacturer Part/Manufacturer/Supplier Part/Supplier)
    plus a trailing ``Note``. One row per line — EVERY class stays in the
    file (fitted, dnp, external, no-part) with the Note explaining any row
    that is not a plain orderable line; assemblers see the whole board, not
    a silently filtered one. Sorted by first designator, csv-module quoting.
    ``Supplier Part`` stays blank when the line did not resolve or is not
    LCSC-sourced — a known-dead C-number must not leak into an order file,
    and an external/no-part row orders nothing from LCSC.
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(_JLC_CSV_HEADER)
    ordered = sorted(lines, key=lambda ln: ln.refs[0] if ln.refs else "")
    for i, line in enumerate(ordered, 1):
        lcsc = line.lcsc if (line.lcsc and line.status != "not-found") else ""
        if line.bom_class in ("external", "no-part"):
            lcsc = ""
        part = line.part
        mfr_part = (part.mpn if part else "") or line.mpn or ""
        mfr = (part.attributes.get("manufacturer") or "") if part else ""
        w.writerow([str(i), str(line.qty), line.value or "",
                    ",".join(line.refs), _short_footprint(line.footprint),
                    line.value or "", mfr_part, mfr,
                    lcsc, "LCSC" if lcsc else "", _csv_note(line)])
    return buf.getvalue()


def to_markdown(lines: list[BomLine], agg: dict, *, qty: int = 1) -> str:
    """A shareable Markdown BOM report (``jlc bom --md``).

    One classification summary + one table for the whole board — the same
    numbers the CLI summary and check --bom's BOM_CLASS_SUMMARY speak.
    """
    cls = agg.get("classes") or {}
    head = [
        "# BOM report",
        "",
        f"- lines: {agg['lines']} · ok {agg['ok']} · problems "
        f"{agg['problems']} · without id {agg['no_part_id']}",
        f"- pieces: {cls.get('fitted', 0)} fitted · {cls.get('dnp', 0)} dnp · "
        f"{cls.get('external', 0)} external · {cls.get('no-part', 0)} no-part",
    ]
    if agg.get("fitted_coverage") is not None:
        head.append(f"- fitted coverage: {agg['fitted_coverage']:.0%}")
    if agg.get("mismatches"):
        head.append(f"- **{agg['mismatches']} C-number MISMATCH(ES)** — "
                    "verify before ordering")
    if agg.get("extended_lines"):
        head.append(f"- {agg['basic_lines']} Basic / {agg['extended_lines']} "
                    f"Extended lines (feeder fees ≈ "
                    f"${agg['extended_fee_est']:.2f})")
    if agg.get("priced_lines"):
        head.append(f"- est. parts cost ${agg['est_cost']:.2f} for {qty} "
                    "board(s)")
    rows = ["", "| Refs | Qty | Value | Part | Class | Status | Unit | Ext | Note |",
            "|---|---|---|---|---|---|---|---|---|"]
    for ln in sorted(lines, key=lambda x: x.refs[0] if x.refs else ""):
        unit = f"${ln.unit_price:.4f}" if ln.unit_price is not None else ""
        ext = f"${ln.ext_price:.2f}" if ln.ext_price is not None else ""
        note = (ln.note or _csv_note(ln)).replace("|", "\\|")
        rows.append(
            f"| {','.join(ln.refs)} | {ln.qty} | {ln.value or ''} "
            f"| {ln.lcsc or ln.mpn or ''} | {ln.bom_class} | {ln.status} "
            f"| {unit} | {ext} | {note} |")
    return "\n".join(head + rows) + "\n"


def totals(lines: list[BomLine], *, extended_fee: float = 3.0) -> dict:
    """Aggregate cost/coverage/assembly-economics over a checked line list.

    ``extended_fee`` is the per-line feeder setup fee JLCPCB charges for
    Extended parts (config: ``[bom].extended_fee``); the estimate prices the
    hidden cost of a BOM full of Extended passives next to the parts cost.
    """
    priced = [ln for ln in lines if ln.ext_price is not None]
    classes = {k: sum(ln.qty for ln in lines if ln.bom_class == k)
               for k in bom_policy.CLASSES}
    fitted = [ln for ln in lines if ln.bom_class == "fitted"]
    fitted_refs = sum(ln.qty for ln in fitted)
    covered_refs = sum(ln.qty for ln in fitted if ln.lcsc or ln.mpn)
    return {
        "lines": len(lines),
        "ok": sum(1 for ln in lines if ln.status == "ok"),
        "problems": sum(1 for ln in lines if ln.status in
                        ("not-found", "out-of-stock", "low-stock")
                        or ln.verified == "mismatch"),
        "mismatches": sum(1 for ln in lines if ln.verified == "mismatch"),
        "no_part_id": sum(1 for ln in lines if ln.status == "no-part-id"),
        "unverified": sum(1 for ln in lines if ln.status == "unverified"),
        "classes": classes,
        # coverage over FITTED pieces only: dnp/external/no-part are not
        # sourced from the catalog, so they neither help nor hurt
        "fitted_coverage": (round(covered_refs / fitted_refs, 4)
                            if fitted_refs else None),
        "basic_lines": sum(1 for ln in fitted if ln.part and ln.part.basic),
        "extended_lines": sum(1 for ln in fitted
                              if ln.part and not ln.part.basic),
        "preferred_lines": sum(1 for ln in fitted
                               if ln.part and ln.part.preferred),
        "extended_fee_est": round(extended_fee * sum(
            1 for ln in fitted if ln.part and not ln.part.basic), 2),
        "priced_lines": len(priced),
        "est_cost": round(
            sum((ln.ext_price for ln in priced if ln.ext_price is not None), 0.0), 4),
    }


# --------------------------------------------------------------------------- #
# BOM lockfile — reproducible orders + drift detection (P2-1)
# --------------------------------------------------------------------------- #
LOCK_SCHEMA_VERSION = "1"


def make_lock(lines: list[BomLine], *, qty: int) -> dict:
    """Snapshot a checked line list for ``jlc bom --lock``.

    Freezes each line's resolved identity and market state (C-number, MPN,
    unit price, stock, Basic/Extended, need) plus a timestamp — the
    order-time ground truth that ``--against-lock`` later diffs against.
    """
    from datetime import datetime, timezone
    rows = []
    for ln in sorted(lines, key=lambda x: x.refs[0] if x.refs else ""):
        rows.append({
            "refs": sorted(ln.refs),
            "value": ln.value, "footprint": ln.footprint,
            "bom_class": ln.bom_class,
            "lcsc": ln.lcsc, "mpn": ln.mpn or (ln.part.mpn if ln.part else None),
            "status": ln.status, "need": ln.need,
            "unit_price": ln.unit_price,
            "stock": ln.part.stock if ln.part else None,
            "basic": ln.part.basic if ln.part else None,
        })
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "qty": qty,
        "lines": rows,
    }


def diff_lock(lines: list[BomLine], lock: dict) -> list[dict]:
    """Drift between a fresh check and a lockfile (``--against-lock``).

    Lines pair by their sorted refs signature (stable across C-number edits,
    so an id change is itself reported as drift). Kinds: ``id_changed``,
    ``price_changed``, ``stock_below_need`` (newly), ``gone`` (was
    purchasable, now not-found — EOL suspect), ``status_changed``,
    ``line_added`` / ``line_removed``.
    """
    def _key(refs: list[str]) -> tuple:
        return tuple(sorted(refs))

    locked = {_key(r.get("refs") or []): r for r in lock.get("lines") or []}
    current = {_key(ln.refs): ln for ln in lines}
    drift: list[dict] = []

    for key, ln in current.items():
        old = locked.get(key)
        refs = ",".join(key)
        if old is None:
            drift.append({"kind": "line_added", "refs": refs,
                          "detail": f"not in the lockfile ({ln.status})"})
            continue
        if (old.get("lcsc") or None) != (ln.lcsc or None):
            drift.append({"kind": "id_changed", "refs": refs,
                          "detail": f"{old.get('lcsc')} -> {ln.lcsc}"})
        was_buyable = old.get("status") in ("ok", "low-stock")
        if was_buyable and ln.status == "not-found":
            drift.append({"kind": "gone", "refs": refs,
                          "detail": f"{old.get('lcsc')} vanished from the "
                                    "catalog — EOL suspect"})
        elif old.get("status") != ln.status:
            drift.append({"kind": "status_changed", "refs": refs,
                          "detail": f"{old.get('status')} -> {ln.status}"})
        old_price = old.get("unit_price")
        if (old_price is not None and ln.unit_price is not None
                and abs(float(old_price) - ln.unit_price) > 1e-9):
            drift.append({"kind": "price_changed", "refs": refs,
                          "detail": f"${float(old_price):.4f} -> "
                                    f"${ln.unit_price:.4f}"})
        old_stock = old.get("stock")
        now_stock = ln.part.stock if ln.part else None
        if (now_stock is not None and now_stock < ln.need
                and (old_stock is None or old_stock >= int(old.get("need") or 0))):
            drift.append({"kind": "stock_below_need", "refs": refs,
                          "detail": f"stock {now_stock} < need {ln.need}"})

    for key, old in locked.items():
        if key not in current:
            drift.append({"kind": "line_removed", "refs": ",".join(key),
                          "detail": f"was {old.get('lcsc') or old.get('mpn')}"})
    return drift


def bom_diff(a: Schematic, b: Schematic) -> list[dict]:
    """Per-component BOM delta between two schematic revisions (offline).

    The direct answer to "I changed the circuit — what does purchasing see?":
    added/removed parts, value edits, order-id edits and assembly-class flips
    (fitted -> dnp is an intent change, not a cosmetic one). Components pair
    by designator — line-level grouping would misread a value edit inside a
    shared-value group as an add+remove. Cost deltas need the live catalog:
    freeze each revision with ``--lock`` and diff the lockfiles for money.
    """
    def _index(sch: Schematic) -> dict[str, tuple]:
        out: dict[str, tuple] = {}
        for comp in _real_components(sch):
            if comp.designator in out:
                continue                     # multi-unit: first placement wins
            klass, _note = bom_policy.classify(comp)
            hit, mpn = _lcsc_of(comp), _mpn_of(comp)
            ident = (hit[1] if hit else None) or mpn
            out[comp.designator] = (_clean(comp.value), ident, klass)
        return out

    old, new = _index(a), _index(b)
    out: list[dict] = []
    for ref, (value, ident, klass) in new.items():
        prior = old.get(ref)
        if prior is None:
            out.append({"kind": "line_added", "refs": ref,
                        "detail": f"{value or '?'} [{ident or 'no id'}]"})
            continue
        p_value, p_ident, p_klass = prior
        if (p_value or "") != (value or ""):
            out.append({"kind": "value_changed", "refs": ref,
                        "detail": f"{p_value or '?'} -> {value or '?'}"})
        if p_ident != ident:
            out.append({"kind": "id_changed", "refs": ref,
                        "detail": f"{p_ident or 'no id'} -> {ident or 'no id'}"})
        if p_klass != klass:
            out.append({"kind": "class_changed", "refs": ref,
                        "detail": f"{p_klass} -> {klass}"})
    for ref, (value, ident, _k) in old.items():
        if ref not in new:
            out.append({"kind": "line_removed", "refs": ref,
                        "detail": f"{value or '?'} [{ident or 'no id'}]"})
    return sorted(out, key=lambda d: (d["kind"], d["refs"]))
