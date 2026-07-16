"""`akcli` verification commands: ``check diff verify pinmap expected relink-symbols``.

Lint-style checks and net-equivalence proofs over one or two schematics, the
``expected`` pin-table extractor that feeds ``pinmap --expected``, and the
``relink-symbols`` lib_symbols re-embedder. Heavy imports stay LAZY per handler.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import config as _config
from .. import report as _report
from ..errors import EXIT
from ._shared import (
    _add_exit_policy_flags,
    _dumps,
    _emit,
    _ExitWith,
    _findings_exit,
    _load_cfg,
    _load_schematic,
    _require_path,
    _schematic_meta,
    _stamp,
)


def _run_check(name: str, sch, cfg, args: argparse.Namespace) -> list:
    """Run one check by name, importing lazily; missing ERC degrades gracefully."""
    if name == "erc":
        try:
            from ..checks import erc  # lazy; may not exist yet
        except ImportError:
            sys.stderr.write("note: ERC check unavailable in this build; skipped\n")
            return []
        return erc.run(sch, cfg)
    if name == "power":
        from ..checks import power
        return power.run(sch, cfg)
    if name == "bom":
        from ..checks import bom
        return bom.run(sch)
    if name == "layout":
        from ..checks import layout
        return layout.run(args.path)
    if name == "nets":
        from ..checks import nets
        out = nets.run(sch, cfg)
        # geom near-miss lint reads the raw s-expression primitives, so it is
        # KiCad-only; the suffix gate keeps default Altium runs quiet.
        if str(args.path).lower().endswith(".kicad_sch"):
            from ..checks import geom
            out.extend(geom.run(args.path))
        return out
    if name == "libsync":
        from ..checks import libsync
        return libsync.run(args.path, lib_dirs=getattr(args, "symbols", None))
    if name == "pairs":
        from ..checks import pairs
        return pairs.run(sch, cfg)
    return []


def _cmd_check(args: argparse.Namespace) -> int:
    path = _require_path(args.path)
    sch = _load_schematic(path)
    cfg = _load_cfg(args, path)

    which: list[str] = []
    if args.erc:
        which.append("erc")
    if args.power:
        which.append("power")
    if args.bom:
        which.append("bom")
    if getattr(args, "layout", False):
        which.append("layout")
    if getattr(args, "nets", False):
        which.append("nets")
    if getattr(args, "libsync", False):
        which.append("libsync")
    if getattr(args, "pairs", False):
        which.append("pairs")
    intent_file = getattr(args, "intent", None)
    contract_file = getattr(args, "contract", None)
    if not which and not intent_file and not contract_file:
        # --intent/--contract alone are pure assertions, like any other selector.
        which = ["erc", "power", "bom", "nets"]
        if str(path).lower().endswith(".kicad_sch"):
            which.append("layout")
        # name-level pair/bus continuity; [check].pairs = false opts out
        if cfg.check.get("pairs", True):
            which.append("pairs")

    findings: list = []
    for name in which:
        findings.extend(_run_check(name, sch, cfg, args))
    if intent_file:
        from ..checks import intent as intent_mod
        spec = intent_mod.load(intent_file)   # BAD_CONFIG/PROTOCOL_MISMATCH via main
        findings.extend(intent_mod.run(sch, spec))
    if contract_file:
        from ..checks import contract as contract_mod
        doc = contract_mod.load(contract_file)  # BAD_CONFIG/PROTOCOL_MISMATCH via main
        findings.extend(contract_mod.run(sch, doc))

    findings, waived, demoted = _report.apply_waivers(findings, cfg.waivers)

    meta = _schematic_meta(sch)
    # Always surface waiver activity so a run cleaned only by waivers is never
    # mistaken for an intrinsically clean board.
    meta["config_waived"] = f"{waived} ({demoted} demoted)"
    fmt = getattr(args, "format", None) or ("json" if args.json else "text")
    _emit(_report.render(findings, fmt, meta, source=str(path)))
    # one exit policy for every findings-emitting command (see _shared)
    return _findings_exit(findings, args)


def _cmd_diff(args: argparse.Namespace) -> int:
    a = _load_schematic(_require_path(args.path, "first schematic"))
    b = _load_schematic(_require_path(args.other, "second schematic"))
    from ..checks import diff as diffmod
    rep = diffmod.run(a, b)
    findings = rep.findings()
    if args.json:
        _emit(_dumps(rep.export()))
    else:
        _emit(_report.render(findings, "text", {}))
    return _findings_exit(findings, args)


def _cmd_verify_schpcb(args: argparse.Namespace, sch_path, pcb_path) -> int:
    """`verify <sch> <board.kicad_pcb>` — schematic <-> PCB equivalence."""
    from ..checks import schpcb
    from ..readers import kicad as kreader

    sch = _load_schematic(sch_path)
    pcb = kreader.read_pcb(str(pcb_path))
    findings = schpcb.run(sch, pcb)
    if not getattr(args, "strict", False):
        findings = [f for f in findings if f.code != "SCHPCB_VALUE_MISMATCH"]
    errors = [f for f in findings
              if f.severity in (_report.Severity.ERROR, _report.Severity.CRITICAL)]
    equivalent = not errors

    if args.json:
        _emit(_dumps(_stamp({
            "equivalent": equivalent,
            "mode": "sch-pcb",
            "schematic": str(sch_path),
            "board": str(pcb_path),
            "components": {"sch": len(sch.components), "pcb": len(pcb.footprints)},
            "pads": len(pcb.pads),
            "findings": [
                {"code": f.code, "severity": f.severity.value,
                 "message": f.message, "anchors": f.anchors}
                for f in findings
            ],
        })))
    else:
        lines = [f"SCH-PCB EQUIVALENCE: {'PASS' if equivalent else 'FAIL'}",
                 f"  components: {len(sch.components)} (sch) vs "
                 f"{len(pcb.footprints)} (pcb)   pads: {len(pcb.pads)}"]
        for f in findings:
            lines.append(f"  {f.severity.value.upper():<8} {f.code}: {f.message}")
        if not findings:
            lines.append("  refdes, footprints and net partition all match")
        _emit("\n".join(lines))
    if getattr(args, "exit_zero", False):
        return EXIT["OK"]
    return EXIT["OK"] if equivalent else EXIT["FINDINGS"]


def _cmd_verify(args: argparse.Namespace) -> int:
    """`verify <a> <b>` — net-equivalence proof between two schematics,
    or schematic <-> PCB equivalence when ``<b>`` is a ``.kicad_pcb``.

    Schematic mode PASS means: same component set, and every net's pin
    membership is identical (net *names* may differ — conversions rename
    unnamed nets). With --strict, changed component values/footprints also
    fail. PCB mode compares refdes/footprint assignment and the pad-level net
    PARTITION (net names untrusted; --strict adds value mismatches).
    """
    a_path = _require_path(args.path, "first schematic")
    b_path = _require_path(args.other, "second file")
    if str(b_path).lower().endswith(".kicad_pcb"):
        return _cmd_verify_schpcb(args, a_path, b_path)
    a = _load_schematic(a_path)
    b = _load_schematic(b_path)
    from ..checks import diff as diffmod
    rep = diffmod.run(a, b)

    comp_ok = not rep.added_components and not rep.removed_components
    nets_ok = (not rep.added_nets and not rep.removed_nets
               and not rep.member_changed_nets)
    strict_ok = not (getattr(args, "strict", False) and rep.changed_components)
    equivalent = comp_ok and nets_ok and strict_ok

    if args.json:
        _emit(_dumps(_stamp({
            "equivalent": equivalent,
            "strict": bool(getattr(args, "strict", False)),
            "components": {"a": len(a.components), "b": len(b.components)},
            "nets": {"a": len(a.nets), "b": len(b.nets)},
            "renamed_nets": [[n.name_a, n.name_b] for n in rep.renamed_nets],
            "summary": rep.summary(),
            "detail": rep.export(),
        })))
    else:
        lines = [f"CONVERSION PROOF: {'PASS' if equivalent else 'FAIL'}",
                 f"  components: {len(a.components)} vs {len(b.components)}"
                 f"  (+{len(rep.added_components)} −{len(rep.removed_components)}"
                 f" ~{len(rep.changed_components)})",
                 f"  nets:       {len(a.nets)} vs {len(b.nets)}"
                 f"  (+{len(rep.added_nets)} −{len(rep.removed_nets)}"
                 f" membership-changed {len(rep.member_changed_nets)})"]
        if rep.renamed_nets:
            names = ", ".join(f"{n.name_a}→{n.name_b}"
                              for n in rep.renamed_nets[:8])
            lines.append(f"  renamed (connectivity identical): "
                         f"{len(rep.renamed_nets)}  [{names}]")
        if not equivalent:
            for c in rep.added_components[:10]:
                lines.append(f"  + component only in B: {c.designator_b}")
            for c in rep.removed_components[:10]:
                lines.append(f"  − component only in A: {c.designator_a}")
            for n in rep.added_nets[:10]:
                lines.append(f"  + net only in B: {n.name_b}")
            for n in rep.removed_nets[:10]:
                lines.append(f"  − net only in A: {n.name_a}")
            for n in rep.member_changed_nets[:10]:
                lines.append(
                    f"  ~ net {n.name_a or n.name_b}: "
                    f"+{[f'{d}.{p}' for d, p in n.added_members]} "
                    f"−{[f'{d}.{p}' for d, p in n.removed_members]}")
        if rep.changed_components and not getattr(args, "strict", False):
            lines.append(f"  note: {len(rep.changed_components)} component(s) "
                         "differ in value/footprint (connectivity unaffected; "
                         "--strict makes this fail)")
        if rep.low_confidence:
            lines.append("  note: low-confidence net matching — inspect "
                         "`akcli diff` output")
        _emit("\n".join(lines))
    if equivalent or getattr(args, "exit_zero", False):
        return EXIT["OK"]
    return EXIT["FINDINGS"]


def _load_expected(path_str: str) -> dict:
    """Load an external pin->signal table from CSV or JSON."""
    p = Path(path_str)
    data = p.read_text(encoding="utf-8")  # FileNotFound -> exit 4 via main
    if p.suffix.lower() == ".json":
        obj = json.loads(data)
        if isinstance(obj, dict):
            return {str(k): str(v) for k, v in obj.items()}
        if isinstance(obj, list):
            out: dict = {}
            for row in obj:
                if not isinstance(row, dict):
                    continue
                key = row.get("pin") or row.get("Pin") or row.get("number")
                val = row.get("signal") or row.get("net") or row.get("name")
                if key is not None and val is not None:
                    out[str(key)] = str(val)
            return out
        raise _ExitWith(EXIT["USAGE"], "ERROR: expected JSON must be an object or array")
    # CSV
    import csv as _csv
    rows = list(_csv.reader(data.splitlines()))
    out = {}
    start = 0
    if rows and rows[0] and rows[0][0].strip().lower() in ("pin", "number", "#"):
        start = 1
    for r in rows[start:]:
        if len(r) >= 2 and r[0].strip():
            out[r[0].strip()] = r[1].strip()
    return out


def _cmd_pinmap(args: argparse.Namespace) -> int:
    path = _require_path(args.path)
    sch = _load_schematic(path)
    cfg = _load_cfg(args, path)

    if getattr(args, "mcu", None):
        cfg = _config.Config(
            mcu_designator=args.mcu,
            rails=cfg.rails,
            paths=cfg.paths,
            erc_waivers=cfg.erc_waivers,
            waivers=cfg.waivers,
            source_path=cfg.source_path,
            grid_nm=cfg.grid_nm,
        )

    expected = _load_expected(args.expected) if getattr(args, "expected", None) else None

    from ..checks import pinmap
    findings = pinmap.run(sch, cfg, expected)
    fmt = "json" if args.json else "text"
    _emit(_report.render(findings, fmt, _schematic_meta(sch)))
    return _findings_exit(findings, args)


def _cmd_expected(args: argparse.Namespace) -> int:
    """Extract an expected pin->signal table from a .dts/.overlay or pinout .md.

    Bridges the adapters into the ``pinmap --expected`` pipeline: the emitted
    JSON object ({pin: signal}) is exactly what ``--expected`` consumes. The
    schematic stays authoritative; this table is advisory input.
    """
    src = getattr(args, "input", None)
    if not src:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing input file (.dts/.overlay or .md)")
    path = Path(src)
    if not path.is_file():
        raise _ExitWith(EXIT["NOT_FOUND"], f"ERROR: file not found: {src}")

    suffix = path.suffix.lower()
    if suffix in (".dts", ".dtsi", ".overlay"):
        from ..adapters import dts as dts_adapter  # lazy
        table = dts_adapter.to_expected_table(dts_adapter.parse_dts(path))
    elif suffix in (".md", ".markdown"):
        from ..adapters import pinout_md  # lazy
        table = pinout_md.parse_pinout_md(
            path,
            key_header=getattr(args, "key_header", None),
            value_header=getattr(args, "value_header", None),
        )
    else:
        raise _ExitWith(
            EXIT["USAGE"],
            f"ERROR: unsupported input {suffix!r} (want .dts/.dtsi/.overlay or .md)",
        )

    payload = _dumps({k: table[k] for k in sorted(table)})
    out = getattr(args, "output", None)
    if out:
        Path(out).write_text(payload + "\n", encoding="utf-8")
        sys.stderr.write(f"wrote {len(table)} pin assignment(s) to {out}\n")
    else:
        _emit(payload)

    if not table:
        # An empty table would make `pinmap --expected` vacuously pass — treat
        # "nothing extracted" as a finding, not a success.
        sys.stderr.write(f"WARNING: no pin assignments found in {src}\n")
        return EXIT["FINDINGS"]
    return EXIT["OK"]


def _cmd_relink(args: argparse.Namespace) -> int:
    """`relink-symbols <sch>` — re-embed stale lib_symbols cache entries.

    Dry-run by default; ``--apply`` splices the fresh blocks in behind the
    net-membership equivalence gate (``VERIFY_FAILED`` -> exit 6, file
    untouched) and leaves ``<name>.bak``. ``missing-lib`` entries exit 6 like
    a failed op — scope with ``--only`` to silence intentionally-unavailable
    nicknames.
    """
    target = _require_path(args.target, "target .kicad_sch")
    if not str(target).lower().endswith(".kicad_sch"):
        raise _ExitWith(EXIT["USAGE"], "ERROR: relink-symbols works on a .kicad_sch")
    from .. import relink
    actions = relink.plan(target, lib_dirs=getattr(args, "libs", None),
                          only=getattr(args, "only", None))
    do_apply = bool(getattr(args, "apply", False))
    res = None
    if do_apply:
        # VERIFY_FAILED (gate refusal) / SYMBOL_NOT_FOUND -> exit 6 via main
        res = relink.apply(str(target), actions)

    replaces = [a for a in actions if a["status"] == "replace"]
    missing = [a for a in actions if a["status"] == "missing-lib"]
    if res is not None and res.get("written"):
        from .. import journal as _journal
        _journal.record(target, "relink-symbols", "applied",
                        op_count=len(res.get("replaced", [])),
                        backup=(Path(res["backup"]).name
                                if res.get("backup") else None))
    if args.json:
        # new_sexpr is a full symbol block; strip it for readability
        slim = [{k: v for k, v in a.items() if k != "new_sexpr"} for a in actions]
        _emit(_dumps(_stamp({
            "actions": slim,
            "applied": bool(res and res["written"]),
            "replaced": (res or {}).get("replaced", []),
            "backup": (res or {}).get("backup"),
        })))
    else:
        lines = []
        for a in actions:
            detail = f" — {a['detail']}" if a.get("detail") else ""
            lines.append(f"  {a['status']:<11} {a['lib_id']}  "
                         f"[{a['source'] or 'no source'}]{detail}")
        if not lines:
            lines.append("  (no embedded lib_symbols entries matched)")
        if res is not None and res["written"]:
            bak = Path(res["backup"]).name if res.get("backup") else None
            lines.append(f"status: APPLIED — re-embedded {len(res['replaced'])} "
                         f"symbol(s) into {target.name}"
                         + (f" (backup {bak}; `akcli undo` reverts)" if bak else ""))
        elif do_apply:
            lines.append("status: nothing to replace — file untouched")
        elif replaces:
            lines.append(f"status: dry-run — {len(replaces)} replacement(s) "
                         "pending; re-run with --apply")
        else:
            lines.append("status: dry-run — nothing to replace")
        _emit("\n".join(lines))
    return EXIT["OPLIST"] if missing else EXIT["OK"]


def register(sub, common) -> None:
    p = sub.add_parser("check", parents=[common], help="run ERC/power/BOM/layout checks")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("--erc", action="store_true", help="run ERC checks")
    p.add_argument("--power", action="store_true", help="run power-rail checks")
    p.add_argument("--bom", action="store_true", help="run BOM-hygiene checks")
    p.add_argument("--layout", action="store_true",
                   help="run geometric-overlap lint (.kicad_sch only)")
    p.add_argument("--nets", action="store_true",
                   help="run connectivity-hygiene checks (single-pin nets, "
                        "off-grid pins, wire/pin/label attachment near-misses)")
    p.add_argument("--pairs", action="store_true",
                   help="run differential-pair / bus-continuity checks "
                        "(_P/_N, D+/D-, CAN_H/_L partners; D0..D7 index gaps)")
    p.add_argument("--intent", metavar="FILE",
                   help="assert a JSON design-intent file (see `akcli nets "
                        "--intent-snapshot`) against the built netlist")
    p.add_argument("--contract", metavar="FILE",
                   help="assert a TOML design-contract file (require/forbid "
                        "pin-net, pin-pair topology, values, NC, approved "
                        "exceptions with owner/expiry)")
    p.add_argument("--libsync", action="store_true",
                   help="check embedded lib_symbols freshness (pin-signature "
                        "drift vs --symbols sources; old-format heuristic "
                        "without them)")
    p.add_argument("--symbols", metavar="PATH", action="append",
                   help="symbol source dir or .kicad_sym for --libsync "
                        "(repeatable)")
    _add_exit_policy_flags(p)
    p.add_argument("--format", choices=["text", "json", "sarif", "junit"],
                   help="output format (sarif: GitHub code scanning; junit: CI test reporters)")
    p.set_defaults(handler=_cmd_check)

    p = sub.add_parser("diff", parents=[common], help="net-level diff of two schematics")
    p.add_argument("path", nargs="?", help="schematic A")
    p.add_argument("other", nargs="?", help="schematic B")
    _add_exit_policy_flags(p)
    p.set_defaults(handler=_cmd_diff)

    p = sub.add_parser("verify", parents=[common],
                       help="net-equivalence proof: two schematics, or "
                            "schematic vs .kicad_pcb (pad-net partition)")
    p.add_argument("path", nargs="?", help="schematic A (the reference)")
    p.add_argument("other", nargs="?",
                   help="schematic B (the candidate) or a .kicad_pcb board")
    p.add_argument("--strict", action="store_true",
                   help="also fail on component value/footprint differences "
                        "(sch-pcb mode: include value mismatches)")
    p.add_argument("--exit-zero", action="store_true", help="always exit 0")
    p.set_defaults(handler=_cmd_verify)

    p = sub.add_parser("pinmap", parents=[common], help="MCU pin->net map + cross-check")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("--mcu", metavar="REF", help="MCU designator (overrides config)")
    p.add_argument("--expected", metavar="FILE",
                   help="expected pin->signal table (.csv or .json)")
    _add_exit_policy_flags(p)
    p.set_defaults(handler=_cmd_pinmap)

    p = sub.add_parser("expected", parents=[common],
                       help="extract an expected pin->signal table from .dts/.overlay or pinout .md")
    p.add_argument("input", nargs="?", help="input file (.dts, .dtsi, .overlay, .md)")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="write JSON here instead of stdout (feed to pinmap --expected)")
    p.add_argument("--key-header", metavar="NAME",
                   help="markdown: explicit pin/GPIO column header")
    p.add_argument("--value-header", metavar="NAME",
                   help="markdown: explicit signal/net column header")
    p.set_defaults(handler=_cmd_expected)

    p = sub.add_parser("relink-symbols", parents=[common],
                       help="re-embed stale lib_symbols cache entries from "
                            "fresh .kicad_sym libraries (dry-run unless --apply)")
    p.add_argument("target", nargs="?", help="the .kicad_sch to relink")
    p.add_argument("--libs", metavar="DIR", action="append",
                   help="symbol library dir or .kicad_sym file (repeatable); "
                        "default: the KiCad.app SharedSupport symbols dir "
                        "if it exists")
    p.add_argument("--only", metavar="NICKS",
                   help="comma-separated library nicknames (or full lib_ids) "
                        "to consider")
    p.add_argument("--apply", action="store_true",
                   help="write the replacements (default: preview only; "
                        "net-membership equivalence gated, leaves <name>.bak)")
    p.set_defaults(handler=_cmd_relink)
