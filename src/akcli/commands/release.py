"""`akcli release preflight` — one gate run, one traceable manifest.

Runs every applicable gate over a schematic/board pair and writes a release
manifest binding: input file hashes, tool versions, git revision (and
cleanliness), the chosen fab profile/contract/order manifest, and each gate's
findings. PASS means every gate passed; a dirty worktree fails unless
``--allow-dirty`` (which records the fact instead of hiding it).

Gates (each skipped-with-reason when its input is absent, never silently):

1. ``check``          — ERC-lite/power/bom/nets over the schematic
2. ``intent``         — design-intent assertions (``--intent``)
3. ``contract``       — design contracts (``--contract``)
4. ``library-audit``  — workspace resolution (project dir of the schematic)
5. ``sch-pcb``        — schematic <-> board equivalence (``--pcb``)
6. ``fab``            — fab profile policy (``--pcb`` + ``--fab-profile``)
7. ``order``          — order manifest completeness (``--order``)
7b. ``sim``           — quantitative SPICE assertions (``--sim`` sim.json;
                        requested-but-unrunnable FAILS, never skips)
8. ``review``         — calibrated review allowlist (``--review-policy``)
8b. ``gerber``        — fab package completeness (``--gerbers``)
9. ``git``            — clean worktree (unless ``--allow-dirty``)
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

from .. import contract as _contract
from ..errors import EXIT
from .. import report as _report
from ._shared import (
    _dumps,
    _emit,
    _gate,
    _load_cfg,
    _load_schematic,
    _require_path,
    _skipped,
    _ExitWith,
)


def _sim_gate(sch, spec_path: Path, cfg=None) -> dict:
    """Run a ``sim.json`` assertion spec as a release gate.

    An explicitly requested gate that cannot run must FAIL, never skip — a
    PASS manifest may not ship just because the release machine lacked
    libngspice.

    Deck-level ERROR/CRITICAL findings (a part absent from a measured node —
    ``SIM_PART_SKIPPED``) join the gate: the ``akcli sim`` CLI already refuses
    such a run, and a release must not pass a sim whose measured values
    exclude a series element. Deck WARNINGs (routine unmodeled parts off the
    measured path) stay out of the gate — the any-WARNING-fails posture here
    would turn them into false reds. Config ``[[waiver]]`` entries apply, so
    a deliberate acceptance can demote/waive exactly like ``check`` findings.
    """
    from ..errors import AkcliError
    from ..sim import assertions as sim_assertions
    from ..sim import deck as sim_deck
    from ..sim import engine as sim_engine
    from ._shared import _ExitWith as _EW
    from .sim import _measure  # the CLI's own engine/assert loop

    def _fail(code: str, message: str) -> dict:
        return {"gate": "sim", "status": "fail", "findings": [
            {"code": code, "severity": "error", "message": message}]}

    try:
        spec = sim_assertions.load(spec_path)
    except AkcliError as exc:
        return _fail(exc.code, exc.message)
    if sim_engine.available() is None:
        return _fail("NGSPICE_MISSING",
                     "sim gate requested but libngspice was not found — "
                     "install KiCad or set AKCLI_NGSPICE")
    try:
        d = sim_deck.build(sch, spec)
    except AkcliError as exc:
        return _fail(exc.code, exc.message)
    try:
        findings, _measured, _ok = _measure(
            sim_engine, sim_assertions, spec, d.text, timeout=60.0)
    except _EW as exc:
        msg = exc.msg.removeprefix("ERROR: ")
        code, sep, rest = msg.partition(": ")
        return _fail(code if sep else "NGSPICE_FAILED", rest if sep else msg)
    blocking = {_report.Severity.ERROR, _report.Severity.CRITICAL}
    merged = [
        *[f for f in d.warnings if f.severity in blocking],
        *findings,
    ]
    if cfg is not None and getattr(cfg, "waivers", None):
        merged, _w, _d = _report.apply_waivers(merged, cfg.waivers)
    return _gate("sim", merged)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_state(cwd: Path) -> dict:
    def _run(*argv: str) -> str | None:
        try:
            proc = subprocess.run(["git", *argv], cwd=cwd, capture_output=True,
                                  text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    rev = _run("rev-parse", "HEAD")
    status = _run("status", "--porcelain")
    return {
        "available": rev is not None,
        "revision": rev,
        "dirty": bool(status) if status is not None else None,
    }


def _cmd_release_preflight(args: argparse.Namespace) -> int:
    from .. import __version__

    sch_path = _require_path(getattr(args, "sch", None), "--sch schematic")
    sch = _load_schematic(sch_path)
    cfg = _load_cfg(args, sch_path)
    gates: list[dict] = []
    inputs: dict[str, dict] = {
        "sch": {"path": str(sch_path), "sha256": _sha256(sch_path)}}

    # 1. schematic checks -----------------------------------------------------
    findings: list = []
    from .checks import _run_check
    ns = argparse.Namespace(path=str(sch_path), symbols=None)
    for name in ("erc", "power", "bom", "nets"):
        findings.extend(_run_check(name, sch, cfg, ns))
    findings, _waived, _demoted = _report.apply_waivers(findings, cfg.waivers)
    gates.append(_gate("check", findings))

    # 2. intent ---------------------------------------------------------------
    if getattr(args, "intent", None):
        from ..checks import intent as intent_mod
        spec = intent_mod.load(args.intent)
        gates.append(_gate("intent", intent_mod.run(sch, spec)))
        inputs["intent"] = {"path": args.intent,
                            "sha256": _sha256(Path(args.intent))}
    else:
        gates.append(_skipped("intent", "no --intent file given"))

    # 3. contract -------------------------------------------------------------
    if getattr(args, "contract", None):
        from ..checks import contract as contract_mod
        doc = contract_mod.load(args.contract)
        gates.append(_gate("contract", contract_mod.run(sch, doc)))
        inputs["contract"] = {"path": args.contract,
                              "sha256": _sha256(Path(args.contract))}
    else:
        gates.append(_skipped("contract", "no --contract file given"))

    # 4. library audit --------------------------------------------------------
    from .. import libtable
    ws = libtable.discover(sch_path.parent)
    gates.append(_gate("library-audit", libtable.audit(ws, [sch_path])))

    # 5+6. board gates ----------------------------------------------------------
    pcb_path = getattr(args, "pcb", None)
    profile = None
    if pcb_path:
        pcb_path = Path(pcb_path)
        inputs["pcb"] = {"path": str(pcb_path), "sha256": _sha256(pcb_path)}
        from ..checks import schpcb
        from ..readers import kicad as kreader
        pcb = kreader.read_pcb(str(pcb_path))
        gates.append(_gate("sch-pcb", schpcb.run(sch, pcb)))
        if getattr(args, "fab_profile", None):
            from .. import fab
            profile = fab.load_profile(args.fab_profile)
            gates.append(_gate("fab", fab.check(pcb, profile)))
            inputs["fab_profile"] = {"path": args.fab_profile,
                                     "sha256": _sha256(Path(args.fab_profile)),
                                     "id": profile.get("id")}
        else:
            gates.append(_skipped("fab", "no --fab-profile given"))
    else:
        gates.append(_skipped("sch-pcb", "no --pcb board given"))
        gates.append(_skipped("fab", "no --pcb board given"))

    # 7. order manifest ---------------------------------------------------------
    if getattr(args, "order", None):
        from .. import fab
        order = fab.load_order(args.order)
        gates.append(_gate("order", fab.check_order(order, profile)))
        inputs["order"] = {"path": args.order,
                           "sha256": _sha256(Path(args.order))}
    else:
        gates.append(_skipped("order", "no --order manifest given"))

    # 7b. sim (quantitative SPICE assertions) -------------------------------------
    if getattr(args, "sim", None):
        sim_path = Path(args.sim)
        if not sim_path.is_file():
            raise _ExitWith(EXIT["NOT_FOUND"],
                            f"ERROR: no such sim spec: {sim_path}")
        inputs["sim"] = {"path": str(sim_path), "sha256": _sha256(sim_path)}
        gates.append(_sim_gate(sch, sim_path, cfg))
    else:
        gates.append(_skipped(
            "sim", "no --sim spec given (simulation stays outside the gate)"))

    # 8. review policy (calibrated allowlist only) --------------------------------
    if getattr(args, "review_policy", None):
        import tomllib
        pol_path = Path(args.review_policy)
        try:
            with pol_path.open("rb") as fh:
                pol = tomllib.load(fh).get("review") or {}
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise _ExitWith(EXIT["USAGE"],
                            f"ERROR: cannot read review policy: {exc}")
        allow = pol.get("allow") or []
        if not isinstance(allow, list) or not all(
                isinstance(a, str) for a in allow) or not allow:
            raise _ExitWith(EXIT["USAGE"],
                            "ERROR: review policy needs a non-empty "
                            "[review] allow = [\"CODE\", ...] list — only "
                            "explicitly calibrated rules may block")
        from ..review import engine as review_engine
        from ..review import facts as review_facts
        facts_dir = sch_path.parent / "datasheets"
        store = (review_facts.load_store(facts_dir)
                 if (facts_dir / "extracted").is_dir() else None)
        rv_findings, _rv_meta = review_engine.analyze(
            sch, pcb=(pcb if pcb_path else None),
            profile=str(pol.get("profile") or "standard"), facts=store)
        rv_findings, _w, _d = _report.apply_waivers(rv_findings, cfg.waivers)
        gated = [f for f in rv_findings if f.code in set(allow)]
        gates.append(_gate("review", gated))
        inputs["review_policy"] = {"path": str(pol_path),
                                   "sha256": _sha256(pol_path),
                                   "allow": sorted(allow)}
    else:
        gates.append(_skipped(
            "review", "no --review-policy given (review stays advisory)"))

    # 8b. gerber package (fab-output completeness/alignment/staleness) ------------
    if getattr(args, "gerbers", None):
        from ..readers import gerber as gerber_reader
        from ..review import topo as review_topo
        from ..review.detectors import gerber as gerber_det
        gdir = Path(args.gerbers)
        if not gdir.is_dir():
            raise _ExitWith(EXIT["USAGE"],
                            f"ERROR: no such gerber dir: {gdir}")
        gset = gerber_reader.read_gerber_dir(gdir)
        gctx = review_topo.build_ctx(
            sch, (pcb if pcb_path else None), None, gset)
        gates.append(_gate("gerber", gerber_det.run(gctx)))
        inputs["gerbers"] = {"path": str(gdir),
                             "files": sorted(f.name for f in gset.files)}
    else:
        gates.append(_skipped("gerber", "no --gerbers dir given"))

    # 8. git --------------------------------------------------------------------
    git = _git_state(sch_path.parent)
    allow_dirty = bool(getattr(args, "allow_dirty", False))
    git_gate: dict
    if not git["available"]:
        git_gate = _skipped("git", "not a git repository (provenance untracked)")
    elif git["dirty"] and not allow_dirty:
        git_gate = {"gate": "git", "status": "fail", "findings": [{
            "code": "RELEASE_DIRTY_WORKTREE", "severity": "error",
            "message": "worktree has uncommitted changes — commit first, or "
                       "pass --allow-dirty to record the fact",
        }]}
    else:
        git_gate = {"gate": "git", "status": "pass", "findings": [],
                    "allow_dirty": allow_dirty and bool(git["dirty"])}
    gates.append(git_gate)

    passed = all(g["status"] in ("pass", "skipped") for g in gates)
    manifest = {
        "schema_version": _contract.SCHEMA_VERSIONS["envelope"],
        "tool": {"akcli": __version__},
        "result": "PASS" if passed else "FAIL",
        "inputs": inputs,
        "git": git,
        "gates": gates,
    }

    out_path = getattr(args, "out", None)
    if out_path:
        from .. import safety
        safety.atomic_write_with_backup(out_path, _dumps(manifest) + "\n")

    if args.json:
        _emit(_dumps(manifest))
    else:
        _emit(f"release preflight: {'PASS' if passed else 'FAIL'}")
        for g in gates:
            status = g["status"].upper()
            extra = f" ({g.get('reason')})" if g["status"] == "skipped" else ""
            fail_count = sum(1 for f in g["findings"]
                             if f["severity"] in ("warning", "error", "critical"))
            if fail_count and g["status"] == "fail":
                extra = f" ({fail_count} finding(s))"
            _emit(f"  {status:<8} {g['gate']}{extra}")
            if g["status"] == "fail":
                for f in g["findings"]:
                    if f["severity"] in ("warning", "error", "critical"):
                        _emit(f"           {f['severity'].upper()} "
                              f"{f['code']}: {f['message']}")
        if out_path:
            _emit(f"manifest: {out_path}")
        if git["dirty"] and allow_dirty:
            sys.stderr.write("WARNING: released from a DIRTY worktree "
                             "(--allow-dirty) — recorded in the manifest\n")
    return EXIT["OK"] if passed else EXIT["FINDINGS"]


def register(sub, common) -> None:
    p = sub.add_parser("release", parents=[common],
                       help="release gating: run every gate, emit a "
                            "traceable manifest")
    rel_sub = p.add_subparsers(dest="release_command", metavar="<subcommand>")
    p.set_defaults(handler=_cmd_release_preflight, sch=None,
                   review_policy=None)

    pp = rel_sub.add_parser(
        "preflight", parents=[common],
        help="run all gates (check/intent/contract/library/sch-pcb/fab/"
             "order/sim/review/gerber/git) and write a release manifest")
    pp.add_argument("--sch", metavar="FILE", help="schematic (.kicad_sch)")
    pp.add_argument("--pcb", metavar="FILE", help="board (.kicad_pcb)")
    pp.add_argument("--intent", metavar="FILE", help="design-intent JSON")
    pp.add_argument("--contract", metavar="FILE", help="design-contract TOML")
    pp.add_argument("--fab-profile", dest="fab_profile", metavar="FILE",
                    help="fab profile TOML")
    pp.add_argument("--order", metavar="FILE", help="order manifest TOML")
    pp.add_argument("--sim", metavar="FILE",
                    help="sim.json assertion spec — runs the quantitative "
                         "simulation gate on the bundled ngspice (a requested "
                         "gate that cannot run FAILS, never skips)")
    pp.add_argument("--gerbers", metavar="DIR",
                    help="fab output dir — gates on gerber package "
                         "completeness/alignment/staleness")
    pp.add_argument("--review-policy", dest="review_policy", metavar="FILE",
                    help="review gate policy TOML: [review] allow = [codes] "
                         "(+ optional profile/fail threshold) — only "
                         "explicitly allowlisted, calibrated rules may block")
    pp.add_argument("-o", "--out", metavar="FILE",
                    help="write the release manifest JSON here")
    pp.add_argument("--allow-dirty", dest="allow_dirty", action="store_true",
                    help="do not fail on a dirty worktree; record it instead")
    pp.set_defaults(handler=_cmd_release_preflight)
