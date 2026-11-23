"""`akcli release preflight` — one gate run, one traceable manifest.

Runs every applicable gate over a schematic/board pair and writes a release
manifest binding: input file hashes (per gerber file too), tool versions
(akcli, and kicad-cli when one is installed), git revision (and cleanliness —
with a hash of the dirty state when it is dirty), the chosen fab
profile/contract/order manifest, and each gate's findings. PASS means every
gate passed; a dirty worktree fails unless ``--allow-dirty`` (which records
the fact instead of hiding it).

Gates (each skipped-with-reason when its input is absent, never silently):

1. ``check``          — ERC-lite/power/bom/nets over the schematic
2. ``intent``         — design-intent assertions (``--intent``)
3. ``contract``       — design contracts (``--contract``)
4. ``library-audit``  — workspace resolution (project dir of the schematic)
4c. ``library-baseline`` — library files vs. the committed baseline
                        (``--library-baseline``, else config
                        ``[paths].library_baseline``, else
                        ``akcli-library.baseline.json`` beside the schematic)
5. ``sch-pcb``        — schematic <-> board equivalence (``--pcb``)
5b. ``pcb-library``   — board footprints vs. the project footprint libraries
                        (``--pcb``; its ``summary`` states how many footprints
                        were NOT compared; skipped when none could be compared)
6. ``fab``            — fab profile policy (``--pcb`` + ``--fab-profile``,
                        a profile file or a bundled profile id)
6b. ``drc``           — KiCad's own DRC via ``kicad-cli pcb drc`` (``--drc``
                        + ``--pcb``; requested-but-unrunnable FAILS, never
                        skips; zone fills are checked as saved)
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
import re
import subprocess
import sys
from collections.abc import Callable
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


# --------------------------------------------------------------------------- #
# KiCad DRC gate (opt-in via --drc)
# --------------------------------------------------------------------------- #
# KiCad's report severity -> ours. Only the two blocking levels are ever
# requested (--severity-error --severity-warning); the rest are mapped so a
# future kicad-cli default cannot crash the gate. A severity KiCad invents
# later stays BLOCKING (WARNING): unknown must never read as harmless.
_DRC_SEVERITY = {
    "error": _report.Severity.ERROR,
    "warning": _report.Severity.WARNING,
    "info": _report.Severity.NOTE,
    "action": _report.Severity.NOTE,
    "exclusion": _report.Severity.NOTE,
    "ignore": _report.Severity.NOTE,
    "debug": _report.Severity.NOTE,
}
# The report's two lists -> the finding code each becomes.
_DRC_SECTIONS = (
    ("violations", "KICAD_DRC_VIOLATION"),
    ("unconnected_items", "KICAD_DRC_UNCONNECTED"),
)
_DRC_ZONE_FILLS = "zone fills evaluated as saved (not refilled)"
# KiCad prints an item's net as ``[NAME]`` in every language it ships.
_NET_IN_BRACKETS = re.compile(r"\[([^\[\]]+)\]")


def _board_items(pcb_path: Path) -> tuple[dict[str, dict], set[str]]:
    """``uuid -> {ref?, pad?, net?}`` for a board's identified items, + its nets.

    This is the join key that makes DRC findings waivable. KiCad's report
    names an item by ``uuid`` and a ``description`` that is LOCALIZED (``Pad 1
    [+3V3] of R1 on F.Cu`` / ``1 上 [+3V3] 的焊盤 R1 F.Cu``), so designators are
    never parsed out of the text: the uuid is looked up in the board file
    instead. Footprints map to their Reference, pads to Reference + pad number
    + net, every other identified child of a footprint (texts, silk, courtyard)
    to its Reference, and tracks / arcs / vias / zones to their net.

    ``model.Pcb`` does not carry uuids, hence this second, narrow read.
    """
    from .. import safety
    from ..sexpr import parse
    from ..sexpr.query import atom_value

    root = parse(safety.read_text_guarded(pcb_path))
    net_by_idx: dict[str, str] = {}
    for net in root.find_all("net"):
        idx, name = atom_value(net, 1), atom_value(net, 2)
        if idx is not None and name:
            net_by_idx[idx] = name
    nets: set[str] = set(net_by_idx.values())

    def net_of(node) -> str | None:
        # Both dialects: ``(net 2 "+3V3")`` / ``(net 2)`` (index into the
        # board's net table, v6-v9) and ``(net "GND")`` (name only, KiCad 10).
        name = None
        ref = node.find("net")
        if ref is not None and len(ref.children or ()) >= 2:
            arg = ref.children[1]
            if len(ref.children) >= 3 and ref.children[2].is_atom:
                name = ref.children[2].value
            elif arg.is_atom and (arg.text or "").startswith('"'):
                name = arg.value
            elif arg.is_atom:
                name = net_by_idx.get(arg.value or "")
        name = name or atom_value(node.find("net_name"), 1)   # v6-v9 zones
        if name:
            nets.add(name)
        return name or None

    def uid(node) -> str | None:
        # ``tstamp`` is the pre-KiCad-8 spelling of the same identity.
        return atom_value(node.find("uuid"), 1) or atom_value(node.find("tstamp"), 1)

    items: dict[str, dict] = {}
    for fp in root.find_all("footprint"):
        ref = next((atom_value(prop, 2) for prop in fp.find_all("property")
                    if atom_value(prop, 1) == "Reference"), None)
        if ref is None:                                  # legacy fp_text form
            ref = next((atom_value(t, 2) for t in fp.find_all("fp_text")
                        if atom_value(t, 1) == "reference"), None)
        owner = {"ref": ref} if ref else {}
        for pad in fp.find_all("pad"):
            key = uid(pad)
            if key:
                items[key] = {**owner, "pad": atom_value(pad, 1) or "",
                              "net": net_of(pad)}
        if not owner:
            continue
        stack = [fp]
        while stack:                                     # every identified child
            node = stack.pop()
            key = uid(node)
            if key:
                items.setdefault(key, dict(owner))
            stack.extend(c for c in node.children or () if c.is_list)
    for tag in ("segment", "arc", "via", "zone"):
        for node in root.find_all(tag):
            key, name = uid(node), net_of(node)
            if key and name:
                items[key] = {"net": name}
    return items, nets


def _drc_finding(code: str, entry: dict, units: str,
                 items: dict[str, dict], nets: set[str]):
    """One entry of KiCad's DRC report -> one :class:`report.Finding`.

    ``refs`` — what a config ``[[waiver]]`` matches against — is KiCad's rule
    ``type`` first (so one waiver can cover a whole rule: ``refs =
    "lib_footprint_mismatch"``), then every involved designator, ``REF.PAD``
    pin and net the board file resolves the report's item uuids to. The
    message keeps KiCad's own (localized) wording verbatim, behind the stable
    rule type.
    """
    rule = str(entry.get("type") or "unknown")
    severity = _DRC_SEVERITY.get(str(entry.get("severity") or "").lower(),
                                 _report.Severity.WARNING)
    refs: list[str] = [rule]
    anchors: list[dict] = []
    parts: list[str] = []
    pos: tuple[float, float] | None = None
    for item in entry.get("items") or []:
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description") or "").strip()
        at = item.get("pos")
        try:
            xy = (float(at["x"]), float(at["y"]))
        except (KeyError, TypeError, ValueError):
            xy = None
        known = items.get(str(item.get("uuid") or ""), {})
        ref, pad, net = known.get("ref"), known.get("pad"), known.get("net")
        if net is None:
            # A uuid KiCad minted at load (the file gives the item none)
            # resolves to nothing; its net is still printed as ``[NAME]``.
            # Accepted only when NAME really is one of this board's nets.
            net = next((m for m in _NET_IN_BRACKETS.findall(desc)
                        if m in nets), None)
        pin = f"{ref}.{pad}" if ref and pad else None
        for name in (ref, pin, net):
            if name and name not in refs:
                refs.append(name)
        # Board-frame positions are mm; a report in other units keeps its
        # numbers in the message but anchors nothing.
        here = xy if units == "mm" else None
        if pos is None:
            pos = here
        if pin:
            anchors.append(_report.anchor("pin", pin, here))
        elif ref:
            anchors.append(_report.anchor("component", ref, here))
        elif net:
            anchors.append(_report.anchor("net", net, here))
        parts.append(desc + (f" @({xy[0]:g},{xy[1]:g}){units}" if xy else ""))
    message = f"{rule}: {str(entry.get('description') or '').strip()}"
    if parts:
        message += " — " + "; ".join(parts)
    return _report.Finding(code, severity, message, refs=refs, pos=pos,
                           anchors=anchors)


def _drc_findings(report: dict, items: dict[str, dict],
                  nets: set[str]) -> list:
    """KiCad's DRC report (``drc.v1.json``) -> findings, in report order."""
    units = str(report.get("coordinate_units") or "mm")
    return [
        _drc_finding(code, entry, units, items, nets)
        for section, code in _DRC_SECTIONS
        for entry in report.get(section) or []
        if isinstance(entry, dict)
    ]


