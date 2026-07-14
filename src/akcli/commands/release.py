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
8. ``git``            — clean worktree (unless ``--allow-dirty``)
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

from ..errors import EXIT
from .. import report as _report
from ._shared import (
    _dumps,
    _emit,
    _load_cfg,
    _load_schematic,
    _require_path,
)

_ACTIONABLE = {_report.Severity.WARNING, _report.Severity.ERROR,
               _report.Severity.CRITICAL}


def _gate(name: str, findings: list) -> dict:
    worst = max((_report._SEV_RANK.get(f.severity, 0) for f in findings),
                default=0)
    return {
        "gate": name,
        "status": "fail" if any(f.severity in _ACTIONABLE for f in findings)
                  else "pass",
        "findings": [
            {"code": f.code, "severity": f.severity.value, "message": f.message}
            for f in findings
        ],
        "worst_rank": worst,
    }


def _skipped(name: str, reason: str) -> dict:
    return {"gate": name, "status": "skipped", "reason": reason, "findings": []}


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
        "schema_version": "1",
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
    p.set_defaults(handler=_cmd_release_preflight, sch=None)

    pp = rel_sub.add_parser(
        "preflight", parents=[common],
        help="run all gates (check/intent/contract/library/sch-pcb/fab/"
             "order/git) and write a release manifest")
    pp.add_argument("--sch", metavar="FILE", help="schematic (.kicad_sch)")
    pp.add_argument("--pcb", metavar="FILE", help="board (.kicad_pcb)")
    pp.add_argument("--intent", metavar="FILE", help="design-intent JSON")
    pp.add_argument("--contract", metavar="FILE", help="design-contract TOML")
    pp.add_argument("--fab-profile", dest="fab_profile", metavar="FILE",
                    help="fab profile TOML")
    pp.add_argument("--order", metavar="FILE", help="order manifest TOML")
    pp.add_argument("--out", metavar="FILE",
                    help="write the release manifest JSON here")
    pp.add_argument("--allow-dirty", dest="allow_dirty", action="store_true",
                    help="do not fail on a dirty worktree; record it instead")
    pp.set_defaults(handler=_cmd_release_preflight)
