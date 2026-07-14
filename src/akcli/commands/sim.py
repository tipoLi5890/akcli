"""`akcli sim` — schematic -> SPICE deck -> libngspice -> pass/fail assertions.

The ``sim`` command is **dual-mode**; the first positional argument selects
which mode runs:

* **schematic mode** (the first positional is a file path) wires the four
  ``..sim`` stages — ``deck`` renders the SPICE text, ``models`` resolves
  devices, ``engine`` drives libngspice in an isolated child, and ``assertions``
  turns ``.meas`` output into :class:`~akcli.report.Finding` — into
  one run::

      akcli sim <sch> [--sim FILE] [--deck-only] [--out PATH] [--gnd NET]
                      [--wave OUT.csv] [--sweep "R=a,b,c"]... [--timeout S]
                      [--json] [--exit-zero]

  ``--deck-only`` emits the deck (stdout, or ``--out``) and exits 0 — unmodeled
  warnings print but never fail it. Otherwise ``--sim`` is required: the engine
  runs, measurements are compared to their bounds, and the measured-value table
  plus findings are rendered. ``--wave`` writes a tidy CSV (single ``time``
  column + one column per ``options.wave_vectors`` entry) via
  :func:`akcli.sim.wave.rewrite_wrdata`. Repeatable ``--sweep`` runs a
  corner matrix (component-value overrides and/or ``temp=...``); the exit code is
  ``1`` if *any* corner fails.

* **fit-diode mode** (the first positional is the literal ``fit-diode``) fits a
  Shockley ``.model`` from datasheet forward points and prints the card (schematic
  mode is untouched)::

      akcli sim fit-diode --point V@I [--point V@I ...] [--n-prior 1.05]
                          [--rs-point V@I] [--cjo 50p] [--name DFIT] [--json]
                          [--apply SCH --designator D4 [--write]]

  ``--apply SCH --designator D4`` additionally plans a native ``Sim.Device`` /
  ``Sim.Params`` write onto that component. Following the ``draw`` convention it
  is **dry-run by default** — the op-list JSON it *would* apply is printed with
  instructions; ``--write`` commits it through the KiCad writer (rotated ``.bak``
  backup + connectivity re-verify).

Exit codes: ``0`` clean · ``1`` assertion/measure/corner failure · ``2``
usage/config · ``6`` deck build (no ground / node collision) or a failed apply ·
``7`` libngspice missing or an engine failure/timeout. Heavy imports stay LAZY.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import shutil
import sys
import tempfile
from pathlib import Path

from .. import report as _report
from ..errors import EXIT, AkcliError
from ._shared import (
    _did_you_mean,
    _draw_exit,
    _dumps,
    _emit,
    _ExitWith,
    _load_schematic,
    _require_path,
)

# Fixed, space-free wrdata filename written inside the (space-free) tempdir; the
# result is moved/rewritten to the user's --wave path afterwards so paths with
# spaces work.
_WAVE_FILENAME = "__akcli_wave__.data"

# The literal first positional that selects fit-diode mode instead of a path.
_FIT_DIODE = "fit-diode"

# Cartesian-product cap for --sweep corners; beyond this is a usage error.
_MAX_CORNERS = 64

_BOUND_KEYS = ("gt", "lt", "ge", "le", "approx")
_BOUND_SYMBOL = {"gt": ">", "lt": "<", "ge": ">=", "le": "<="}
_FAIL_SEVERITIES = frozenset({_report.Severity.ERROR, _report.Severity.CRITICAL})


def _deck_sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _finding_dict(f) -> dict:
    return {
        "code": f.code,
        "severity": f.severity.value,
        "message": f.message,
        "refs": list(f.refs or []),
    }


def _bound_desc(a: dict) -> str:
    """Return the human-readable bound description for an assert.

    A two-sided window carries both a lower (``gt``/``ge``) and an upper
    (``lt``/``le``) bound; render *both* (``>= 3 & <= 3.6``) so the table never
    shows only the half a failing value happens to satisfy — the earlier
    first-key-wins rendering printed a satisfied bound next to a FAIL verdict.
    """
    if "approx" in a:
        tol = a.get("tol", 0.05)
        return f"~{a['approx']:g} (tol {tol * 100:g}%)"
    parts = [f"{_BOUND_SYMBOL[k]} {a[k]:g}" for k in _BOUND_KEYS
             if k in a and k != "approx"]
    return " & ".join(parts)


def _measured_table(spec, measured: dict, findings: list) -> str:
    """Render the always-printed ``name / value / bound / verdict`` table."""
    lines = ["measured values:"]
    if not spec.asserts:
        lines.append("  (no assertions declared)")
        return "\n".join(lines)
    failed = {str(r) for f in findings for r in (f.refs or [])
              if f.severity in _FAIL_SEVERITIES}
    for a in spec.asserts:
        name = a["name"]
        desc = _bound_desc(a)
        val = measured.get(name)
        if val is None:
            valstr, verdict = "failed", "FAIL"
        else:
            valstr = f"{val:g}"
            verdict = "FAIL" if name in failed else "PASS"
        lines.append(f"  {name:<22} {valstr:>14}   {desc:<22} {verdict}")
    return "\n".join(lines)


def _deck_build_exit(exc: AkcliError) -> _ExitWith:
    """Map a deck-build ``AkcliError`` onto its process exit code.

    ``SIM_NO_GROUND``/``SIM_NODE_COLLISION`` are op-list-class failures (exit 6);
    ``BAD_CONFIG`` is a usage error (exit 2); anything else falls back to the
    registry default via :attr:`AkcliError.exit_code`.
    """
    code = {
        "SIM_NO_GROUND": EXIT["OPLIST"],
        "SIM_NODE_COLLISION": EXIT["OPLIST"],
        "BAD_CONFIG": EXIT["USAGE"],
    }.get(exc.code, exc.exit_code)
    return _ExitWith(code, exc.as_error_line())


def _cmd_sim(args: argparse.Namespace) -> int:
    # Dual-mode dispatch: the literal first positional 'fit-diode' selects the
    # diode-fit subcommand; anything else is treated as a schematic path.
    if (getattr(args, "path", None) or "") == _FIT_DIODE:
        return _cmd_fit_diode(args)

    path = _require_path(args.path)
    sim_file = getattr(args, "sim", None)
    deck_only = bool(getattr(args, "deck_only", False))
    sweeps_arg = getattr(args, "sweep", None) or []
    if not sim_file and not deck_only:
        raise _ExitWith(
            EXIT["USAGE"],
            "ERROR: sim needs --sim FILE (assertions to run) or --deck-only "
            "(emit the SPICE deck without simulating); neither was given",
        )

    sch = _load_schematic(path)
    from ..sim import assertions, deck as deckmod, engine  # lazy

    # sim.json shape/protocol errors surface as BAD_CONFIG/PROTOCOL_MISMATCH via
    # cli.main; a missing file becomes FileNotFoundError -> exit 4.
    spec = assertions.load(sim_file) if sim_file else assertions.SimSpec()

    if sweeps_arg:
        return _run_sweep(args, path, sch, spec, sweeps_arg,
                          deck_only, sim_file, assertions, deckmod, engine)

    try:
        d = deckmod.build(sch, spec, gnd=args.gnd)
    except AkcliError as exc:
        raise _deck_build_exit(exc) from exc
    deck_sha = _deck_sha(d.text)

    if deck_only:
        return _emit_deck(args, d, deck_sha)

    return _run_engine(args, path, spec, d, deck_sha, assertions, engine)


def _emit_deck(args: argparse.Namespace, d, deck_sha: str) -> int:
    """`--deck-only`: write the deck (stdout/--out/JSON); warnings never fail it."""
    if getattr(args, "json", False):
        _emit(_dumps({
            "deck_sha": deck_sha,
            "deck": d.text,
            "warnings": [_finding_dict(f) for f in d.warnings],
            "unmodeled": list(d.unmodeled),
        }))
        return EXIT["OK"]
    if getattr(args, "out", None):
        Path(args.out).write_text(d.text, encoding="utf-8")
        sys.stderr.write(f"wrote deck ({len(d.text.splitlines())} lines) to {args.out}\n")
    else:
        _emit(d.text)
    for f in d.warnings:
        sys.stderr.write(f"warning: {f.code}: {f.message}\n")
    return EXIT["OK"]


def _require_engine(engine):
    """Return the libngspice path or raise the exit-7 'missing' control-flow."""
    lib = engine.available()
    if lib is None:
        raise _ExitWith(
            EXIT["TOOL_MISSING"],
            "ERROR: NGSPICE_MISSING: libngspice not found — install KiCad or "
            "set AKCLI_NGSPICE",
        )
    return lib


def _measure(engine, assertions, spec, deck_text: str, timeout: float):
    """Run ``deck_text`` and evaluate its asserts (no waveform capture).

    Returns ``(findings, measured, ok)``. Raises the exit-7 control-flow when the
    engine itself fails, so a corner sweep aborts loudly rather than reporting a
    bogus PASS/FAIL.
    """
    commands = list(assertions.run_commands(spec))
    with tempfile.TemporaryDirectory(prefix="akcli-sim-") as td:
        result = engine.run(deck_text, commands, timeout=timeout, workdir=Path(td))
    if not result.ok:
        raise _ExitWith(
            EXIT["TOOL_MISSING"],
            f"ERROR: NGSPICE_FAILED: {result.error or 'engine failed'}",
        )
    measured = assertions.parse_meas_output(result.meas_lines)
    findings, measured = assertions.evaluate(spec, measured)
    ok = not any(f.severity in _FAIL_SEVERITIES for f in findings)
    return findings, measured, ok


def _run_engine(args, path, spec, d, deck_sha, assertions, engine) -> int:
    lib = _require_engine(engine)

    commands = list(assertions.run_commands(spec))
    wave_target: Path | None = None
    vec_list: list[str] | None = None
    if getattr(args, "wave", None):
        wave_target = Path(args.wave).resolve()
        raw_vectors = spec.options.get("wave_vectors")
        if isinstance(raw_vectors, (list, tuple)):
            vec_list = [str(v) for v in raw_vectors]
            vectors = " ".join(vec_list)
        else:
            vectors = str(raw_vectors) if raw_vectors else "all"
        # Write to a fixed name in the workdir (ngspice splits an unquoted path
        # on spaces); rewrite/move to the real --wave target after the run.
        commands.append(f"wrdata {_WAVE_FILENAME} {vectors}")

    wave_written = False
    with tempfile.TemporaryDirectory(prefix="akcli-sim-") as td:
        tdp = Path(td)
        result = engine.run(d.text, commands, timeout=args.timeout, workdir=tdp)
        if result.ok and wave_target is not None:
            produced = tdp / _WAVE_FILENAME
            if produced.exists():
                wave_target.parent.mkdir(parents=True, exist_ok=True)
                if vec_list:
                    # Tidy CSV: single scale column + one column per vector. The
                    # scale is frequency for an AC analysis, time otherwise; the
                    # captured plot is the last analysis run, so label the column
                    # from it (see assertions.run_commands).
                    from ..sim import wave as wavemod
                    analyses = list(spec.analyses)
                    last_an = analyses[-1] if analyses else "tran"
                    scale = "frequency" if last_an.lower() == "ac" else "time"
                    wavemod.rewrite_wrdata(produced, wave_target, vec_list,
                                           scale=scale)
                else:
                    # 'all' -> column names unknown; keep the raw wrdata verbatim.
                    shutil.move(str(produced), str(wave_target))
                wave_written = True
    if not result.ok:
        raise _ExitWith(
            EXIT["TOOL_MISSING"],
            f"ERROR: NGSPICE_FAILED: {result.error or 'engine failed'}",
        )

    measured = assertions.parse_meas_output(result.meas_lines)
    findings, measured = assertions.evaluate(spec, measured)
    all_findings = [*d.warnings, *findings]
    ok = not any(f.severity in _FAIL_SEVERITIES for f in findings)

    if getattr(args, "json", False):
        _emit(_dumps({
            "deck_sha": deck_sha,
            "engine": lib,
            "measured": measured,
            "findings": [_finding_dict(f) for f in all_findings],
            "ok": ok,
        }))
    else:
        header = f"# akcli sim\n  engine: {lib}\n  deck sha1: {deck_sha}"
        table = _measured_table(spec, measured, findings)
        rendered = _report.render(all_findings, "text", {}, source=str(path))
        _emit(header + "\n\n" + table + "\n\n" + rendered)
        if wave_target is not None and wave_written:
            sys.stderr.write(f"wrote waveform to {wave_target}\n")

    if getattr(args, "exit_zero", False):
        return EXIT["OK"]
    return EXIT["OK"] if ok else EXIT["FINDINGS"]


# --------------------------------------------------------------------------- #
# --sweep: corner matrix
# --------------------------------------------------------------------------- #
def _parse_sweeps(specs: list[str], sch) -> list[tuple[str, str, list[str]]]:
    """Parse ``--sweep`` strings into ``(key, kind, values)`` tuples.

    Each spec is ``NAME=v1,v2,...``; ``NAME`` is either the case-insensitive
    literal ``temp`` (a ``.option temp`` sweep) or an existing component
    designator (a per-corner value override). Anything else is a usage error.
    """
    designators = {c.designator for c in sch.components}
    out: list[tuple[str, str, list[str]]] = []
    for spec in specs:
        if "=" not in spec:
            raise _ExitWith(EXIT["USAGE"],
                            f"ERROR: --sweep must be 'NAME=v1,v2,...', got {spec!r}")
        key, rhs = spec.split("=", 1)
        key = key.strip()
        values = [v.strip() for v in rhs.split(",") if v.strip()]
        if not key:
            raise _ExitWith(EXIT["USAGE"], f"ERROR: --sweep {spec!r} has an empty name")
        if not values:
            raise _ExitWith(EXIT["USAGE"], f"ERROR: --sweep {key!r} has no values")
        if key.lower() == "temp":
            kind = "temp"
        elif key in designators:
            kind = "component"
        else:
            hint = _did_you_mean(key, designators | {"temp"})
            raise _ExitWith(
                EXIT["USAGE"],
                f"ERROR: --sweep {key!r} is neither 'temp' nor a component "
                f"designator on this schematic{hint}",
            )
        out.append((key, kind, values))
    return out


def _inject_temp(deck_text: str, temp: str) -> str:
    """Insert a ``.option temp=<temp>`` card just before the deck's ``.end``."""
    lines = deck_text.rstrip("\n").split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().lower() == ".end":
            lines.insert(i, f".option temp={temp}")
            break
    else:
        lines.append(f".option temp={temp}")
    return "\n".join(lines) + "\n"


