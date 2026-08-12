"""BOM-vs-catalog report assembly (rank 76) — the engine behind ``jlc bom``.

Everything ``akcli jlc bom`` needs to turn a schematic into report DATA:
the catalog check (single board or merged cart), optional second-source
suggestions, the fixed-width table's row tuples and the one-line summary.
Rendering (padding the columns, the CSV/Markdown writers, the JSON envelope)
stays in the CLI layer; so does mapping a failure onto an exit code —
:class:`..parts.search.JlcNetworkError` propagates on purpose.

:func:`resolve_mpn_only_rows` lives here too: ``jlc datasheet --resolve-mpn``
and ``jlc bom`` share one exact-MPN resolution policy, and it must stay one
implementation.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MPN_RESOLVE_LIMIT",
    "collect_lines",
    "resolve_mpn_only_rows",
    "summary_text",
    "table_rows",
]

# Catalog depth for an *exact*-MPN resolution (datasheet --resolve-mpn / the
# standalone-MPN path). The jlcsearch mirror ranks longer/marketing variants
# ahead of a short generic MPN, so a depth of 10 could push the true exact
# casefold match past the cut and mis-report the part as not-found; search wider
# and let the exact-match filter pick it out.
MPN_RESOLVE_LIMIT = 100


def resolve_mpn_only_rows(rows: list[Any], cache: Any) -> None:
    """In-place: pin a C-number onto ``no-lcsc`` rows via exact MPN match.

    One catalog search per *distinct* MPN (casefold-deduped across BOM
    refs) — misses are rewritten to ``not-found`` with the nearest-MPN
    hint, same as the standalone-MPN lookup path. Network errors propagate
    (``JlcNetworkError``) so the caller can map them to exit 7.
    """
    from . import search as parts_search  # lazy: networked

    pending = [r for r in rows if r.status == "no-lcsc" and r.mpn]
    by_mpn: dict[str, list[Any]] = {}
    for r in pending:
        by_mpn.setdefault(r.mpn.casefold(), []).append(r)
    for key, group in by_mpn.items():
        mpn = group[0].mpn
        results = parts_search.search(mpn, limit=MPN_RESOLVE_LIMIT,
                                      cache_dir=cache)
        exact = [p for p in results if p.mpn.casefold() == key]
        exact.sort(key=lambda p: (p.stock > 0, p.basic, p.stock), reverse=True)
        for r in group:
            if exact:
                r.lcsc = exact[0].lcsc
                r.mpn = exact[0].mpn
                r.status = ""
                r.note = ""
            else:
                near = f" (nearest: {results[0].mpn})" if results else ""
                r.status = "not-found"
                r.note = "no exact MPN match" + near


def collect_lines(sch: Any, boards: list[tuple[str, Any]] | None, *,
                  min_stock: int, qty: int, cache: Any,
                  no_part_prefixes: tuple[str, ...], offline: bool,
                  suggest: bool, alternates: bool) -> list[Any]:
    """Check a BOM against the catalog and (optionally) suggest second sources.

    ``boards`` non-``None`` selects the merged-cart path (several schematics
    become ONE order: same-C-number lines combine and price at the merged
    quantity); otherwise the single-board ``sch`` is checked.
    ``JlcNetworkError`` propagates — the CLI owns the exit code.
    """
    from . import bom_jlc  # lazy: networked

    if boards is not None:
        lines = bom_jlc.check_multi(
            boards, min_stock=min_stock, qty=qty,
            cache_dir=cache, no_part_prefixes=no_part_prefixes, offline=offline)
    else:
        lines = bom_jlc.check(
            sch, min_stock=min_stock, qty=qty,
            cache_dir=cache, no_part_prefixes=no_part_prefixes, offline=offline)
    if suggest or alternates:
        bom_jlc.suggest_parts(lines, cache_dir=cache, include_risk=alternates)
        bom_jlc.basic_alternatives(lines, cache_dir=cache)
    return lines


def table_rows(lines: list[Any]) -> list[tuple[str, ...]]:
    """Header + one row tuple per BOM line, ordered by first designator.

    Pure data assembly: every cell is already the string the table shows, but
    nothing is padded — the caller owns the column widths.
    """
    rows: list[tuple[str, ...]] = [
        ("REFS", "QTY", "NEED", "VALUE", "PART", "STATUS",
         "STOCK", "UNIT", "EXT", "B", "NOTE")]
    for ln in sorted(lines, key=lambda x: x.refs[0]):
        p = ln.part
        rows.append((
            ",".join(ln.refs[:4]) + ("…" if len(ln.refs) > 4 else ""),
            str(ln.qty),
            str(ln.need),
            (ln.value or "-")[:16],
            ln.lcsc or ln.mpn or "-",
            ln.status,
            str(p.stock) if p else "-",
            f"${ln.unit_price:.4f}" if ln.unit_price is not None else "-",
            f"${ln.ext_price:.2f}" if ln.ext_price is not None else "-",
            ("B" if p.basic else "P" if p.preferred else "-") if p else "-",
            (f"→ {ln.suggestion.lcsc} {ln.suggestion.mpn} "
             f"(stock {ln.suggestion.stock}"
             f"{', Basic' if ln.suggestion.basic else ''}) — "
             "--fix writes it" if ln.suggestion else ln.note),
        ))
    return rows


def summary_text(agg: dict[str, Any], *, qty: int, offline: bool) -> str:
    """The one-line verdict under the table (line classes, fees, est. cost)."""
    cls = agg.get("classes") or {}
    summary = (f"{agg['lines']} line(s): {agg['ok']} ok, "
               f"{agg['problems']} problem(s), "
               f"{agg['no_part_id']} without a part id")
    if agg.get("mismatches"):
        summary += f" · {agg['mismatches']} C-number MISMATCH(ES)"
    extra = [f"{cls.get(k, 0)} {k}" for k in ("dnp", "external", "no-part")
             if cls.get(k)]
    if extra:
        summary += " · " + ", ".join(extra)
    if agg.get("fitted_coverage") is not None:
        summary += f" · fitted coverage {agg['fitted_coverage']:.0%}"
    if offline and agg.get("unverified"):
        summary += f" · {agg['unverified']} unverified (offline)"
    if agg.get("extended_lines"):
        summary += (f" · {agg['basic_lines']}B/{agg['extended_lines']}E "
                    f"lines (ext. feeder fees ≈ ${agg['extended_fee_est']:.2f})")
    if agg["priced_lines"]:
        summary += (f" · est. parts cost ${agg['est_cost']:.2f} "
                    f"for {qty} board(s)"
                    + (f" ({agg['priced_lines']}/{agg['lines']} lines priced)"
                       if agg["priced_lines"] < agg["lines"] else ""))
    return summary
