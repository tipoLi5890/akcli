"""`akcli review` — engineering design review (analyze / report / explain).

Advisory by default: ``review analyze`` exits 0 whatever it finds — findings
are engineering observations with explicit confidence, not gates. ``--fail-on
SEVERITY`` opts a CI job into failing; blocking release policy stays with
``release preflight``. Heavy imports are LAZY per handler.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import report as _report
from ..errors import EXIT
from ._shared import _did_you_mean, _dumps, _emit, _ExitWith, _require_path, _stamp

_FAIL_ON = ("warning", "error", "critical")
_FORMATS = ("text", "json", "sarif", "junit", "markdown")


def _read_schematic(path: Path):
    """Format-dispatch a schematic read (.kicad_sch or Altium .SchDoc)."""
    name = path.name.lower()
    if name.endswith(".kicad_sch"):
        from ..readers import kicad
        return kicad.read_sch(str(path))
    if name.endswith(".schdoc"):
        from ..readers import altium_sch
        return altium_sch.read(str(path))
    raise _ExitWith(EXIT["USAGE"],
                    "ERROR: review analyze reads .kicad_sch or .SchDoc")


def _fail_on_exit(findings: list, threshold: str | None) -> int:
    if not threshold:
        return EXIT["OK"]
    ranks = {"warning": 2, "error": 3, "critical": 4}
    floor = ranks[threshold]
    sev_rank = {"info": 0, "note": 1, "warning": 2, "error": 3, "critical": 4}
    if any(sev_rank.get(f.severity.value, 0) >= floor for f in findings):
        return EXIT["FINDINGS"]
    return EXIT["OK"]


def _facts_root(args: argparse.Namespace, target: Path):
    """The datasheets dir: ``--facts`` explicit, else ``<sch dir>/datasheets``.

    Auto-discovery only counts when the conventional dir actually holds an
    ``extracted/`` store — an empty neighbour named ``datasheets`` is not a
    facts store.
    """
    explicit = getattr(args, "facts", None)
    if explicit:
        root = Path(explicit)
        if not root.is_dir():
            raise _ExitWith(EXIT["NOT_FOUND"],
                            f"ERROR: no such facts dir: {root}")
        return root
    probe = target.parent / "datasheets"
    if (probe / "extracted").is_dir():
        return probe
    return None


def _cmd_review_analyze(args: argparse.Namespace) -> int:
    target = _require_path(args.path, "schematic (.kicad_sch / .SchDoc)")
    sch = _read_schematic(target)
    pcb = None
    if getattr(args, "pcb", None):
        from ..readers import kicad
        pcb = kicad.read_pcb(str(_require_path(args.pcb, "board .kicad_pcb")))

    from ..review import engine
    from ..review import facts as factsmod
    root = _facts_root(args, target)
    store = factsmod.load_store(root) if root is not None else None
    gerbers = None
    if getattr(args, "gerbers", None):
        from ..readers import gerber as gerber_reader
        gdir = Path(args.gerbers)
        if not gdir.is_dir():
            raise _ExitWith(EXIT["NOT_FOUND"],
                            f"ERROR: no such gerber dir: {gdir}")
        gerbers = gerber_reader.read_gerber_dir(gdir)
        for w in gerbers.warnings:
            sys.stderr.write(f"WARNING: gerbers: {w}\n")
    profile = getattr(args, "profile", None) or "standard"
    try:
        findings, meta = engine.analyze(
            sch, pcb=pcb, profile=profile,
            detectors=getattr(args, "detector", None) or None,
            facts=store, gerbers=gerbers)
    except KeyError as exc:
        raise _ExitWith(EXIT["USAGE"], f"ERROR: unknown profile/detector {exc}")
    if store is not None and store.errors:
        for err in store.errors:
            sys.stderr.write(f"WARNING: facts store: {err}\n")

    # config waivers apply to review findings exactly as to check findings
    from ._shared import _load_cfg
    cfg = _load_cfg(args, target)
    waivers = getattr(cfg, "waivers", None) or []
    findings, waived, demoted = _report.apply_waivers(findings, waivers)
    meta["config_waived"] = f"{waived} ({demoted} demoted)"

    payload = _report.render(findings, "json", meta=meta, source=str(target))
    if getattr(args, "out", None):
        Path(args.out).write_text(payload, encoding="utf-8")
    if args.json:
        _emit(payload)
    else:
        _emit(_report.render(findings, "text", meta=meta, source=str(target)))
        if getattr(args, "out", None):
            _emit(f"wrote {args.out}")
    return _fail_on_exit(findings, getattr(args, "fail_on", None))


def _load_findings_file(path: Path) -> tuple[list, dict]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _ExitWith(EXIT["USAGE"], f"ERROR: invalid findings JSON: {exc}")
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: not a findings file (expected the "
                        "schema_version/metadata/findings envelope)")
    findings = [_report.finding_from_json(d) for d in doc["findings"]
                if isinstance(d, dict)]
    return findings, doc.get("metadata") or {}


def _cmd_review_report(args: argparse.Namespace) -> int:
    path = _require_path(args.path, "findings .json")
    fmt = getattr(args, "format", None) or "text"
    if fmt not in _FORMATS:
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: unknown format {fmt!r} "
                        f"(one of: {', '.join(_FORMATS)})")
    findings, meta = _load_findings_file(path)
    _emit(_report.render(findings, fmt, meta=meta, source=str(path)))
    return EXIT["OK"]


def _cmd_review_explain(args: argparse.Namespace) -> int:
    code = getattr(args, "code", None)
    if not code:
        raise _ExitWith(EXIT["USAGE"], "ERROR: review explain needs a rule code")
    from ..review import rules_index
    rules = rules_index()
    rule = rules.get(code.upper())
    if rule is None:
        raise _ExitWith(
            EXIT["USAGE"],
            f"ERROR: unknown rule {code!r}"
            f"{_did_you_mean(code.upper(), rules)} (see docs/review-rules.md)")
    if args.json:
        _emit(_dumps(_stamp({
            "code": rule.code, "title": rule.title, "explain": rule.explain,
            "default_severity": rule.default_severity,
            "confidence": rule.confidence, "rule_version": rule.version,
            "reference": rule.reference,
        })))
        return EXIT["OK"]
    lines = [
        f"{rule.code} — {rule.title}",
        f"  severity: {rule.default_severity}   confidence: {rule.confidence}"
        f"   rule_version: {rule.version}",
        "",
        f"  {rule.explain}",
    ]
    if rule.reference:
        lines += ["", f"  reference: {rule.reference}"]
    _emit("\n".join(lines))
    return EXIT["OK"]


# --------------------------------------------------------------------------- #
# facts store (M4)
# --------------------------------------------------------------------------- #
def _cmd_facts_add(args: argparse.Namespace) -> int:
    """`review facts add <MPN> --pdf F [--set k=V@pN ...]` — create/update."""
    from ..review import facts as fx

    mpn = getattr(args, "mpn", None)
    if not mpn:
        raise _ExitWith(EXIT["USAGE"], "ERROR: review facts add needs an MPN")
    root = Path(getattr(args, "dir", None) or "datasheets")
    pdf_arg = getattr(args, "pdf", None)
    if not pdf_arg:
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: --pdf <datasheet.pdf> is required — a fact "
                        "without a source document cannot be audited")
    pdf = Path(pdf_arg)
    if not pdf.is_file():
        raise _ExitWith(EXIT["NOT_FOUND"], f"ERROR: no such PDF: {pdf}")
    method = getattr(args, "method", None) or "manual"
    if method not in fx.EXTRACTION_METHODS:
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: --method must be one of "
                        f"{'/'.join(fx.EXTRACTION_METHODS)}")

    path = fx.facts_path(root, mpn)
    if path.exists():
        try:
            facts = fx._facts_from_doc(
                json.loads(path.read_text(encoding="utf-8")), path)
        except (json.JSONDecodeError, ValueError) as exc:
            raise _ExitWith(EXIT["USAGE"],
                            f"ERROR: existing {path.name} is unreadable: {exc}")
    else:
        facts = fx.Facts(mpn=mpn, sha256="", path=path)
    facts.mpn = mpn
    facts.extraction_method = method
    # pdf recorded RELATIVE to the datasheets dir when it lives inside it
    try:
        facts.pdf = str(pdf.resolve().relative_to(root.resolve()))
    except ValueError:
        facts.pdf = str(pdf)
    facts.sha256 = fx.sha256_file(pdf)

    added = []
    for expr in getattr(args, "set", None) or []:
        try:
            key, value, unit, page = fx.parse_set(expr)
        except ValueError as exc:
            raise _ExitWith(EXIT["USAGE"], f"ERROR: --set {exc}")
        facts.values[key] = fx.FactValue(
            key=key, unit=unit, page=page, value=value,
            sha256=facts.sha256, pdf=facts.pdf)
        added.append(key)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fx.facts_to_doc(facts), ensure_ascii=False,
                               indent=2) + "\n", encoding="utf-8")
    if args.json:
        _emit(_dumps(_stamp({"written": str(path), "mpn": mpn,
                             "sha256": facts.sha256, "facts_set": sorted(added),
                             "facts_total": len(facts.values)})))
    else:
        _emit(f"wrote {path} ({len(facts.values)} fact(s)"
              + (f"; set: {', '.join(added)}" if added else "")
              + f"; pdf sha256 {facts.sha256[:12]}…)")
        _emit("add quotes/conditions by editing the JSON, then run "
              "`akcli review facts verify`")
    return EXIT["OK"]


def _cmd_facts_verify(args: argparse.Namespace) -> int:
    """`review facts verify [MPN]` — audit the store; exit 1 on findings."""
    from ..review import facts as fx

    root = Path(getattr(args, "dir", None) or "datasheets")
    store = fx.load_store(root)
    mpn = getattr(args, "mpn", None)
    targets = ([store.lookup(mpn)] if mpn else
               [store.by_mpn[k] for k in sorted(store.by_mpn)])
    if mpn and targets == [None]:
        raise _ExitWith(EXIT["NOT_FOUND"],
                        f"ERROR: no facts for {mpn!r} under {root}/extracted")
    findings: list = []
    for err in store.errors:
        findings.append(_report.Finding(
            code="FACTS_SCHEMA_INVALID", severity=_report.Severity.ERROR,
            message=err, detector="review.facts", confidence="deterministic"))
    for facts in targets:
        findings.extend(fx.verify_facts(facts, root))
    meta = {"facts_files": len(store.by_mpn), "facts_dir": str(root)}
    _emit(_report.render(findings, "json" if args.json else "text",
                         meta=meta, source=str(root)))
    from ._shared import _findings_exit
    return _findings_exit(findings, args)


def _cmd_facts_lookup(args: argparse.Namespace) -> int:
    """`review facts lookup <MPN> [key]` — print audited facts."""
    from ..review import facts as fx

    mpn = getattr(args, "mpn", None)
    if not mpn:
        raise _ExitWith(EXIT["USAGE"], "ERROR: review facts lookup needs an MPN")
    root = Path(getattr(args, "dir", None) or "datasheets")
    store = fx.load_store(root)
    facts = store.lookup(mpn)
    if facts is None:
        raise _ExitWith(EXIT["NOT_FOUND"],
                        f"ERROR: no facts for {mpn!r} under {root}/extracted")
    key = getattr(args, "key", None)
    if key:
        v = facts.get(key)
        if v is None:
            raise _ExitWith(EXIT["NOT_FOUND"],
                            f"ERROR: {facts.mpn} has no fact {key!r} "
                            f"(has: {', '.join(sorted(facts.values)) or '-'})")
        values = {key: v}
    else:
        values = facts.values
    if args.json:
        doc = fx.facts_to_doc(facts)
        if key:
            doc["facts"] = {key: doc["facts"][key]}
        _emit(_dumps(doc))
        return EXIT["OK"]
    _emit(f"{facts.mpn}  (pdf sha256 {facts.sha256[:12]}…, "
          f"{facts.extraction_method}, {facts.quality})")
    for k in sorted(values):
        v = values[k]
        nums = ", ".join(f"{slot}={getattr(v, slot):g}"
                         for slot in ("min", "typ", "max", "value")
                         if getattr(v, slot) is not None)
        _emit(f"  {k}: {nums} {v.unit}  @p{v.page}"
              + (f"  \"{v.quote}\"" if v.quote else ""))
    return EXIT["OK"]



# --------------------------------------------------------------------------- #
# propose / diff / tree (M7)
# --------------------------------------------------------------------------- #
def _cmd_review_propose(args: argparse.Namespace) -> int:
    """`review propose <findings.json>` — declarative candidate changes."""
    from ..review import propose as prop

    path = _require_path(args.path, "findings .json")
    try:
        doc = prop.load_findings(path)
    except (ValueError, json.JSONDecodeError) as exc:
        raise _ExitWith(EXIT["USAGE"], f"ERROR: {exc}")
    out_doc = prop.build_proposals(doc, source=str(path))
    payload = _dumps(out_doc)
    if getattr(args, "out", None):
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    if args.json or not getattr(args, "out", None):
        _emit(payload)
    else:
        n = len(out_doc["proposals"])
        applyable = sum(1 for pr in out_doc["proposals"]
                        if pr["oplist_draft"] is not None)
        _emit(f"wrote {args.out}: {n} proposal(s), {applyable} with an "
              "op-list draft (run via `akcli plan` / `draw --apply`), "
              f"{n - applyable} awaiting confirmation")
    return EXIT["OK"]


def _cmd_review_testbench(args: argparse.Namespace) -> int:
    """`review testbench <sch>` — auto-generated subcircuit SPICE benches.

    Findings with a simulable claim (RC corners, divider ratios) become
    runnable cone testbenches; ngspice delivers the verdict. ``--findings``
    reuses a saved envelope, otherwise ``review analyze`` runs in-process.
    ``--deck-only`` writes the decks without an engine (exit 0); a run mode
    without libngspice is ``NGSPICE_MISSING`` (exit 7) like ``akcli sim``.
    """
    from ..review import engine
    from ..review import testbench as tbmod

    target = _require_path(args.path, "schematic (.kicad_sch / .SchDoc)")
    sch = _read_schematic(target)

    if getattr(args, "findings", None):
        doc = json.loads(Path(args.findings).read_text(encoding="utf-8"))
        fdicts = [f for f in doc.get("findings", []) if isinstance(f, dict)]
    else:
        found, _meta = engine.analyze(sch, profile="standard")
        fdicts = [_report._finding_json(f) for f in found]

    benches, skipped = tbmod.generate(sch, fdicts)
    for s in skipped:
        sys.stderr.write(f"note: testbench skipped for {s['finding_code']} "
                         f"({s['fingerprint'][:8]}): {s['reason']}\n")

    deck_only = bool(getattr(args, "deck_only", False))
    out_dir = getattr(args, "out", None)
    if deck_only:
        from ..sim import deck as _deck
        written = []
        for b in benches:
            d = _deck.build(b.schematic, b.spec, gnd=b.gnd)
            entry = {**b.describe(), "deck": d.text}
            if out_dir:
                base = Path(out_dir) / f"{b.fingerprint[:8]}_{b.kind}"
                base.parent.mkdir(parents=True, exist_ok=True)
                base.with_suffix(".deck").write_text(d.text, encoding="utf-8")
                entry["deck_path"] = str(base.with_suffix(".deck"))
                entry.pop("deck")
            written.append(entry)
        if args.json:
            _emit(_dumps({"testbench_version": tbmod.TESTBENCH_VERSION,
                          "source": str(target), "mode": "deck-only",
                          "benches": written, "skipped": skipped}))
        else:
            for e in written:
                where = e.get("deck_path", "(stdout suppressed; use --out)")
                _emit(f"deck  {e['kind']:<12} {'+'.join(e['refs']):<12} {where}")
            _emit(f"{len(written)} testbench deck(s), {len(skipped)} skipped")
        return EXIT["OK"]

    from ..sim import engine as sim_engine
    if sim_engine.available() is None:
        raise _ExitWith(EXIT["TOOL_MISSING"],
                        "ERROR: NGSPICE_MISSING: no libngspice found — "
                        "use --deck-only, or install KiCad / set AKCLI_NGSPICE")

    verdicts = [tbmod.run_bench(b, timeout=float(getattr(args, "timeout", None)
                                                 or 60.0))
                for b in benches]
    failed = [v for v in verdicts if not v["ok"]]
    if args.json:
        _emit(_dumps({
            "testbench_version": tbmod.TESTBENCH_VERSION,
            "source": str(target),
            "benches": verdicts,
            "skipped": skipped,
            "summary": {"total": len(verdicts), "passed":
                        len(verdicts) - len(failed), "failed": len(failed)},
            "ok": not failed,
        }))
    else:
        for v in verdicts:
            mark = "PASS" if v["ok"] else "FAIL"
            expect = ", ".join(
                f"{k}~{e['value']:.4g}{e.get('unit', '')}"
                for k, e in v["expect"].items())
            got = ", ".join(
                f"{k}={val:.4g}" if isinstance(val, (int, float))
                else f"{k}=?" for k, val in (v.get("measured") or {}).items())
            _emit(f"{mark}  {v['kind']:<12} {'+'.join(v['refs']):<12} "
                  f"expected {expect}  measured {got}")
            for f in v.get("findings") or []:
                _emit(f"      {f['severity'].upper()} [{f['code']}] {f['message']}")
            if v.get("error"):
                _emit(f"      engine: {v['error']}")
        _emit(f"{len(verdicts)} testbench(es): "
              f"{len(verdicts) - len(failed)} passed, {len(failed)} failed, "
              f"{len(skipped)} skipped")
    if getattr(args, "exit_zero", False):
        return EXIT["OK"]
    return EXIT["FINDINGS"] if failed else EXIT["OK"]


def _cmd_review_diff(args: argparse.Namespace) -> int:
    """`review diff <old.json> <new.json>` — fingerprint-aligned drift."""
    from ..review import diff as diffmod

    old_p = _require_path(args.old, "old findings .json")
    new_p = _require_path(args.new, "new findings .json")
    old_f, _m1 = _load_findings_file(old_p)
    new_f, _m2 = _load_findings_file(new_p)
    old_doc = {"findings": [_report._finding_json(f) for f in old_f]}
    new_doc = {"findings": [_report._finding_json(f) for f in new_f]}
    d = diffmod.diff_findings(old_doc, new_doc)
    if args.json:
        _emit(_dumps(_stamp(d)))
    else:
        _emit(diffmod.render_text(d).rstrip("\n"))
    if getattr(args, "fail_on_new", False) and d["added"]:
        return EXIT["FINDINGS"]
    return EXIT["OK"]


def _cmd_review_tree(args: argparse.Namespace) -> int:
    """`review tree <sch>` — the power structure, rail by rail."""
    from ..review import tree as treemod

    target = _require_path(args.path, "schematic (.kicad_sch / .SchDoc)")
    sch = _read_schematic(target)
    doc = treemod.power_tree(sch)
    if args.json:
        _emit(_dumps(_stamp(doc)))
    else:
        _emit(treemod.render_text(doc).rstrip("\n"))
    return EXIT["OK"]



def _cmd_review_validate(args: argparse.Namespace) -> int:
    """`review validate <candidates.json> <sch>` — the LLM deep-review gate."""
    from ..review import facts as factsmod
    from ..review import validate as val

    cand_path = _require_path(args.candidates, "candidates .json")
    target = _require_path(args.sch, "schematic (.kicad_sch / .SchDoc)")
    sch = _read_schematic(target)
    try:
        doc = json.loads(cand_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _ExitWith(EXIT["USAGE"], f"ERROR: invalid candidates JSON: {exc}")
    root = _facts_root(args, target)
    store = factsmod.load_store(root) if root is not None else None
    try:
        accepted, quarantined = val.validate_candidates(
            doc, sch, facts=store, facts_root=root)
    except ValueError as exc:
        raise _ExitWith(EXIT["USAGE"], f"ERROR: {exc}")
    meta = {
        "validate_accepted": len(accepted),
        "validate_quarantined": len(quarantined),
        "quarantined": quarantined,
        "source_format": sch.source_format,
    }
    payload = _report.render(accepted, "json", meta=meta,
                             source=str(cand_path))
    if getattr(args, "out", None):
        Path(args.out).write_text(payload, encoding="utf-8")
    if args.json:
        _emit(payload)
    else:
        _emit(f"validate: {len(accepted)} accepted (llm_reviewed), "
              f"{len(quarantined)} quarantined")
        for q in quarantined:
            code = (q.get("candidate") or {}).get("code", "?") \
                if isinstance(q.get("candidate"), dict) else "?"
            _emit(f"  QUARANTINED [{code}]")
            for r in q["reasons"]:
                _emit(f"    - {r}")
        for f in accepted:
            _emit(f"  ACCEPTED [{f.code}] {f.message}")
        if getattr(args, "out", None):
            _emit(f"wrote {args.out}")
    return EXIT["OK"]          # observations either way — never blocking


def register(sub, common) -> None:
    p = sub.add_parser(
        "review", parents=[common],
        help="engineering design review: advisory findings with explicit "
             "confidence + evidence (analyze / report / explain)")
    rev = p.add_subparsers(dest="review_command", metavar="<subcommand>")
    p.set_defaults(handler=lambda a: (
        sys.stderr.write("ERROR: use `akcli review analyze|report|explain`\n")
        or EXIT["USAGE"]))

    pa = rev.add_parser(
        "analyze", parents=[common],
        help="run review detectors over a schematic (advisory; exit 0 "
             "unless --fail-on)")
    pa.add_argument("path", nargs="?", help="schematic (.kicad_sch / .SchDoc)")
    pa.add_argument("--pcb", metavar="FILE", help="board .kicad_pcb (reserved "
                    "for pcb/cross detectors)")
    pa.add_argument("--profile", choices=sorted(("fast", "standard", "deep")),
                    default="standard", help="detector families to run")
    pa.add_argument("--detector", metavar="NAME", action="append",
                    help="run only this detector (repeatable; overrides "
                         "--profile)")
    pa.add_argument("--out", metavar="FILE",
                    help="also write the findings JSON envelope here")
    pa.add_argument("--gerbers", metavar="DIR",
                    help="fab output dir (gerber/drill) for the gerber "
                         "package checks")
    pa.add_argument("--facts", metavar="DIR",
                    help="datasheet facts dir (default: auto-discover "
                         "<sch dir>/datasheets when it holds extracted/)")
    pa.add_argument("--fail-on", dest="fail_on", choices=_FAIL_ON,
                    help="exit 1 when any finding is at/above this severity "
                         "(default: always exit 0 — review is advisory)")
    pa.set_defaults(handler=_cmd_review_analyze)

    pf = rev.add_parser(
        "facts", parents=[common],
        help="datasheet facts store: audited, PDF-pinned numbers that turn "
             "heuristic findings datasheet_backed")
    facts_sub = pf.add_subparsers(dest="facts_command", metavar="<action>")
    pf.set_defaults(handler=lambda a: (
        sys.stderr.write("ERROR: use `akcli review facts add|verify|lookup`\n")
        or EXIT["USAGE"]))

    pfa = facts_sub.add_parser(
        "add", parents=[common],
        help="create/update one MPN's facts (binds the source PDF by sha256)")
    pfa.add_argument("mpn", nargs="?", help="exact manufacturer part number")
    pfa.add_argument("--pdf", metavar="FILE", required=False,
                     help="the source datasheet PDF (required)")
    pfa.add_argument("--dir", metavar="DIR", default="datasheets",
                     help="datasheets dir (default: ./datasheets)")
    pfa.add_argument("--method", choices=("manual", "pdftotext", "llm"),
                     default="manual", help="how the numbers were extracted")
    pfa.add_argument("--set", metavar="KEY=VAL@pN", action="append",
                     help="record one fact, e.g. vref=0.6V@5 or "
                          "load_capacitance=12pF@3 (repeatable)")
    pfa.set_defaults(handler=_cmd_facts_add)

    pfv = facts_sub.add_parser(
        "verify", parents=[common],
        help="audit the store: schema, PDF sha256 staleness, page bounds, "
             "quotes (via optional pdftotext)")
    pfv.add_argument("mpn", nargs="?", help="verify one MPN (default: all)")
    pfv.add_argument("--dir", metavar="DIR", default="datasheets",
                     help="datasheets dir (default: ./datasheets)")
    pfv.add_argument("--exit-zero", action="store_true",
                     help="always exit 0 (report mode)")
    pfv.set_defaults(handler=_cmd_facts_verify)

    pfl = facts_sub.add_parser(
        "lookup", parents=[common],
        help="print one MPN's audited facts")
    pfl.add_argument("mpn", nargs="?", help="exact manufacturer part number")
    pfl.add_argument("key", nargs="?", help="one fact key (default: all)")
    pfl.add_argument("--dir", metavar="DIR", default="datasheets",
                     help="datasheets dir (default: ./datasheets)")
    pfl.set_defaults(handler=_cmd_facts_lookup)

    pr = rev.add_parser(
        "report", parents=[common],
        help="re-render a findings JSON file (text/json/sarif/junit/markdown)")
    pr.add_argument("path", nargs="?", help="findings .json (from analyze --out)")
    pr.add_argument("--format", choices=_FORMATS, default="text",
                    help="output format")
    pr.set_defaults(handler=_cmd_review_report)

    pe = rev.add_parser(
        "explain", parents=[common],
        help="print one review rule: what it checks, formula, provenance")
    pe.add_argument("code", nargs="?", help="rule code, e.g. REVIEW_XTAL_LOAD")
    pe.set_defaults(handler=_cmd_review_explain)

    pp = rev.add_parser(
        "propose", parents=[common],
        help="turn findings into declarative candidate changes (op-list / "
             "contract / sim drafts; never touches design files)")
    pp.add_argument("path", nargs="?", help="findings .json (from analyze --out)")
    pp.add_argument("--out", metavar="FILE", help="write proposals JSON here")
    pp.set_defaults(handler=_cmd_review_propose)

    pb = rev.add_parser(
        "testbench", parents=[common],
        help="auto-generate + run subcircuit SPICE testbenches from "
             "quantitative findings (RC corners, divider ratios)")
    pb.add_argument("path", nargs="?", help="schematic (.kicad_sch / .SchDoc)")
    pb.add_argument("--findings", metavar="FILE",
                    help="reuse a findings .json (default: run "
                         "`review analyze` in-process, standard profile)")
    pb.add_argument("--deck-only", dest="deck_only", action="store_true",
                    help="emit the decks without running ngspice (exit 0)")
    pb.add_argument("--out", metavar="DIR",
                    help="with --deck-only: write <fingerprint>_<kind>.deck "
                         "files here")
    pb.add_argument("--timeout", type=float, metavar="S",
                    help="per-bench engine timeout (default 60)")
    pb.add_argument("--exit-zero", action="store_true",
                    help="always exit 0 (report mode)")
    pb.set_defaults(handler=_cmd_review_testbench)

    pd = rev.add_parser(
        "diff", parents=[common],
        help="compare two findings files (fingerprint-aligned drift)")
    pd.add_argument("old", nargs="?", help="earlier findings .json")
    pd.add_argument("new", nargs="?", help="later findings .json")
    pd.add_argument("--fail-on-new", dest="fail_on_new", action="store_true",
                    help="exit 1 when the later run adds findings")
    pd.set_defaults(handler=_cmd_review_diff)

    pt = rev.add_parser(
        "tree", parents=[common],
        help="print the schematic's power tree (rails, regulators, consumers)")
    pt.add_argument("path", nargs="?", help="schematic (.kicad_sch / .SchDoc)")
    pt.set_defaults(handler=_cmd_review_tree)

    pv = rev.add_parser(
        "validate", parents=[common],
        help="gate LLM deep-review candidates: schema / anchors / datasheet "
             "evidence / masquerade — failures are quarantined with reasons")
    pv.add_argument("candidates", nargs="?", help="candidates .json")
    pv.add_argument("sch", nargs="?", help="schematic (.kicad_sch / .SchDoc)")
    pv.add_argument("--facts", metavar="DIR",
                    help="datasheet facts dir for evidence verification")
    pv.add_argument("--out", metavar="FILE",
                    help="write the accepted-findings envelope here")
    pv.set_defaults(handler=_cmd_review_validate)