def _apply_corner(sch, assignment: list[tuple[str, str, str]]):
    """Return ``(sch_copy, temp)`` with per-corner component overrides applied.

    ``assignment`` is a list of ``(key, kind, value)`` for one corner. Component
    overrides are applied on a deep copy of ``sch`` (so ``deck.build`` needs no
    change); a ``temp`` entry is returned separately for :func:`_inject_temp`.
    """
    sch2 = copy.deepcopy(sch)
    temp: str | None = None
    for key, kind, value in assignment:
        if kind == "temp":
            temp = value
        else:  # component value override
            for comp in sch2.components:
                if comp.designator == key:
                    comp.value = value
    return sch2, temp


def _sweep_effect_warnings(sch, spec, sweeps, models) -> list:
    """Warn for component ``--sweep`` overrides that the deck will silently ignore.

    ``_apply_corner`` only rewrites ``comp.value``, but a component whose SPICE
    card resolves from ``Sim.Params`` / a ``spec.models`` entry / a D/Q/X device
    model never consults that value — so every corner would be byte-identical
    with no warning, and an engineer could sign off a corner analysis that varied
    nothing. Probe each component sweep with its first value and flag any whose
    resolved card does not actually carry that value.
    """
    warnings: list = []
    for key, kind, values in sweeps:
        if kind != "component":
            continue
        comp = next((c for c in sch.components if c.designator == key), None)
        if comp is None:
            continue
        probe = copy.deepcopy(comp)
        probe.value = values[0]
        try:
            card = models.resolve(probe, spec)
        except AkcliError:
            continue  # a resolve error surfaces later on the real build
        effective = (getattr(card, "status", "") == "ok"
                     and getattr(card, "value", None) == models.spice_value(values[0]))
        if not effective:
            warnings.append(_report.Finding(
                "SIM_SWEEP_IGNORED", _report.Severity.WARNING,
                f"--sweep {key}=... has no effect: {key}'s SPICE card does not "
                f"take its component value (it resolves through Sim.Params, a "
                f"spec.models entry, or a D/Q/X device model), so every corner "
                f"is identical. Sweep the model's parameter directly, or drop "
                f"the Sim.Params/spec.models override on {key}.",
                refs=[key]))
    return warnings