def _drc_gate(pcb_path: Path, cfg=None) -> dict:
    """Run KiCad's own DRC (``kicad-cli pcb drc``) as a release gate.

    Opt-in like the sim gate, and strict like it: an explicitly requested
    gate that cannot run must FAIL, never skip — a PASS manifest may not ship
    just because the release machine had no KiCad (``KICAD_CLI_MISSING``) or
    KiCad could not produce a report (``KICAD_DRC_FAILED``: board would not
    load, KiCad older than 8, unreadable JSON; a timeout keeps its own
    ``KICAD_CLI_TIMEOUT``).

    Each violation becomes a ``KICAD_DRC_VIOLATION`` and each unrouted
    connection a ``KICAD_DRC_UNCONNECTED``, at KiCad's own severity, so the
    project's rule severities and DRC exclusions (``.kicad_pro``) and custom
    rules (``.kicad_dru``) decide what blocks — they travel with the board
    into the driver's staging copy. Config ``[[waiver]]`` entries apply on
    top, exactly as for the ``check`` and ``sim`` gates.

    Zone fills are evaluated AS SAVED: the driver never passes
    ``--refill-zones`` (nor ``--save-board``), because the gate must judge
    the bytes being released, not a board KiCad silently recomputed. The
    gate's ``summary`` says so every time.
    """
    from ..drivers import kicad_cli

    def _fail(code: str, message: str) -> dict:
        return {"gate": "drc", "status": "fail", "findings": [
            {"code": code, "severity": "error", "message": message}]}

    out = kicad_cli.drc(str(pcb_path))
    if out is None:
        return _fail("KICAD_CLI_MISSING",
                     "drc gate requested but kicad-cli was not found — "
                     "install KiCad or set KICAD_CLI")
    report = out.get("report")
    if not (isinstance(report, dict)
            and isinstance(report.get("violations"), list)
            and isinstance(report.get("unconnected_items"), list)):
        return _fail(out.get("error_code") or "KICAD_DRC_FAILED",
                     "drc gate requested but KiCad DRC produced no usable "
                     "report — " + (out.get("error") or "its JSON lacks the "
                                    "violations/unconnected_items lists"))
    items, nets = _board_items(pcb_path)
    findings = _drc_findings(report, items, nets)
    counts = [sum(1 for f in findings if f.code == code)
              for _section, code in _DRC_SECTIONS]
    if cfg is not None and getattr(cfg, "waivers", None):
        findings, _w, _d = _report.apply_waivers(findings, cfg.waivers)
    gate = _gate("drc", findings)
    gate["summary"] = (
        f"KiCad {report.get('kicad_version') or '?'} DRC: {counts[0]} "
        f"violation(s), {counts[1]} unconnected item(s); {_DRC_ZONE_FILLS}")
    return gate


