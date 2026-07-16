"""`akcli calc` — standards-cited engineering calculators.

``calc list`` / ``calc info <name>`` / ``calc <name> key=value ...`` / ``calc
batch <file>``, with ``--md`` (markdown table), ``--json`` (compute envelope),
and ``--ops`` (emit a place_component op-list from a design calculator). Heavy
imports stay LAZY per handler.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..errors import EXIT
from ._shared import _did_you_mean, _dumps, _emit, _ExitWith, _stamp


def _calc_md(doc: dict) -> str:
    """Render one compute() envelope as a markdown table."""
    from ..calc.si import fmt_eng

    lines = [f"### {doc['title']}", "",
             "| result | value | note |", "|---|---|---|"]
    for k, cell in doc["results"].items():
        v, unit = cell["value"], cell.get("unit", "")
        if isinstance(v, float):
            plain = unit not in ("Ω", "V", "A", "W", "F", "H", "Hz", "s", "m")
            shown = f"{v:.6g} {unit}".strip() if plain else fmt_eng(v, unit)
        elif isinstance(v, list):
            shown = "; ".join(str(x) for x in v) if v and not isinstance(v[0], dict) \
                else f"{len(v)} entries (use --json)"
        else:
            shown = f"{v} {unit}".strip()
        lines.append(f"| {k} | {shown} | {cell.get('note', '')} |")
    lines += ["", f"*Reference: {doc['reference']}*"]
    return "\n".join(lines)


def _calc_batch(args: argparse.Namespace, params: list[str]) -> int:
    """`calc batch <file|->`: run a JSON job list, emit an array of envelopes."""
    import json as _json
    import sys as _sys

    from .. import calc as calcmod
    from ..calc.registry import CALCS, CalcError

    if not params:
        raise _ExitWith(EXIT["USAGE"], "ERROR: calc batch needs a jobs file ('-' = stdin)")
    src = _sys.stdin.read() if params[0] == "-" else None
    if src is None:
        path = Path(params[0])
        if not path.exists():
            raise _ExitWith(EXIT["NOT_FOUND"], f"ERROR: {path} not found")
        src = path.read_text(encoding="utf-8")
    try:
        doc = _json.loads(src)
        jobs = doc["jobs"] if isinstance(doc, dict) else doc
        assert isinstance(jobs, list)
    except Exception:
        raise _ExitWith(EXIT["USAGE"],
                        'ERROR: batch input must be {"jobs": [{"calc": ..., "params": {...}}, ...]}')
    out, failed = [], 0
    for i, job in enumerate(jobs):
        name = job.get("calc") if isinstance(job, dict) else None
        raw = {k: str(v) for k, v in (job.get("params") or {}).items()} \
            if isinstance(job, dict) else {}
        if not name or name not in CALCS:
            out.append({"calc": name, "error": f"job {i}: unknown calculator {name!r}"})
            failed += 1
            continue
        try:
            out.append(calcmod.compute(name, raw))
        except CalcError as exc:
            out.append({"calc": name, "error": str(exc)})
            failed += 1
    _emit(_dumps(out))
    if failed:
        print(f"{failed}/{len(jobs)} jobs failed", file=_sys.stderr)
    return EXIT["FINDINGS"] if failed else EXIT["OK"]


def _cmd_calc(args: argparse.Namespace) -> int:
    """`calc list` / `calc info <name>` / `calc <name> key=value ...`."""
    from .. import calc as calcmod
    from ..calc.registry import CALCS, CalcError
    from ..calc.si import fmt_eng

    name = getattr(args, "name", None)
    params = list(getattr(args, "params", []) or [])
    if not name or name == "list":
        if getattr(args, "json", False):
            table = {
                c.name: {
                    "title": c.title, "group": c.group,
                    "params": [{"name": p.name, "unit": p.unit, "help": p.help,
                                "required": p.default is None,
                                **({"choices": list(p.choices)} if p.choices else {})}
                               for p in c.params],
                    "reference": c.reference,
                    **({"notes": c.notes} if c.notes else {}),
                }
                for c in sorted(CALCS.values(), key=lambda c: (c.group, c.name))
            }
            _emit(_dumps(table))
            return EXIT["OK"]
        lines, group = [], None
        for c in sorted(CALCS.values(), key=lambda c: (c.group, c.name)):
            if c.group != group:
                group = c.group
                lines.append(f"[{group}]")
            req = " ".join(p.name for p in c.params if p.default is None)
            lines.append(f"  {c.name:18} {c.title}" + (f"  ({req})" if req else ""))
        lines.append("`akcli calc info <name>` shows params + the reference; "
                     "`akcli calc <name> key=value ...` runs it.")
        _emit("\n".join(lines))
        return EXIT["OK"]
    if name == "info":
        target = params[0] if params else None
        c = CALCS.get(target or "")
        if c is None:
            raise _ExitWith(EXIT["USAGE"],
                            f"ERROR: unknown calculator {target!r}"
                            f"{_did_you_mean(target or '', CALCS)} "
                            "(see `akcli calc list`)")
        lines = [f"{c.name} — {c.title}", ""]
        for p in c.params:
            d = "required" if p.default is None else f"default {p.default}"
            ch = f" one of {list(p.choices)}" if p.choices else ""
            lines.append(f"  {p.name:14} [{p.unit or '-'}] {p.help} ({d}){ch}")
        lines += ["", f"Reference: {c.reference}"]
        if c.notes:
            lines.append(f"Note: {c.notes}")
        _emit("\n".join(lines))
        return EXIT["OK"]
    if name == "batch":
        return _calc_batch(args, params)
    if name not in CALCS:
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: unknown calculator {name!r}"
                        f"{_did_you_mean(name, CALCS)} "
                        "(see `akcli calc list`)")
    raw: dict[str, str] = {}
    for tok in params:
        if "=" not in tok:
            raise _ExitWith(EXIT["USAGE"],
                            f"ERROR: expected key=value, got {tok!r}")
        k, v = tok.split("=", 1)
        raw[k.strip()] = v.strip()
    try:
        doc = calcmod.compute(name, raw)
    except CalcError as exc:
        raise _ExitWith(EXIT["USAGE"], f"ERROR: {exc}")
    if getattr(args, "ops", None):
        from ..calc import opsmap
        try:
            opdoc = opsmap.to_oplist(name, doc)
        except CalcError as exc:
            raise _ExitWith(EXIT["USAGE"], f"ERROR: {exc}")
        rendered = _dumps(opdoc)
        if args.ops == "-":
            _emit(rendered)
            return EXIT["OK"]
        Path(args.ops).write_text(rendered + "\n", encoding="utf-8")
        import sys as _sys
        print(f"op-list written to {args.ops} "
              f"({len(opdoc['ops'])} ops — edit coordinates, then `akcli plan`)",
              file=_sys.stderr)
    if getattr(args, "md", False):
        _emit(_calc_md(doc))
        return EXIT["OK"]
    if getattr(args, "json", False):
        _emit(_dumps(_stamp(doc)))
        return EXIT["OK"]
    lines = [f"{doc['title']}"]
    for key, cell in doc["results"].items():
        val, unit = cell["value"], cell.get("unit", "")
        if isinstance(val, float):
            # SI prefixes scale linearly — only prefix bare base units
            # (never mm, °C/W, m², Ω/km, ...)
            prefixable = unit in ("Ω", "V", "A", "W", "F", "H", "Hz", "s", "m")
            shown = (fmt_eng(val, unit) if prefixable
                     else f"{val:.6g} {unit}".strip())
        elif isinstance(val, list):
            shown = f"{len(val)} entries (use --json for detail)"
        else:
            shown = f"{val} {unit}".strip()
        note = f"   ({cell['note']})" if cell.get("note") else ""
        lines.append(f"  {key:22} {shown}{note}")
    lines.append(f"reference: {doc['reference']}")
    _emit("\n".join(lines))
    return EXIT["OK"]


def register(sub, common) -> None:
    p = sub.add_parser("calc", parents=[common],
                       help="engineering calculators (E-series, IPC-2221, via, "
                            "SMPS, 555, I2C, ... — every result cites its source)")
    p.add_argument("name", nargs="?",
                   help="calculator name, or `list` / `info <name>` / `batch <file>`")
    p.add_argument("params", nargs="*", metavar="key=value",
                   help="inputs, engineering notation ok (4k7, 100n, 35u)")
    p.add_argument("--md", action="store_true",
                   help="render the result as a markdown table")
    p.add_argument("--ops", metavar="FILE",
                   help="also emit a place_component op-list with the computed "
                        "values ('-' = stdout; design-type calculators only)")
    p.set_defaults(handler=_cmd_calc)