def _run_sweep(args, path, sch, spec, sweeps_arg, deck_only, sim_file,
               assertions, deckmod, engine) -> int:
    """Run a ``--sweep`` corner matrix and render the results."""
    if deck_only:
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: --sweep runs the engine; it cannot be combined "
                        "with --deck-only")
    if not sim_file:
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: --sweep needs --sim FILE (the asserts evaluated "
                        "per corner)")
    if getattr(args, "wave", None):
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: --wave cannot be combined with --sweep")

    sweeps = _parse_sweeps(sweeps_arg, sch)
    total = 1
    for _key, _kind, values in sweeps:
        total *= len(values)
    if total > _MAX_CORNERS:
        raise _ExitWith(
            EXIT["USAGE"],
            f"ERROR: --sweep would produce {total} corners (cap is "
            f"{_MAX_CORNERS}); reduce the number of sweep points",
        )

    lib = _require_engine(engine)

    from ..sim import models  # lazy; for the sweep-effectiveness probe
    # Warn up front for component sweeps the deck will silently ignore.
    notices: list = _sweep_effect_warnings(sch, spec, sweeps, models)
    # Deck-build diagnostics (floating node, rshunt auto-add, unmodeled, ...) are
    # gathered per corner and deduped: single-run mode prints them, so sign-off
    # sweep mode must too — an auto-inserted rshunt must never mask a mis-wire.
    seen_warn: set[tuple[str, str]] = set()

    # Each corner is one pick from every sweep's value list (Cartesian product).
    value_axes = [[(key, kind, v) for v in values] for key, kind, values in sweeps]
    corners: list[dict] = []
    any_fail = False
    for assignment in itertools.product(*value_axes):
        sch2, temp = _apply_corner(sch, list(assignment))
        try:
            d = deckmod.build(sch2, spec, gnd=args.gnd)
        except AkcliError as exc:
            raise _deck_build_exit(exc) from exc
        for w in d.warnings:
            key = (w.code, w.message)
            if key not in seen_warn:
                seen_warn.add(key)
                notices.append(w)
        deck_text = _inject_temp(d.text, temp) if temp is not None else d.text
        findings, measured, ok = _measure(engine, assertions, spec, deck_text,
                                           args.timeout)
        if not ok:
            any_fail = True
        corners.append({
            "params": {key: value for key, _kind, value in assignment},
            "measured": measured,
            "ok": ok,
            "findings": [_finding_dict(f) for f in findings],
        })

    if getattr(args, "json", False):
        _emit(_dumps({"engine": lib, "corners": corners,
                      "warnings": [_finding_dict(f) for f in notices],
                      "ok": not any_fail}))
    else:
        table = _sweep_table(lib, spec, sweeps, corners)
        if notices:
            table += "\n\n" + _report.render(notices, "text", {},
                                             source=str(path))
        _emit(table)

    if getattr(args, "exit_zero", False):
        return EXIT["OK"]
    return EXIT["FINDINGS"] if any_fail else EXIT["OK"]


