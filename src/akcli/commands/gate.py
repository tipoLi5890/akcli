"""`akcli gate` — iteration-weight four-check bundle for the edit loop.

During a multi-day agent-operated board campaign, four commands get re-run by
hand after every GUI edit round: intent assertions, sch<->pcb verify,
ERC-lite checks, BOM lock-drift. ``release preflight`` is the wrong vehicle
for that loop: its dirty-worktree git gate is inverted for mid-edit use, its
manifest/provenance machinery (input hashing, git state, fab/order/review/
gerber gates) is release-weight, and its BOM lock-drift check has no gate at
all today. ``gate`` runs exactly four gates, reusing the same pure functions
``check``/``release preflight``/``jlc bom --against-lock`` already use — no
new detector logic:

1. ``check``    — the SAME default check set ``akcli check PATH`` runs
                  (erc/power/bom/nets [+layout/integrity for .kicad_sch]
                  [+pairs]) — NOT ``release preflight``'s narrower fixed
                  erc/power/bom/nets four-tuple.
2. ``intent``   — ``checks/intent.py``, skipped-with-reason with no intent
                  file (``--intent`` or config ``[paths].intent`` — the same
                  key ``check --intent`` falls back to).
3. ``sch-pcb``  — ``checks/schpcb.py`` against ``--pcb``/config
                  ``[paths].pcb``; skipped-with-reason when absent.
4. ``bom-lock`` — the lock-drift diff (``parts/bom_jlc.check`` +
                  ``diff_lock``), the same library calls
                  ``jlc bom --against-lock`` makes. DEFAULT OFFLINE
                  (HTTP-cache-only): an every-edit loop must never hit the
                  network on its own; ``--live`` opts in. Skipped-with-reason
                  when no lockfile (``--lock``/config ``[paths].bom_lock``).

Not to be confused with the CI-green *gate* (``.github/scripts/
ci_green_gate.sh``), which checks GitHub Actions status on a tagged release
commit — this is a local, offline, per-edit check bundle, unrelated to that
release-CI workflow gate.

``PATH`` is optional: it falls back to config ``[paths].schematic`` (the
first command to actually consume that documented-but-unconsumed key). No
path anywhere is a structured USAGE error naming the config key.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import report as _report
from ..errors import EXIT
from ._shared import (
    _add_exit_policy_flags,
    _dumps,
    _emit,
    _gate,
    _gate_exit,
    _load_cfg,
    _load_schematic,
    _skipped,
    _stamp,
    _ExitWith,
)


def _bomlock_gate(sch, sch_path: Path, lock_path: Path,
                  args: argparse.Namespace, *, live: bool) -> dict:
    """BOM lockfile drift — the same ``diff_lock()`` library call
    ``jlc bom --against-lock`` makes.

    DEFAULT OFFLINE (HTTP-cache-only, mirroring ``jlc bom --offline``): an
    every-edit iteration loop must never hit the network on its own;
    ``--live`` opts a single run into a fresh catalog fetch. A cache miss
    degrades that line to ``status="unverified"`` rather than raising (see
    ``bom_jlc.check``'s own docstring) — this gate surfaces that degradation
    honestly (an ``unverified`` count + a note) instead of letting a cold
    cache read as a fully-verified PASS.
    """
    import json as _json

    from ..parts import bom_jlc
    from ..parts import search as parts_search
    from .jlc import _bom_cfg

    try:
        lock_doc = _json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: cannot read BOM lockfile: {exc}")

    prefixes, min_stock, _fee = _bom_cfg(args, sch_path)
    cache = parts_search.default_cache_dir()
    try:
        lines = bom_jlc.check(sch, min_stock=min_stock, qty=1, cache_dir=cache,
                              no_part_prefixes=prefixes, offline=not live)
    except parts_search.JlcNetworkError as exc:
        return {
            "gate": "bom-lock", "status": "fail", "offline": not live,
            "worst_rank": _report._SEV_RANK[_report.Severity.ERROR],
            "findings": [{"code": "NETWORK", "severity": "error",
                         "message": f"live BOM catalog check failed: "
                                    f"{exc.message}"}],
        }

    drift = bom_jlc.diff_lock(lines, lock_doc)
    unverified = bom_jlc.totals(lines).get("unverified", 0)
    findings = [
        {"code": "BOM_LOCK_DRIFT", "severity": "warning",
         "message": f"{d['kind']} [{d['refs']}]: {d['detail']}"}
        for d in drift
    ]
    out: dict = {
        "gate": "bom-lock",
        "status": "fail" if findings else "pass",
        "findings": findings,
        "worst_rank": (_report._SEV_RANK[_report.Severity.WARNING]
                       if findings else 0),
        "offline": not live,
    }
    if unverified:
        # A cold/no cache degrades every line to "unverified" (see
        # bom_jlc.check); that must never read as a clean, verified PASS.
        out["unverified"] = unverified
        out["note"] = (f"{unverified} BOM line(s) unverified (offline cache "
                       "miss) — this pass is provisional; rerun with --live "
                       "for a network-verified answer")
    return out


def _cmd_gate(args: argparse.Namespace) -> int:
    raw_path = getattr(args, "path", None)
    cfg = _load_cfg(args, Path(raw_path) if raw_path else None)

    sch_path_str = raw_path or cfg.paths.get("schematic")
    if not sch_path_str:
        raise _ExitWith(
            EXIT["USAGE"],
            "ERROR: missing schematic — pass PATH or set [paths].schematic "
            "in akcli.toml")
    sch_path = Path(sch_path_str)
    sch = _load_schematic(sch_path)

    gates: list[dict] = []

    # 1. check — the full default set (see module docstring); shares its
    #    selection logic with bare `akcli check PATH` via _default_check_names.
    from .checks import _default_check_names, _run_check
    ns = argparse.Namespace(path=str(sch_path), symbols=None)
    findings: list = []
    for name in _default_check_names(sch_path, cfg):
        findings.extend(_run_check(name, sch, cfg, ns))
    findings, _waived, _demoted = _report.apply_waivers(findings, cfg.waivers)
    gates.append(_gate("check", findings))

    # 2. intent -----------------------------------------------------------
    intent_path = getattr(args, "intent", None) or cfg.paths.get("intent")
    if intent_path:
        from ..checks import intent as intent_mod
        spec = intent_mod.load(intent_path)
        gates.append(_gate("intent", intent_mod.run(sch, spec)))
    else:
        gates.append(_skipped(
            "intent",
            "no --intent file given and no [paths].intent configured"))

    # 3. sch-pcb ------------------------------------------------------------
    pcb_path = getattr(args, "pcb", None) or cfg.paths.get("pcb")
    if pcb_path:
        from ..checks import schpcb
        from ..readers import kicad as kreader
        pcb = kreader.read_pcb(str(pcb_path))
        sp_findings, _w, _d = _report.apply_waivers(
            schpcb.run(sch, pcb), cfg.waivers)
        gates.append(_gate("sch-pcb", sp_findings))
    else:
        gates.append(_skipped(
            "sch-pcb", "no --pcb board given and no [paths].pcb configured"))

    # 4. bom-lock -------------------------------------------------------------
    lock_path = getattr(args, "lock", None) or cfg.paths.get("bom_lock")
    if lock_path:
        gates.append(_bomlock_gate(
            sch, sch_path, Path(lock_path), args,
            live=bool(getattr(args, "live", False))))
    else:
        gates.append(_skipped(
            "bom-lock",
            "no --lock file given and no [paths].bom_lock configured"))

    passed = all(g["status"] in ("pass", "skipped") for g in gates)
    result = "PASS" if passed else "FAIL"

    if args.json:
        _emit(_dumps(_stamp({"result": result, "gates": gates})))
    else:
        _emit(f"gate: {result}")
        for g in gates:
            status = g["status"].upper()
            bits: list[str] = []
            if g["status"] == "skipped":
                bits.append(g.get("reason", ""))
            else:
                fail_count = sum(
                    1 for f in g["findings"]
                    if f["severity"] in ("warning", "error", "critical"))
                if fail_count:
                    bits.append(f"{fail_count} finding(s)")
                if g.get("unverified"):
                    bits.append(f"{g['unverified']} unverified "
                               "(offline cache miss)")
            extra = f" ({'; '.join(b for b in bits if b)})" if bits else ""
            _emit(f"  {status:<8} {g['gate']}{extra}")
            if g["status"] == "fail":
                for f in g["findings"]:
                    if f["severity"] in ("warning", "error", "critical"):
                        _emit(f"           {f['severity'].upper()} "
                              f"{f['code']}: {f['message']}")
            if g.get("note"):
                _emit(f"           note: {g['note']}")

    return _gate_exit(gates, args)


def register(sub, common) -> None:
    p = sub.add_parser(
        "gate", parents=[common],
        help="iteration-weight four-check bundle (check + intent + sch-pcb "
             "+ bom-lock), offline by default — for the every-edit GUI "
             "loop; see `release preflight` for the release-weight, "
             "manifest-producing gate run")
    p.add_argument("path", nargs="?",
                   help="schematic (.kicad_sch/.SchDoc); falls back to "
                        "[paths].schematic in akcli.toml")
    p.add_argument("--pcb", metavar="FILE",
                   help="board (.kicad_pcb) for the sch-pcb gate; falls "
                        "back to [paths].pcb")
    p.add_argument("--intent", metavar="FILE",
                   help="design-intent JSON for the intent gate; falls back "
                        "to [paths].intent (the same key `check --intent` "
                        "falls back to)")
    p.add_argument("--lock", metavar="FILE",
                   help="BOM lockfile (`jlc bom --lock`) for the bom-lock "
                        "gate; falls back to [paths].bom_lock")
    p.add_argument("--live", action="store_true",
                   help="let the bom-lock gate hit the network for a fresh "
                        "catalog fetch (default: HTTP-cache-only, so a "
                        "gate run never touches the network on its own)")
    _add_exit_policy_flags(p)
    p.set_defaults(handler=_cmd_gate)