def _pcb_library_gate(pcb, pcb_path: Path, sch_path: Path, ws, cfg) -> dict:
    """Board footprints vs. the project footprint libraries (``checks/fplib``).

    The same leg as ``verify <sch> <board> --library``, with the same
    ``[[waiver]]`` handling as ``sch-pcb``. Only footprints whose library the
    PROJECT ``fp-lib-table`` registers can be compared, so the gate is honest
    about it twice: ``summary`` always states the compared / NOT-compared
    counts, and a run that compared NO footprint (and found nothing that
    blocks) is ``skipped`` with those counts instead of passing on zero
    evidence.
    """
    from .. import libtable
    from ..checks import fplib

    if pcb_path.parent.resolve() != sch_path.parent.resolve():
        ws = libtable.discover(pcb_path.parent)   # ${KIPRJMOD} is the board's dir
    findings, coverage = fplib.run(pcb, ws)
    findings, _w, _d = _report.apply_waivers(findings, cfg.waivers)
    gate = _gate("pcb-library", findings)
    if coverage["compared"] == 0 and gate["status"] == "pass":
        return _skipped(
            "pcb-library",
            "no board footprint could be compared with a project "
            "fp-lib-table library — " + fplib.coverage_text(coverage))
    gate["summary"] = fplib.coverage_text(coverage)
    gate["coverage"] = coverage
    return gate


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dirty_sha256(run: Callable[..., bytes | None]) -> str | None:
    """Evidence hash of a dirty worktree, or ``None`` if git would not say.

    sha256 over: the bytes of ``git diff HEAD --binary`` (staged + unstaged
    changes to tracked files), then — for every untracked, non-ignored path,
    in byte order — one NUL followed by the path. Untracked files contribute
    their PATH only, not their content.

    This is evidence that two manifests describe the SAME dirty state on one
    machine, not a cross-machine identity: the diff text follows the local
    git configuration (``core.autocrlf`` changes the bytes on Windows, as do
    ``core.quotePath`` and the diff algorithm).
    """
    # --no-ext-diff/--no-textconv/--no-color: whatever the user's config, the
    # bytes hashed are a plain patch, never an external tool's rendering.
    diff = run("diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv",
               "--no-color", timeout=120)
    # `ls-files --others` is scoped to the CWD (the schematic's directory)
    # unless told otherwise; `diff HEAD` and `status` are repo-wide. `:/` +
    # --full-name put all three on the same scope: the whole worktree.
    untracked = run("ls-files", "--others", "--exclude-standard", "-z",
                    "--full-name", "--", ":/", timeout=120)
    if diff is None or untracked is None:
        return None
    digest = hashlib.sha256(diff)
    for path in sorted(p for p in untracked.split(b"\0") if p):
        digest.update(b"\0" + path)
    return digest.hexdigest()