def _sweep_table(lib: str, spec, sweeps, corners: list[dict]) -> str:
    """Render the corner-matrix table (sweep params + each measured value)."""
    param_cols = [key for key, _kind, _values in sweeps]
    meas_cols = [a["name"] for a in spec.asserts]
    header = ["corner", *param_cols, *meas_cols, "verdict"]

    rows: list[list[str]] = []
    for i, corner in enumerate(corners, start=1):
        row = [str(i)]
        row += [str(corner["params"].get(c, "")) for c in param_cols]
        for m in meas_cols:
            val = corner["measured"].get(m)
            row.append("failed" if val is None else f"{val:g}")
        row.append("PASS" if corner["ok"] else "FAIL")
        rows.append(row)

    widths = [len(h) for h in header]
    for row in rows:
        for j, cell in enumerate(row):
            widths[j] = max(widths[j], len(cell))

    def _fmt(cells: list[str]) -> str:
        return "  ".join(c.ljust(widths[j]) for j, c in enumerate(cells))

    lines = [
        f"# akcli sim (sweep: {len(corners)} corner(s))",
        f"  engine: {lib}",
        "",
        _fmt(header),
        _fmt(["-" * w for w in widths]),
    ]
    lines += [_fmt(row) for row in rows]
    passed = sum(1 for c in corners if c["ok"])
    lines.append("")
    lines.append(f"corners: {passed}/{len(corners)} passed")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# fit-diode mode
