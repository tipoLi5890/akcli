"""`akcli jlc` — JLCPCB/LCSC part commands (the ONLY networked subcommands).

``jlc search`` / ``show`` / ``bom`` / ``datasheet`` / ``add`` — keyword search,
part detail (+ optional EasyEDA metadata), BOM-vs-catalog stock/price check
(with ``--fix`` writing LCSC part numbers), datasheet resolution/fetch, and the
in-process LCSC->KiCad converter. Every network dependency is imported LAZILY
so offline paths never touch it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..errors import EXIT
from ._shared import (
    _draw_exit,
    _dumps,
    _emit,
    _ExitWith,
    _load_schematic,
    _require_path,
)


# Catalog depth for an *exact*-MPN resolution (datasheet --resolve-mpn / the
# standalone-MPN path). The jlcsearch mirror ranks longer/marketing variants
# ahead of a short generic MPN, so a depth of 10 could push the true exact
# casefold match past the cut and mis-report the part as not-found; search wider
# and let the exact-match filter pick it out.
_MPN_RESOLVE_LIMIT = 100


def _jlc_price(p) -> str:
    return f"${p:.4f}" if isinstance(p, (int, float)) else "-"


def _jlc_table(parts: list) -> str:
    """Render search results as a fixed-width table (one part per row)."""
    header = ("LCSC", "MPN", "PACKAGE", "STOCK", "PRICE", "B", "DESCRIPTION")
    rows = [header]
    for p in parts:
        desc = p.description or p.category or ""
        rows.append((
            p.lcsc or "-",
            (p.mpn or "-")[:28],
            p.package or "-",
            str(p.stock),
            _jlc_price(p.price),
            "B" if p.basic else ("P" if p.preferred else "-"),
            desc[:40],
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    out = []
    for r in rows:
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
    return "\n".join(out)


def _jlc_detail(p) -> str:
    """Render a single part as a key/value block."""
    lines = [
        f"LCSC:        {p.lcsc or '-'}",
        f"MPN:         {p.mpn or '-'}",
        f"description: {p.description or '-'}",
        f"package:     {p.package or '-'}",
        f"category:    {p.category or '-'}",
        f"stock:       {p.stock}",
        f"unit price:  {_jlc_price(p.price)}",
        f"basic:       {'yes' if p.basic else 'no'}",
        f"preferred:   {'yes' if p.preferred else 'no'}",
        f"datasheet:   {p.datasheet or '-'}",
    ]
    sub = p.attributes.get("subcategory")
    if sub:
        lines.append(f"subcategory: {sub}")
    return "\n".join(lines)


def _cmd_jlc(args: argparse.Namespace) -> int:
    """No subcommand given: print usage."""
    raise _ExitWith(
        EXIT["USAGE"],
        "ERROR: use `akcli jlc search <query>`, `akcli jlc show <C-number>`, "
        "`akcli jlc bom <sch>`, `akcli jlc datasheet <C-number|MPN|sch>`, "
        "or `akcli jlc add <C-number>`",
    )


def _cmd_jlc_search(args: argparse.Namespace) -> int:
    query = getattr(args, "query", None)
    if not query:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing search query")
    from ..parts import search as parts_search  # lazy: keeps network out of offline paths
    try:
        results = parts_search.search(query, limit=getattr(args, "limit", None) or 20,
                                      cache_dir=parts_search.default_cache_dir())
    except parts_search.JlcNetworkError as exc:
        sys.stderr.write(f"ERROR: NETWORK: {exc.message}\n")
        return EXIT["TOOL_MISSING"]
    if not results:
        sys.stderr.write(f"no parts found for {query!r}\n")
        if args.json:
            _emit(_dumps([]))
        return EXIT["OK"]
    if args.json:
        _emit(_dumps([p.to_dict() for p in results]))
    else:
        _emit(_jlc_table(results))
    return EXIT["OK"]


def _easyeda_enrich(lcsc: str):
    """Best-effort EasyEDA metadata/3D-availability lookup; never raises.

    Returns an ``EasyEdaInfo`` or ``None`` — a failed/absent EasyEDA lookup must never
    break ``jlc show``; it just omits the EasyEDA-derived fields.
    """
    try:
        from ..parts import easyeda  # lazy: keeps network out of offline paths
        return easyeda.lookup(lcsc)
    except Exception:  # EasyEdaError or anything unexpected -> degrade gracefully
        return None


def _enrich(lcsc: str):
    """Resolve EasyEDA enrichment via the top-level ``cli`` module.

    ``cli`` re-exports ``_easyeda_enrich`` and tests patch ``cli._easyeda_enrich``
    (the documented offline seam); routing the handler calls through ``cli``
    keeps that patch effective after the command-module split.
    """
    from .. import cli
    return cli._easyeda_enrich(lcsc)


def _easyeda_lines(info) -> list[str]:
    return [
        "-- EasyEDA --",
        f"3D model:     {'available' if info.has_3d else 'none'}",
        f"model uuid:   {info.model_uuid or '-'}",
        f"manufacturer: {info.manufacturer or '-'}",
        f"EasyEDA MPN:  {info.mpn or '-'}",
        f"EasyEDA pkg:  {info.package or '-'}",
        f"source:       {info.source}",
    ]


def _cmd_jlc_bom(args: argparse.Namespace) -> int:
    """`jlc bom <sch>` — BOM lines vs the JLCPCB catalog (stock/price/cost)."""
    path = _require_path(args.path)
    sch = _load_schematic(path)
    from ..parts import bom_jlc, search as parts_search  # lazy: networked
    qty = max(1, getattr(args, "qty", 1) or 1)
    fix_all = getattr(args, "fix_all", False)
    do_fix = getattr(args, "fix", False) or fix_all
    do_suggest = do_fix or getattr(args, "suggest", False)
    if do_fix and not str(path).lower().endswith(".kicad_sch"):
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: --fix writes the schematic; it needs a .kicad_sch")
    cache = parts_search.default_cache_dir()
    try:
        lines = bom_jlc.check(
            sch, min_stock=getattr(args, "min_stock", 1) or 1, qty=qty,
            cache_dir=cache)
        if do_suggest:
            bom_jlc.suggest_parts(lines, cache_dir=cache)
    except parts_search.JlcNetworkError as exc:
        sys.stderr.write(f"ERROR: NETWORK: {exc.message}\n")
        return EXIT["TOOL_MISSING"]
    if do_fix:
        # plain --fix writes high-confidence suggestions only; --fix-all also
        # writes low-confidence ones (package matched, value unverified)
        ops = bom_jlc.fix_ops(
            lines, min_confidence=("low" if fix_all else "high"))
        if not fix_all:
            low = sum(1 for ln in lines
                      if ln.suggestion
                      and (ln.suggestion_confidence or "low") == "low")
            if low:
                sys.stderr.write(f"{low} low-confidence suggestion(s) not "
                                 "written (use --fix-all)\n")
        if not ops:
            sys.stderr.write("--fix: nothing to fix (no suggestions)\n")
        else:
            from ..writers import kicad as kwriter
            oplist = {"protocol_version": 1, "target_format": "kicad",
                      "target_file": path.name, "ops": ops}
            findings: list = []
            results = kwriter.apply(oplist, str(path), apply=True,
                                    sources=[], verify_out=findings,
                                    backup_dir=path.parent)
            code = _draw_exit(results, findings)
            if code != EXIT["OK"]:
                return code
            fixed = [ln for ln in lines if ln.suggestion]
            for ln in fixed:
                sys.stderr.write(
                    f"fixed {','.join(ln.refs)}: "
                    f"{ln.lcsc_key or 'LCSC'} = {ln.suggestion.lcsc} "
                    f"({ln.suggestion.mpn}) — verify against the datasheet\n")
            # re-check so the report reflects the written ids
            sch = _load_schematic(path)
            try:
                lines = bom_jlc.check(
                    sch, min_stock=getattr(args, "min_stock", 1) or 1,
                    qty=qty, cache_dir=cache)
            except parts_search.JlcNetworkError as exc:
                sys.stderr.write(f"ERROR: NETWORK: {exc.message}\n")
                return EXIT["TOOL_MISSING"]
    csv_out = getattr(args, "csv", None)
    if csv_out:
        text = bom_jlc.to_jlc_csv(lines)
        if csv_out == "-":
            sys.stdout.write(text)          # CSV replaces the table: stdout = data
        else:
            Path(csv_out).write_text(text, encoding="utf-8")
            sys.stderr.write(f"wrote JLCPCB BOM CSV: {csv_out}\n")
    agg = bom_jlc.totals(lines)
    if csv_out == "-":
        pass
    elif args.json:
        _emit(_dumps({"qty": qty, "lines": [ln.to_dict() for ln in lines],
                      "totals": agg}))
    else:
        rows = [("REFS", "QTY", "NEED", "VALUE", "PART", "STATUS",
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
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
        _emit("\n".join(
            "  ".join(c.ljust(w) for c, w in zip(r[:-1], widths)) + "  " + r[-1]
            for r in rows).rstrip())
        summary = (f"{agg['lines']} line(s): {agg['ok']} ok, "
                   f"{agg['problems']} problem(s), "
                   f"{agg['no_part_id']} without a part id")
        if agg["priced_lines"]:
            summary += (f" · est. parts cost ${agg['est_cost']:.2f} "
                        f"for {qty} board(s)"
                        + (f" ({agg['priced_lines']}/{agg['lines']} lines priced)"
                           if agg["priced_lines"] < agg["lines"] else ""))
        sys.stdout.flush()          # keep the table above the stderr summary
        sys.stderr.write(summary + "\n")
    if agg["problems"] and not getattr(args, "exit_zero", False):
        return EXIT["FINDINGS"]
    return EXIT["OK"]


def _resolve_mpn_only_rows(rows: list, cache) -> None:
    """In-place: pin a C-number onto ``no-lcsc`` rows via exact MPN match.

    One catalog search per *distinct* MPN (casefold-deduped across BOM
    refs) — misses are rewritten to ``not-found`` with the nearest-MPN
    hint, same as the standalone-MPN lookup path. Network errors propagate
    (``JlcNetworkError``) so the caller can map them to exit 7.
    """
    from ..parts import search as parts_search  # lazy: networked

    pending = [r for r in rows if r.status == "no-lcsc" and r.mpn]
    by_mpn: dict[str, list] = {}
    for r in pending:
        by_mpn.setdefault(r.mpn.casefold(), []).append(r)
    for key, group in by_mpn.items():
        mpn = group[0].mpn
        results = parts_search.search(mpn, limit=_MPN_RESOLVE_LIMIT,
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


def _cmd_jlc_datasheet(args: argparse.Namespace) -> int:
    """`jlc datasheet <target>` — resolve (and fetch) datasheet PDFs.

    ``target`` is a C-number, an MPN (exact catalog match, same policy as
    ``jlc bom``), or a schematic whose BOM lines carry LCSC parameters.
    Resolution goes through the EasyEDA component record (the jlcsearch
    mirror never carries datasheet links; lcsc.com bot-gates direct fetches).
    """
    target = getattr(args, "target", None)
    if not target:
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: give a C-number (C2984661), an MPN, or a "
                        "schematic path")
    from ..parts import datasheet as ds  # lazy: networked
    from ..parts import easyeda
    from ..parts import search as parts_search
    cache = parts_search.default_cache_dir()

    if ds._CNUM_RE.match(target.strip()):
        rows = [ds.DatasheetRow(lcsc=target.strip())]
    elif Path(target).exists():
        sch = _load_schematic(_require_path(target))
        rows = ds.rows_for_schematic(sch)
        if getattr(args, "resolve_mpn", False):
            try:
                _resolve_mpn_only_rows(rows, cache)
            except parts_search.JlcNetworkError as exc:
                sys.stderr.write(f"ERROR: NETWORK: {exc.message}\n")
                return EXIT["TOOL_MISSING"]
    else:
        # MPN: exact match against the catalog, prefer in-stock/Basic depth
        try:
            results = parts_search.search(target, limit=_MPN_RESOLVE_LIMIT,
                                          cache_dir=cache)
        except parts_search.JlcNetworkError as exc:
            sys.stderr.write(f"ERROR: NETWORK: {exc.message}\n")
            return EXIT["TOOL_MISSING"]
        exact = [p for p in results if p.mpn.casefold() == target.casefold()]
        exact.sort(key=lambda p: (p.stock > 0, p.basic, p.stock), reverse=True)
        if exact:
            rows = [ds.DatasheetRow(lcsc=exact[0].lcsc, mpn=exact[0].mpn)]
        else:
            near = f" (nearest: {results[0].mpn})" if results else ""
            rows = [ds.DatasheetRow(mpn=target, status="not-found",
                                    note="no exact MPN match" + near)]

    try:
        for i, row in enumerate(rows):
            if row.status or not row.lcsc:
                continue
            r = ds.resolve(row.lcsc, cache_dir=cache)
            r.refs = row.refs
            rows[i] = r
    except easyeda.EasyEdaError as exc:
        sys.stderr.write(f"ERROR: NETWORK: {exc.message}\n")
        return EXIT["TOOL_MISSING"]

    out_dir = None
    if getattr(args, "fetch", False):
        out_dir = (Path(args.out).expanduser() if getattr(args, "out", None)
                   else ds.default_dir())
        for row in rows:
            if row.status != "resolved":
                continue
            dest = out_dir / ds.pdf_filename(row.lcsc, row.mpn)
            try:
                path, downloaded = ds.fetch_pdf(
                    row.url, dest, force=getattr(args, "force", False))
                row.path = str(path)
                row.status = "fetched" if downloaded else "cached"
            except easyeda.EasyEdaError as exc:
                if exc.kind in ("network", "timeout"):
                    sys.stderr.write(f"ERROR: NETWORK: {exc.message}\n")
                    return EXIT["TOOL_MISSING"]
                row.status = "fetch-failed"   # content problem: report, go on
                row.note = exc.message

    ok = sum(1 for r in rows if r.status in ("resolved", "fetched", "cached"))
    page_link = sum(1 for r in rows if r.status == "page-link")
    problems = sum(1 for r in rows
                   if r.status in ("not-found", "no-link", "fetch-failed"))
    no_lcsc = sum(1 for r in rows if r.status == "no-lcsc")

    if args.json:
        _emit(_dumps({"rows": [r.to_dict() for r in rows], "ok": ok,
                      "page_link": page_link, "problems": problems,
                      "no_lcsc": no_lcsc,
                      "out_dir": str(out_dir) if out_dir else None}))
    else:
        for r in rows:
            name = " ".join(x for x in (r.lcsc, r.mpn) if x) or "?"
            what = r.path or r.url or (f"- ({r.note})" if r.note else "-")
            line = f"{r.status:<12} {name:<30} {what}"
            if r.refs:
                line += f"   [{','.join(r.refs)}]"
            _emit(line)
        _emit(f"# {len(rows)} row(s): {ok} ok, {page_link} page-link(s) "
              f"(browser/WebFetch takes it from here), {problems} "
              f"problem(s), {no_lcsc} without an LCSC id")
        if out_dir is not None:
            _emit(f"# saved under {out_dir}")
    if (problems or page_link) and not getattr(args, "exit_zero", False):
        return EXIT["FINDINGS"]
    return EXIT["OK"]


def _cmd_jlc_show(args: argparse.Namespace) -> int:
    lcsc = getattr(args, "lcsc", None)
    if not lcsc:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing LCSC C-number")
    from ..parts import search as parts_search  # lazy
    try:
        part = parts_search.get(lcsc, cache_dir=parts_search.default_cache_dir())
    except parts_search.JlcNetworkError as exc:
        sys.stderr.write(f"ERROR: NETWORK: {exc.message}\n")
        return EXIT["TOOL_MISSING"]
    if part is None:
        sys.stderr.write(f"no part {lcsc!r} found\n")
        if args.json:
            _emit(_dumps(None))
        return EXIT["OK"]

    info = _enrich(part.lcsc or lcsc) if getattr(args, "easyeda", False) else None

    if args.json:
        payload = part.to_dict()
        if getattr(args, "easyeda", False):
            payload["easyeda"] = info.to_dict() if info is not None else None
        _emit(_dumps(payload))
    else:
        out = _jlc_detail(part)
        if info is not None:
            out += "\n" + "\n".join(_easyeda_lines(info))
        elif getattr(args, "easyeda", False):
            out += "\n-- EasyEDA --\n(metadata unavailable)"
        _emit(out)
    return EXIT["OK"]


_VERIFY_CAVEAT = (
    "NOTE: symbol/footprint/3D converted from EasyEDA/LCSC CAD data. Verify pin "
    "mapping, footprint dimensions, and 3D alignment against the datasheet before use."
)

_ADD_EXIT = {
    "NETWORK": EXIT["TOOL_MISSING"],
    "CONVERT_PART_NOT_FOUND": EXIT["NOT_FOUND"],
    "CONVERT_FAILED": EXIT["OPLIST"],
    "CONVERT_NO_ARTIFACTS": EXIT["OPLIST"],
}


def _read_symbol_name(kicad_sym_path: str) -> str | None:
    """Read the first symbol id from a produced ``.kicad_sym`` (don't guess from files)."""
    try:
        from ..readers import kicad_lib  # lazy
        lib = kicad_lib.read(kicad_sym_path)
    except Exception:
        return None
    return lib.symbols[0].name if lib.symbols else None


def _build_place_oplist(result, args, value: str | None) -> dict | None:
    """Build a one-op ``place_component`` op-list from a successful conversion.

    ``lib_id``'s symbol name is read from the produced ``.kicad_sym`` (the converter
    names by component name, not the C-number); the footprint id comes from the
    produced ``.kicad_mod`` stem. Returns ``None`` if no symbol artifact was produced.
    """
    from ..ops import PROTOCOL_VERSION

    lib_name = getattr(args, "lib_name", None) or "akcli"
    sym_art = next((a for a in result.artifacts if a.endswith(".kicad_sym")), None)
    if sym_art is None:
        return None
    sym_name = _read_symbol_name(sym_art)
    if not sym_name:
        return None
    fp_art = next((a for a in result.artifacts if a.endswith(".kicad_mod")), None)
    fp_name = Path(fp_art).stem if fp_art else sym_name

    fp_lib = getattr(args, "footprint_lib", None) or "footprint"
    x_mil, y_mil = args.at
    op: dict = {
        "op": "place_component",
        "lib_id": f"{lib_name}:{sym_name}",
        "designator": args.designator,
        "x_mil": float(x_mil),
        "y_mil": float(y_mil),
        "footprint": f"{fp_lib}:{fp_name}",
    }
    if value:
        op["value"] = value
    return {
        "protocol_version": PROTOCOL_VERSION,
        "target_format": "kicad",
        "ops": [op],
    }


def _cmd_jlc_add(args: argparse.Namespace) -> int:
    from ..parts import search as parts_search  # lazy: reuse the C-number normalizer

    digits = parts_search._lcsc_digits(getattr(args, "lcsc", None))
    if not digits:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing/invalid LCSC C-number")
    lcsc = "C" + digits

    place = bool(getattr(args, "place", False))
    if place:
        if not getattr(args, "designator", None):
            raise _ExitWith(EXIT["USAGE"], "ERROR: --place requires --designator REF")
        if not getattr(args, "at", None):
            raise _ExitWith(EXIT["USAGE"], "ERROR: --place requires --at X Y")

    out_dir = getattr(args, "out", None) or str(Path("akcli-parts") / lcsc)
    with_3d = bool(getattr(args, "with_3d", False))

    # Advisory EasyEDA lookup: what is being fetched + whether 3D exists.
    info = _enrich(lcsc)
    if with_3d and info is not None and not info.has_3d:
        sys.stderr.write(
            f"warning: no 3D model is published for {lcsc} on EasyEDA; "
            "no STEP will be produced\n"
        )

    from ..drivers import jlc2kicad  # lazy: vendored converter (networked)

    fp_lib = getattr(args, "footprint_lib", None) or "footprint"
    model_path = getattr(args, "model_path", None) or "relative"
    result = jlc2kicad.convert(
        lcsc,
        out_dir,
        with_3d=with_3d,
        lib_name=getattr(args, "lib_name", None) or "akcli",
        footprint_lib=fp_lib,
        model_path=model_path,
        force=bool(getattr(args, "force", False)),
    )

    if result.error_code is not None:
        sys.stderr.write(f"ERROR: {result.error_code}: {result.message}\n")
        return _ADD_EXIT.get(result.error_code, EXIT["OPLIST"])

    if fp_lib == "footprint":
        sys.stderr.write(
            "note: the symbol's Footprint field uses the nickname 'footprint:'; "
            "pass --footprint-lib <nickname> to match your project's "
            "fp-lib-table (or register the library as 'footprint').\n")
    if with_3d and model_path == "relative":
        sys.stderr.write(
            "note: 3D model paths are bare-relative (resolve only next to the "
            "library). Use --3d-path absolute for this machine, or "
            "--3d-path '${VAR}' with a configured path variable.\n")
    elif with_3d and model_path == "absolute":
        sys.stderr.write(
            "note: 3D model paths are absolute — always resolvable on this "
            "machine, NOT portable across machines/checkouts.\n")

    value = (info.mpn or info.title) if info is not None else None
    place_doc = None
    place_path = None
    if place:
        place_doc = _build_place_oplist(result, args, value)
        if place_doc is not None:
            place_path = Path(out_dir) / "place.json"
            try:
                place_path.write_text(_dumps(place_doc) + "\n", encoding="utf-8")
            except OSError:  # pragma: no cover - best-effort file write
                place_path = None
        else:
            sys.stderr.write(
                "warning: --place skipped: no KiCad symbol artifact to place\n"
            )

    if args.json:
        payload = result.to_dict()
        payload["note"] = _VERIFY_CAVEAT
        if place:
            payload["place"] = place_doc
        _emit(_dumps(payload))
    else:
        lines = [
            f"converted {lcsc} -> kicad (in-process, vendored JLC2KiCadLib)",
            f"out: {result.out_dir}",
            "artifacts:",
        ]
        for a in result.artifacts:
            lines.append(f"  {a}")
        if place and place_path is not None:
            lines.append(f"placement op-list: {place_path}")
            lines.append("  apply with: akcli draw <target.kicad_sch> --ops "
                         f"{place_path} --apply")
        lines.append(_VERIFY_CAVEAT)
        lines.append("hint: review with `akcli check`/`kicad-cli erc` before use.")
        _emit("\n".join(lines))
    return EXIT["OK"]


def register(sub, common) -> None:
    # jlc — JLCPCB/LCSC part search (needs network; powered by jlcsearch)
    p = sub.add_parser("jlc", parents=[common],
                       help="search JLCPCB/LCSC parts via jlcsearch (needs network)")
    p.set_defaults(handler=_cmd_jlc)
    jlc_sub = p.add_subparsers(dest="jlc_command", metavar="<subcommand>")

    ps = jlc_sub.add_parser("search", parents=[common],
                            help="keyword search for parts (MPN, category, C-number)")
    ps.add_argument("query", nargs="?", help="search keywords")
    ps.add_argument("--limit", type=int, default=20, metavar="N",
                    help="max results (default: 20)")
    ps.set_defaults(handler=_cmd_jlc_search)

    pb = jlc_sub.add_parser(
        "bom", parents=[common],
        help="check a schematic's BOM against the JLCPCB catalog "
             "(stock/price via LCSC/MPN parameters; networked)")
    pb.add_argument("path", nargs="?", help="input schematic (.kicad_sch/.SchDoc)")
    pb.add_argument("--qty", type=int, default=1, metavar="N",
                    help="number of boards: stock and tier pricing are "
                         "evaluated at N x per-line quantity (default: 1)")
    pb.add_argument("--min-stock", type=int, default=1, metavar="N",
                    help="flag lines with stock below N (default: 1)")
    pb.add_argument("--suggest", action="store_true",
                    help="search the catalog for not-found / no-part-id "
                         "lines (match by value + package) and print the "
                         "best candidate")
    pb.add_argument("--fix", action="store_true",
                    help="write suggested C-numbers into the schematic's "
                         "LCSC parameters (implies --suggest; .kicad_sch "
                         "only; leaves a .bak — `akcli undo` reverts; "
                         "high-confidence suggestions only)")
    pb.add_argument("--fix-all", dest="fix_all", action="store_true",
                    help="like --fix but also writes LOW-confidence "
                         "suggestions (package matched, value not verified "
                         "in the candidate description/MPN)")
    pb.add_argument("--csv", metavar="OUT.csv",
                    help="also write a JLCPCB upload BOM CSV (Comment,"
                         "Designator,Footprint,LCSC Part #); '-' writes "
                         "to stdout")
    pb.add_argument("--exit-zero", action="store_true",
                    help="always exit 0 (report mode)")
    pb.set_defaults(handler=_cmd_jlc_bom)

    pds = jlc_sub.add_parser(
        "datasheet", parents=[common],
        help="resolve/download datasheet PDFs for a C-number, an MPN, or a "
             "schematic's whole BOM (via the EasyEDA record; networked)")
    pds.add_argument("target", nargs="?",
                     help="LCSC C-number (C2984661), an exact MPN, or a "
                          "schematic path (.kicad_sch/.SchDoc)")
    pds.add_argument("--fetch", action="store_true",
                     help="download the PDF(s); the %%PDF magic is verified "
                          "so an HTML challenge page is never saved")
    pds.add_argument("--out", metavar="DIR",
                     help="download directory (default: AKCLI_DATASHEET_DIR "
                          "or ~/.cache/akcli/datasheets)")
    pds.add_argument("--force", action="store_true",
                     help="re-download even when the file already exists")
    pds.add_argument("--resolve-mpn", dest="resolve_mpn", action="store_true",
                     help="for MPN-only BOM lines, exact-match the catalog "
                          "(same policy as `jlc bom`) to pin a C-number "
                          "before resolving (one search per distinct MPN)")
    pds.add_argument("--exit-zero", action="store_true",
                     help="always exit 0 (report mode)")
    pds.set_defaults(handler=_cmd_jlc_datasheet)

    psh = jlc_sub.add_parser("show", parents=[common],
                             help="show one part by LCSC C-number (e.g. C7593)")
    psh.add_argument("lcsc", nargs="?", help="LCSC part number, e.g. C7593")
    psh.add_argument("--easyeda", action="store_true",
                     help="also query EasyEDA for metadata + 3D-model availability")
    psh.set_defaults(handler=_cmd_jlc_show)

    pa = jlc_sub.add_parser(
        "add", parents=[common],
        help="fetch an LCSC part and convert it into a KiCad library (networked)",
    )
    pa.add_argument("lcsc", nargs="?", help="LCSC part number, e.g. C2040")
    pa.add_argument("--3d", dest="with_3d", action="store_true",
                    help="also download the 3D STEP model")
    pa.add_argument("--out", metavar="DIR",
                    help="output directory (default: ./akcli-parts/<C-number>/)")
    pa.add_argument("--lib-name", metavar="NAME", default="akcli",
                    help="KiCad symbol library name (default: akcli)")
    pa.add_argument("--footprint-lib", dest="footprint_lib", metavar="NICKNAME",
                    default="footprint",
                    help="fp-lib-table nickname written into the symbol's "
                         "Footprint field AND used as the footprint output "
                         "directory (default: footprint) — set it to the "
                         "nickname your project registers or KiCad will not "
                         "find the package")
    pa.add_argument("--3d-path", dest="model_path", metavar="MODE",
                    default="relative",
                    help="how footprints reference the 3D model: 'relative' "
                         "(default; portable, resolves only next to the "
                         "library), 'absolute' (always resolves on this "
                         "machine, not portable), or a '${VAR}' prefix "
                         "(portable via a KiCad path variable)")
    pa.add_argument("--force", action="store_true",
                    help="overwrite existing artifacts")
    pa.add_argument("--place", action="store_true",
                    help="also emit a place_component op-list")
    pa.add_argument("--designator", metavar="REF",
                    help="reference designator for --place (e.g. U1)")
    pa.add_argument("--at", nargs=2, type=float, metavar=("X", "Y"),
                    help="placement position in mils for --place")
    pa.set_defaults(handler=_cmd_jlc_add)