def _git_state(cwd: Path) -> dict:
    # BYTES, never text mode: text mode decodes with the locale codec (cp1252
    # on Windows — a non-ASCII file name in `status` would raise) and
    # translates newlines, which would change the dirty hash.
    def _run(*argv: str, timeout: float = 10) -> bytes | None:
        try:
            proc = subprocess.run(["git", *argv], cwd=cwd, capture_output=True,
                                  timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return proc.stdout if proc.returncode == 0 else None

    head = _run("rev-parse", "HEAD")
    rev = head.decode("ascii", "replace").strip() if head is not None else None
    status = _run("status", "--porcelain")
    state: dict = {
        "available": rev is not None,
        "revision": rev,
        "dirty": bool(status.strip()) if status is not None else None,
    }
    if state["available"] and state["dirty"]:
        state["dirty_sha256"] = _dirty_sha256(_run)
    return state


def _cmd_release_preflight(args: argparse.Namespace) -> int:
    from .. import __version__

    sch_path = _require_path(getattr(args, "sch", None), "--sch schematic")
    want_drc = bool(getattr(args, "drc", False))
    if want_drc and not getattr(args, "pcb", None):
        # Before any gate runs: a DRC with no board is a malformed request,
        # not a finding — and never something to skip quietly.
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: --drc needs a board — pass --pcb FILE "
                        "(KiCad DRC runs on the .kicad_pcb)")
    sch = _load_schematic(sch_path)
    cfg = _load_cfg(args, sch_path)
    gates: list[dict] = []
    inputs: dict[str, dict] = {
        "sch": {"path": str(sch_path), "sha256": _sha256(sch_path)}}

    # 1. schematic checks -----------------------------------------------------
    findings: list = []
    from .checks import _run_check
    ns = argparse.Namespace(path=str(sch_path), symbols=None)
    names = ["erc", "power", "bom", "nets"]
    if sch_path.suffix.lower() == ".kicad_sch":
        # the write gate's document checks, over the whole sheet tree
        names.append("integrity")
    for name in names:
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
    lib_findings, _w, _d = _report.apply_waivers(
        libtable.audit(ws, [sch_path]), cfg.waivers)
    gates.append(_gate("library-audit", lib_findings))

    # 4b. project integrity ---------------------------------------------------
    # A release candidate is a PROJECT, not a loose schematic: if the shipped
    # tree has a .kicad_pro, its root binding, sheet tree and board container
    # are part of what is being released. `with_lib_audit=False` because
    # library-audit above is the same check — one finding reported twice reads
    # as two problems. No project file: an explicit skip, never a silent pass.
    from ..readers import kicad_pro as _kpro
    pro_path = _kpro.find(sch_path.parent)
    if pro_path is not None:
        from .project import check_findings
        gates.append(_gate("project",
                           check_findings(pro_path, with_lib_audit=False)))
        inputs["project"] = {"path": str(pro_path),
                             "sha256": _sha256(pro_path)}
    else:
        gates.append(_skipped("project",
                              "no .kicad_pro beside the --sch schematic"))

    # 4c. library baseline ----------------------------------------------------
    # The committed, hashed record of the project's libraries. A NAMED
    # baseline (flag or config key) that is missing/unparseable is an error
    # like a missing --intent file; only the default-named file is optional.
    from .. import library_baseline
    baseline_arg = (getattr(args, "library_baseline", None)
                    or cfg.paths.get("library_baseline"))
    baseline_path = Path(baseline_arg) if baseline_arg else None
    if baseline_path is None:
        default = ws.project_dir / library_baseline.DEFAULT_FILENAME
        if default.is_file():
            baseline_path = default
    if baseline_path is not None:
        base_doc = library_baseline.load(baseline_path)
        bl_findings, _w, _d = _report.apply_waivers(
            library_baseline.compare(base_doc, library_baseline.build(ws)),
            cfg.waivers)
        gates.append(_gate("library-baseline", bl_findings))
        inputs["library_baseline"] = {"path": str(baseline_path),
                                      "sha256": _sha256(baseline_path)}
    else:
        gates.append(_skipped(
            "library-baseline",
            "no --library-baseline given, no [paths].library_baseline "
            f"configured, and no {library_baseline.DEFAULT_FILENAME} beside "
            "the schematic (write one: akcli library audit --write-baseline)"))

    # 5+6. board gates ----------------------------------------------------------
    pcb_path = getattr(args, "pcb", None)
    profile = None
    if pcb_path:
        pcb_path = Path(pcb_path)
        inputs["pcb"] = {"path": str(pcb_path), "sha256": _sha256(pcb_path)}
        from ..checks import schpcb
        from ..readers import kicad as kreader
        pcb = kreader.read_pcb(str(pcb_path))
        sp_findings, _w, _d = _report.apply_waivers(
            schpcb.run(sch, pcb), cfg.waivers)
        gates.append(_gate("sch-pcb", sp_findings))
        # 5b. board footprints vs. the project libraries. Its OWN gate, not
        # part of sch-pcb: sch-pcb answers "does the board implement the
        # schematic?", this one "does the board still match its libraries?" —
        # and its coverage line (how many footprints were NOT compared)
        # belongs to it alone.
        gates.append(_pcb_library_gate(pcb, pcb_path, sch_path, ws, cfg))
        if getattr(args, "fab_profile", None):
            from .. import fab
            # A file, or the id of a profile bundled with akcli (file wins).
            profile, origin = fab.resolve_profile(args.fab_profile)
            # The order's via_covering decides which vias the tenting cap
            # binds; the project file (hashed into `inputs` by gate 4b) is
            # the board's own DRC floor the profile's minimums are held to.
            fab_order = (fab.load_order(args.order)
                         if getattr(args, "order", None) else None)
            gates.append(_gate("fab", fab.check(
                pcb, profile, fab_order, origin=origin,
                rules=fab.read_project_rules(pro_path).rules)))
            inputs["fab_profile"] = {"path": origin.path,   # null when bundled
                                     "sha256": origin.sha256,
                                     "id": profile.get("id"),
                                     "bundled": origin.bundled}
        else:
            gates.append(_skipped("fab", "no --fab-profile given"))
        # 6b. KiCad's own DRC (opt-in; requested-but-unrunnable FAILS) --------
        if want_drc:
            gates.append(_drc_gate(pcb_path, cfg))
        else:
            gates.append(_skipped(
                "drc", "no --drc given (KiCad DRC stays outside the gate)"))
    else:
        gates.append(_skipped("sch-pcb", "no --pcb board given"))
        gates.append(_skipped("pcb-library", "no --pcb board given"))
        gates.append(_skipped("fab", "no --pcb board given"))
        gates.append(_skipped("drc", "no --pcb board given"))

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
        names = sorted(f.name for f in gset.files)
        inputs["gerbers"] = {"path": str(gdir),
                             "files": names,
                             # per-file evidence: WHICH bytes were gated
                             "file_hashes": {n: _sha256(gdir / n)
                                             for n in names}}
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
    from ..drivers import kicad_cli
    manifest = {
        "schema_version": _contract.SCHEMA_VERSIONS["envelope"],
        # kicad_cli: the installed kicad-cli's version string, or null when
        # none is discoverable — recorded whether or not a gate used it.
        "tool": {"akcli": __version__, "kicad_cli": kicad_cli.version_text()},
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
            if g.get("summary"):
                _emit(f"           {g['summary']}")
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
        help="run all gates (check/intent/contract/library/library-baseline/"
             "sch-pcb/pcb-library/fab/drc/order/sim/review/gerber/git) and "
             "write a release manifest")
    pp.add_argument("--sch", metavar="FILE", help="schematic (.kicad_sch)")
    pp.add_argument("--pcb", metavar="FILE", help="board (.kicad_pcb)")
    pp.add_argument("--intent", metavar="FILE", help="design-intent JSON")
    pp.add_argument("--contract", metavar="FILE", help="design-contract TOML")
    pp.add_argument("--library-baseline", dest="library_baseline",
                    metavar="FILE",
                    help="library baseline (`library audit --write-baseline`)"
                         " for the library-baseline gate; falls back to "
                         "[paths].library_baseline, then to "
                         "akcli-library.baseline.json beside the schematic")
    pp.add_argument("--fab-profile", dest="fab_profile", metavar="FILE|ID",
                    help="fab profile: a TOML file, or the id of a profile "
                         "bundled with akcli (`akcli fab profiles`); an "
                         "existing file wins")
    pp.add_argument("--drc", action="store_true",
                    help="also run KiCad's own DRC on --pcb (kicad-cli pcb "
                         "drc, on a staged copy — the project is never "
                         "modified; zone fills are checked as saved, not "
                         "refilled). A requested gate that cannot run "
                         "(no kicad-cli, no report) FAILS, never skips")
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