# --------------------------------------------------------------------------- #
def _parse_vi(text: str, what: str) -> tuple[float, float]:
    """Parse a ``V@I`` datasheet point (each side in engineering notation)."""
    if "@" not in text:
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: {what} must be 'V@I' (e.g. '0.3@1m'), got {text!r}")
    v_txt, i_txt = text.split("@", 1)
    from ..calc.registry import CalcError
    from ..calc.si import parse_value
    try:
        return parse_value(v_txt, name="V"), parse_value(i_txt, name="I")
    except CalcError as exc:
        raise _ExitWith(EXIT["USAGE"], f"ERROR: {what} {text!r}: {exc}") from exc


def _sim_params_string(models, result: dict) -> str:
    """Build a KiCad ``Sim.Params`` string from a :func:`fit_diode` result."""
    parts = [f"IS={result['IS']:.4e}", f"N={result['N']:.4f}"]
    if result.get("RS"):
        parts.append(f"RS={result['RS']:.4g}")
    if result.get("CJO") is not None:
        parts.append(f"CJO={models.spice_value(str(result['CJO']))}")
    return " ".join(parts)


def _cmd_fit_diode(args: argparse.Namespace) -> int:
    from ..sim import models  # lazy

    points_arg = getattr(args, "point", None) or []
    if not points_arg:
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: fit-diode needs at least one --point V@I "
                        "(e.g. --point 0.3@1m)")
    points = [_parse_vi(p, "--point") for p in points_arg]

    rs_point = None
    if getattr(args, "rs_point", None):
        rs_point = _parse_vi(args.rs_point, "--rs-point")

    cjo = None
    if getattr(args, "cjo", None):
        from ..calc.registry import CalcError
        from ..calc.si import parse_value
        try:
            cjo = parse_value(args.cjo, name="cjo")
        except CalcError as exc:
            raise _ExitWith(EXIT["USAGE"], f"ERROR: --cjo {args.cjo!r}: {exc}") from exc

    n_prior = getattr(args, "n_prior", 1.05)
    # fit_diode raises AkcliError('BAD_CONFIG') (exit 2 via cli.main) or ValueError.
    try:
        result = models.fit_diode(points, n_prior=n_prior,
                                  rs_point=rs_point, cjo=cjo)
    except ValueError as exc:
        raise _ExitWith(EXIT["USAGE"], f"ERROR: fit-diode: {exc}") from exc

    name = (getattr(args, "name", None) or "DFIT").strip() or "DFIT"
    model_card = result["model_card"]
    if name != "DFIT":
        model_card = model_card.replace("DFIT", name, 1)
    sim_params = _sim_params_string(models, result)

    if getattr(args, "apply", None):
        return _fit_diode_apply(args, models, result, name, model_card, sim_params)

    note = result.get("note") or ""
    if getattr(args, "json", False):
        _emit(_dumps({
            "name": name,
            "model_card": model_card,
            "sim_params": sim_params,
            "params": {"IS": result["IS"], "N": result["N"],
                       "RS": result["RS"], "CJO": result["CJO"]},
            "note": note,
        }))
    else:
        lines = ["# akcli sim fit-diode", "", model_card, "",
                 f"Sim.Params: {sim_params}"]
        if note:
            lines += ["", f"note: {note}"]
        _emit("\n".join(lines))
    return EXIT["OK"]


def _fit_diode_op(designator: str, sim_params: str) -> dict:
    """The single-op op-list that writes native Sim.* fields onto a component."""
    from .. import ops  # lazy
    return {
        "protocol_version": ops.PROTOCOL_VERSION,
        "target_format": "kicad",
        "ops": [{
            "op": "set_component_parameters",
            "designator": designator,
            "parameters": {"Sim.Device": "D", "Sim.Params": sim_params},
        }],
    }


def _fit_diode_apply(args, models, result, name, model_card,
                     sim_params) -> int:
    """`fit-diode --apply SCH --designator D4`: plan (dry-run) or commit the write.

    Dry-run (default) prints the op-list JSON it *would* apply plus instructions;
    ``--write`` commits it through the KiCad writer with a rotated ``.bak`` backup
    and post-write connectivity re-verify (the ``draw`` convention).
    """
    target = Path(args.apply)
    designator = getattr(args, "designator", None)
    if not designator:
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: fit-diode --apply needs --designator REF (the "
                        "component to write the fit onto)")
    oplist = _fit_diode_op(designator, sim_params)
    commit = bool(getattr(args, "write", False))

    if not commit:
        if getattr(args, "json", False):
            _emit(_dumps({
                "name": name,
                "model_card": model_card,
                "sim_params": sim_params,
                "designator": designator,
                "target": str(target),
                "oplist": oplist,
                "applied": False,
            }))
        else:
            _emit(
                "# akcli sim fit-diode (dry-run)\n\n"
                f"{model_card}\n\n"
                f"would set on {designator} in {target}:\n"
                f"{_dumps(oplist)}\n\n"
                "re-run with --write to apply "
                f"(writes {target.name}.bak; `akcli undo` reverts)"
            )
        return EXIT["OK"]

    from ..writers import kicad as kwriter  # lazy
    findings: list = []
    try:
        results = kwriter.apply(oplist, str(target), apply=True,
                                backup_dir=target.parent, verify_out=findings)
    except AkcliError as exc:
        raise _ExitWith(exc.exit_code, exc.as_error_line()) from exc

    code = _draw_exit(results, findings)
    if getattr(args, "json", False):
        _emit(_dumps({
            "name": name,
            "model_card": model_card,
            "sim_params": sim_params,
            "designator": designator,
            "target": str(target),
            "oplist": oplist,
            "applied": code == EXIT["OK"],
            "results": [r.to_dict() for r in results],
        }))
    else:
        if code == EXIT["OK"]:
            _emit(f"# akcli sim fit-diode\n\n{model_card}\n\n"
                  f"applied Sim.Device=D / Sim.Params to {designator} in {target}\n"
                  f"(backup {target.name}.bak; `akcli undo` reverts)")
        else:
            _emit(f"# akcli sim fit-diode\n\nfailed to apply to {designator} in "
                  f"{target} — nothing written")
    return code


def register(sub, common) -> None:
    p = sub.add_parser(
        "sim", parents=[common],
        help="simulate a schematic with ngspice and assert on the results "
             "(or 'sim fit-diode' to fit a diode .model)",
    )
    p.add_argument("path", nargs="?",
                   help="input schematic, or the literal 'fit-diode'")
    # --- schematic mode --- #
    p.add_argument("--sim", metavar="FILE",
                   help="sim assertion spec (sim.json: stimuli, analyses, asserts)")
    p.add_argument("--deck-only", action="store_true",
                   help="emit the SPICE deck and exit 0 (no simulation)")
    p.add_argument("--out", metavar="PATH",
                   help="write the deck here (--deck-only) instead of stdout")
    p.add_argument("--gnd", metavar="NET", default="GND",
                   help="net that becomes SPICE ground node 0 (default: GND)")
    p.add_argument("--wave", metavar="OUT.csv",
                   help="write simulated waveforms to this tidy CSV "
                        "(needs options.wave_vectors for named columns)")
    p.add_argument("--sweep", action="append", metavar="NAME=v1,v2,...",
                   help="corner sweep: a component-value override "
                        "(R21=2.2k,3.3k) or temp=0,25,60 — repeatable, "
                        "≤64 corners total")
    p.add_argument("--timeout", type=float, default=60.0, metavar="S",
                   help="kill the engine after S seconds (default: 60)")
    p.add_argument("--exit-zero", action="store_true",
                   help="always exit 0 even when assertions/corners fail")
    # --- fit-diode mode ('sim fit-diode ...') --- #
    p.add_argument("--point", action="append", metavar="V@I",
                   help="fit-diode: a datasheet forward point (repeatable)")
    p.add_argument("--n-prior", type=float, default=1.05, metavar="N",
                   help="fit-diode: ideality prior for a single-point fit "
                        "(default: 1.05)")
    p.add_argument("--rs-point", metavar="V@I",
                   help="fit-diode: a high-current point to solve RS")
    p.add_argument("--cjo", metavar="F",
                   help="fit-diode: junction capacitance (e.g. 50p)")
    p.add_argument("--name", metavar="MODEL", default="DFIT",
                   help="fit-diode: .model name (default: DFIT)")
    p.add_argument("--apply", metavar="SCH",
                   help="fit-diode: schematic to write the fit onto "
                        "(dry-run unless --write)")
    p.add_argument("--designator", metavar="REF",
                   help="fit-diode: component to write (with --apply)")
    p.add_argument("--write", action="store_true",
                   help="fit-diode: commit the --apply write (rotated .bak); "
                        "default prints the op-list it would apply")
    p.set_defaults(handler=_cmd_sim)
