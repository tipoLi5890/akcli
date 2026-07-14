"""SPICE-simulation assertion specs: ``sim.json`` -> meas statements -> Findings.

A sim spec (JSON) describes stimuli, which ngspice analyses to run, and a list
of pass/fail assertions against ``.meas`` results::

    {"protocol_version": 1,
     "stimuli": [{"...": "..."}],
     "analyses": {"tran": "5u 100m", "ac": "dec 40 10 100k", "op": ""},
     "models": {"...": "..."},
     "assert": [
        {"name": "vpeak_max", "meas": "MAX v(peak) from=20m to=60m", "gt": "0.35"},
        {"name": "t_detect", "when": "v(peak)=0.297 RISE=1", "lt": "25m"}
     ],
     "options": {}}

Each assert entry names exactly one ``.meas`` source (``meas``: the verbatim
text after ``meas <analysis> <name>``, e.g. ``"MAX v(peak) from=20m to=60m"``;
or ``when``: shorthand for a ``WHEN`` measurement, e.g. ``"v(peak)=0.297
RISE=1"``) and at least one bound. Bounds are either a single ``approx`` or a
window: up to one lower (``gt``/``ge``) **and** up to one upper (``lt``/``le``)
bound, so a two-sided ``{"ge": "3.0", "le": "3.6"}`` is valid. Every bound
accepts engineering-notation strings like ``"25m"`` via the same notation rules
as :mod:`akcli.calc.si`; ``approx`` additionally accepts ``tol``
(relative, default ``0.05`` = 5%) and cannot be combined with other bounds.

``options.rshunt`` (absent/``"auto"``/``false``/number/string) tunes the
deck-builder's floating-node fix — see :mod:`akcli.sim.deck`.

``load`` raises ``AkcliError('BAD_CONFIG', ...)`` naming the offending entry
for shape errors, and ``AkcliError('PROTOCOL_MISMATCH', ...)`` when
``protocol_version`` is greater than the version this build understands
(mirrors :mod:`akcli.ops` / :mod:`akcli.checks.intent`).

``meas_statements`` emits the ``meas <analysis> <name> <text>`` lines to feed
ngspice; ``parse_meas_output`` reads them back from the engine's
``SendChar``-callback lines (verbatim ngspice 45.2 format, captured live);
``evaluate`` turns measured values into pass/fail :class:`~akcli.report.Finding`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import AkcliError, fail
from ..report import Finding, Severity

PROTOCOL_VERSION = 1

SIM_ASSERT_FAIL = "SIM_ASSERT_FAIL"
SIM_MEAS_FAILED = "SIM_MEAS_FAILED"

_TOP_KEYS = frozenset(
    {"protocol_version", "stimuli", "analyses", "models", "assert", "options"}
)
_BOUND_KEYS = ("gt", "lt", "ge", "le", "approx")
_LOWER_KEYS = ("gt", "ge")   # lower bounds (value must exceed / reach)
_UPPER_KEYS = ("lt", "le")   # upper bounds (value must stay under / at)
_ASSERT_KEYS = frozenset({"name", "meas", "when", "analysis", "tol", *_BOUND_KEYS})
_DEFAULT_TOL = 0.05
# A stimulus 'name' becomes the SPICE element designator, so it must be a bare
# identifier (no spaces/punctuation that would corrupt the element line).
_STIM_NAME_RX = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


@dataclass
class SimSpec:
    """A validated ``sim.json`` document."""

    stimuli: list[dict] = field(default_factory=list)
    analyses: dict[str, str] = field(default_factory=dict)
    asserts: list[dict] = field(default_factory=list)
    models: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# value parsing (engineering notation, same rules as calc.si)
# --------------------------------------------------------------------------- #
def _parse_num(text: object, where: str, field_name: str) -> float:
    """Parse an assert bound: a plain number or an engineering-notation string
    (``"25m"``, ``"4.7k"``, ...), via :func:`akcli.calc.si.parse_value`.
    """
    if isinstance(text, bool):  # bool is an int subclass -- reject explicitly
        fail("BAD_CONFIG", f"{where}: {field_name} must be a number, got {text!r}")
    if isinstance(text, (int, float)):
        return float(text)
    if isinstance(text, str):
        from ..calc.registry import CalcError
        from ..calc.si import parse_value
        try:
            return parse_value(text, name=field_name)
        except CalcError as exc:
            fail("BAD_CONFIG", f"{where}: {field_name}: {exc}")
    fail("BAD_CONFIG", f"{where}: {field_name} must be a number, got {text!r}")
    raise AssertionError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------- #
# load / validate
# --------------------------------------------------------------------------- #
def _validate_assert(raw: object, idx: int, analyses: dict, where: str) -> dict:
    path = f"{where}: assert[{idx}]"
    if not isinstance(raw, dict):
        fail("BAD_CONFIG", f"{path}: must be an object")
    extra = set(raw) - _ASSERT_KEYS
    if extra:
        fail("BAD_CONFIG",
             f"{path}: unknown key(s): {', '.join(sorted(map(str, extra)))}")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        fail("BAD_CONFIG", f"{path}: 'name' must be a non-empty string")
    path = f"{where}: assert '{name}'"

    has_meas = "meas" in raw
    has_when = "when" in raw
    if has_meas == has_when:
        fail("BAD_CONFIG",
             f"{path}: exactly one of 'meas' or 'when' is required")
    src_key = "meas" if has_meas else "when"
    src_val = raw[src_key]
    if not isinstance(src_val, str) or not src_val.strip():
        fail("BAD_CONFIG", f"{path}: '{src_key}' must be a non-empty string")

    # An assert carries either a single 'approx', or up to one lower bound
    # (gt|ge) AND up to one upper bound (lt|le) — a two-sided window — with at
    # least one bound present.
    present = [k for k in _BOUND_KEYS if k in raw]
    if not present:
        fail("BAD_CONFIG",
             f"{path}: at least one bound key is required "
             f"({'/'.join(_BOUND_KEYS)})")
    if "approx" in present and len(present) > 1:
        fail("BAD_CONFIG",
             f"{path}: 'approx' cannot be combined with other bound key(s) "
             f"{[k for k in present if k != 'approx']}")
    lowers = [k for k in present if k in _LOWER_KEYS]
    uppers = [k for k in present if k in _UPPER_KEYS]
    if len(lowers) > 1:
        fail("BAD_CONFIG",
             f"{path}: at most one lower bound key ({'/'.join(_LOWER_KEYS)}), "
             f"found {lowers}")
    if len(uppers) > 1:
        fail("BAD_CONFIG",
             f"{path}: at most one upper bound key ({'/'.join(_UPPER_KEYS)}), "
             f"found {uppers}")

    tol = _DEFAULT_TOL
    if "tol" in raw:
        if "approx" not in present:
            fail("BAD_CONFIG", f"{path}: 'tol' is only valid with 'approx'")
        tol = _parse_num(raw["tol"], path, "tol")

    analysis = raw.get("analysis")
    if analysis is not None:
        if not isinstance(analysis, str) or analysis not in analyses:
            fail("BAD_CONFIG",
                 f"{path}: analysis {analysis!r} not in configured analyses "
                 f"{sorted(analyses)}")

    out: dict = {"name": name, src_key: src_val}
    for k in present:
        out[k] = _parse_num(raw[k], path, k)
    if analysis is not None:
        out["analysis"] = analysis
    if "approx" in present:
        out["tol"] = tol
    return out


def load(path: str | Path) -> SimSpec:
    """Load and validate a ``sim.json`` document (see module docstring for shape).

    Raises ``AkcliError('BAD_CONFIG', ...)`` naming the offending entry for
    shape errors, or ``AkcliError('PROTOCOL_MISMATCH', ...)`` when
    ``protocol_version`` is newer than this build understands.
    ``FileNotFoundError`` propagates (the CLI maps it to exit 4).
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AkcliError("BAD_CONFIG", f"invalid sim JSON in {p}: {exc}") from exc

    where = str(p)
    if not isinstance(doc, dict):
        fail("BAD_CONFIG", f"{where}: sim spec root must be a JSON object")
    extra = set(doc) - _TOP_KEYS
    if extra:
        fail("BAD_CONFIG",
             f"{where}: unknown key(s): {', '.join(sorted(map(str, extra)))} "
             f"(expected {', '.join(sorted(_TOP_KEYS))})")

    pv = doc.get("protocol_version")
    if not isinstance(pv, int) or isinstance(pv, bool) or pv < 1:
        fail("BAD_CONFIG", f"{where}: protocol_version must be a positive integer")
    if pv > PROTOCOL_VERSION:
        fail("PROTOCOL_MISMATCH",
             f"{where}: sim protocol_version {pv!r} > {PROTOCOL_VERSION}")

    stimuli = doc.get("stimuli", [])
    if not isinstance(stimuli, list) or not all(isinstance(s, dict) for s in stimuli):
        fail("BAD_CONFIG", f"{where}: 'stimuli' must be an array of objects")
    stim_names: list[str] = []
    for i, s in enumerate(stimuli):
        nm = s.get("name")
        if not isinstance(nm, str) or not _STIM_NAME_RX.match(nm):
            fail("BAD_CONFIG",
                 f"{where}: stimuli[{i}]: 'name' must be an identifier matching "
                 f"^[A-Za-z][A-Za-z0-9_]*$ (it becomes the SPICE element name), "
                 f"got {nm!r}")
        stim_names.append(nm)
    stim_dupes = sorted({n for n in stim_names if stim_names.count(n) > 1})
    if stim_dupes:
        fail("BAD_CONFIG",
             f"{where}: duplicate stimulus name(s): {', '.join(stim_dupes)}")

    analyses = doc.get("analyses", {})
    if not isinstance(analyses, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in analyses.items()
    ):
        fail("BAD_CONFIG", f"{where}: 'analyses' must be an object of string -> string")

    models = doc.get("models", {})
    if not isinstance(models, dict):
        fail("BAD_CONFIG", f"{where}: 'models' must be an object")

    options = doc.get("options", {})
    if not isinstance(options, dict):
        fail("BAD_CONFIG", f"{where}: 'options' must be an object")
    if "rshunt" in options:
        rv = options["rshunt"]
        # bool (false=off / true=auto), number, or string ("auto" or a literal
        # value such as "1e12"/"1G"); anything else is a config error.
        if not isinstance(rv, (bool, int, float, str)):
            fail("BAD_CONFIG",
                 f"{where}: options.rshunt must be a boolean, a number, or a "
                 f"string (got {rv!r})")

    raw_asserts = doc.get("assert", [])
    if not isinstance(raw_asserts, list):
        fail("BAD_CONFIG", f"{where}: 'assert' must be an array")
    asserts = [
        _validate_assert(a, i, analyses, where) for i, a in enumerate(raw_asserts)
    ]
    names = [a["name"] for a in asserts]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        fail("BAD_CONFIG", f"{where}: duplicate assert name(s): {', '.join(dupes)}")

    return SimSpec(
        stimuli=stimuli, analyses=analyses, asserts=asserts,
        models=models, options=options,
    )


# --------------------------------------------------------------------------- #
# meas statement emission
# --------------------------------------------------------------------------- #
_FROM_TO_RX = re.compile(r"\b(from|to)\s*=", re.IGNORECASE)


def _infer_analysis(a: dict, spec: SimSpec) -> str:
    """Pick the ``.meas`` analysis for one validated assert dict.

    Explicit ``"analysis"`` wins; else ``"when"``-style and ``from=``/``to=``
    measurements default to ``tran``; ``FIND ... AT`` defaults to ``ac`` when
    the spec configures an ``ac`` analysis; everything else falls back to
    ``tran``. Raises ``BAD_CONFIG`` if the inferred analysis was never
    configured in ``analyses``.
    """
    analysis = a.get("analysis")
    if analysis is None:
        if "when" in a:
            analysis = "tran"
        else:
            text = a["meas"]
            if _FROM_TO_RX.search(text):
                analysis = "tran"
            elif text.strip().upper().startswith("FIND") and "ac" in spec.analyses:
                analysis = "ac"
            else:
                analysis = "tran"
    if analysis not in spec.analyses:
        fail("BAD_CONFIG",
             f"assert '{a['name']}': inferred analysis {analysis!r} not in "
             f"configured analyses {sorted(spec.analyses)}")
    return analysis


def meas_statements(spec: SimSpec) -> list[str]:
    """Emit one ``meas <analysis> <name> <meas-text>`` line per assert, in
    document order (deterministic)."""
    out = []
    for a in spec.asserts:
        analysis = _infer_analysis(a, spec)
        text = a["meas"] if "meas" in a else f"WHEN {a['when']}"
        out.append(f"meas {analysis} {a['name']} {text}")
    return out


def _analysis_command(analysis: str, params: str) -> str:
    """The interactive ngspice command that runs one configured analysis.

    Normalizes the ``analyses`` value the same way :mod:`akcli.sim.deck`
    does for its dot-cards, but as an *interactive* command (no leading dot):
    ``("tran", "10u 2m") -> "tran 10u 2m"``, ``("tran", "tran 10u 2m") ->
    "tran 10u 2m"``, ``("op", "") -> "op"``, ``("ac", ".ac dec 5 10 100k") ->
    "ac dec 5 10 100k"``.
    """
    p = (params or "").strip()
    if p.startswith("."):
        p = p[1:].strip()
    if not p:
        return analysis
    if p.split(" ", 1)[0].lower() == analysis.lower():
        return p
    return f"{analysis} {p}"


def run_commands(spec: SimSpec) -> list[str]:
    """Interactive ngspice command list that runs every analysis and its meas.

    A single ``run`` executes only the *first* dot-analysis in the deck, so a
    multi-analysis spec (e.g. ``tran`` + ``ac``) would leave every other
    analysis's ``.meas`` with no plot to read — ngspice then prints
    ``Error: meas ac ...`` and the whole run is misdiagnosed as an engine
    failure. Instead we drive each analysis explicitly: for every entry in
    ``spec.analyses`` (document order) we issue its interactive command
    (``tran ...``/``ac ...``/``op``) and immediately follow it with the ``meas``
    lines whose analysis resolves to it, so each measurement reads the plot its
    own analysis just produced. Analyses with no asserts still run (waveform
    capture needs them). When ``spec.analyses`` is empty we fall back to a bare
    ``run`` so any dot-analysis the deck itself carries still executes.
    """
    grouped: dict[str, list[str]] = {}
    for a in spec.asserts:
        analysis = _infer_analysis(a, spec)
        text = a["meas"] if "meas" in a else f"WHEN {a['when']}"
        grouped.setdefault(analysis, []).append(
            f"meas {analysis} {a['name']} {text}")

    cmds: list[str] = []
    for analysis, params in spec.analyses.items():
        if not analysis:
            continue
        cmds.append(_analysis_command(analysis, params))
        cmds.extend(grouped.get(analysis, []))
    return cmds or ["run"]


# --------------------------------------------------------------------------- #
# meas output parsing (verbatim ngspice 45.2 SendChar-callback lines)
# --------------------------------------------------------------------------- #
# success: "stdout vpeak_max           =  4.912189e-01 at=  5.600927e-02"
#          "stdout ripple              =  1.572552e-01 from=  4.0e-02 to=..."
#          "stdout t_detect            =  2.233761e-02"          (no at=/from=)
_OK_RX = re.compile(
    r"^stdout\s+(?P<name>\S+)\s*=\s*(?P<value>[+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)"
)
# failure: "stdout meas tran nope WHEN v(a)=99 RISE=1 failed!"
_FAIL_RX = re.compile(r"^stdout\s+meas\s+(?P<analysis>\S+)\s+(?P<name>\S+)\s+.*failed!\s*$")


def parse_meas_output(lines: list[str]) -> dict[str, float | None]:
    """Parse ngspice ``SendChar`` lines into ``{name: value}`` (``None`` for a
    failed measurement, e.g. a ``WHEN``/edge that never crossed)."""
    results: dict[str, float | None] = {}
    for raw in lines:
        line = raw.rstrip("\n\r")
        m = _FAIL_RX.match(line)
        if m:
            results[m.group("name")] = None
            continue
        m = _OK_RX.match(line)
        if m:
            results[m.group("name")] = float(m.group("value"))
    return results


# --------------------------------------------------------------------------- #
# evaluate
# --------------------------------------------------------------------------- #
_BOUND_SYMBOL = {"gt": ">", "lt": "<", "ge": ">=", "le": "<="}


def _bound_ok(bound_key: str, value: float, bound: float, tol: float) -> bool:
    if bound_key == "gt":
        return value > bound
    if bound_key == "lt":
        return value < bound
    if bound_key == "ge":
        return value >= bound
    if bound_key == "le":
        return value <= bound
    # approx
    ref = abs(bound) if bound != 0 else 1.0
    return abs(value - bound) <= tol * ref


def evaluate(
    spec: SimSpec, results: dict[str, float | None]
) -> tuple[list[Finding], dict[str, float | None]]:
    """Compare measured values against each assert's bound.

    Returns ``(findings, measured)`` where ``measured`` reports every assert's
    value (``None`` for a failed measurement, or a missing one) in document
    order. Findings: ``SIM_MEAS_FAILED`` (ERROR) when the measurement itself
    failed or never ran; ``SIM_ASSERT_FAIL`` (ERROR) when it ran but violated
    its bound; nothing on pass.
    """
    findings: list[Finding] = []
    measured: dict[str, float | None] = {}
    for a in spec.asserts:
        name = a["name"]
        value = results.get(name)
        measured[name] = value
        if value is None:
            findings.append(Finding(
                SIM_MEAS_FAILED, Severity.ERROR,
                f"measurement '{name}' failed (no result from ngspice)",
                refs=[name],
            ))
            continue
        tol = a.get("tol", _DEFAULT_TOL)
        # A two-sided assert carries both a lower (gt|ge) and an upper (lt|le)
        # bound; report the first side the value violates.
        for bound_key in _BOUND_KEYS:
            if bound_key not in a:
                continue
            bound = a[bound_key]
            if _bound_ok(bound_key, value, bound, tol):
                continue
            if bound_key == "approx":
                desc = f"violates ~{bound:g} (tol {tol * 100:g}%)"
            else:
                desc = f"violates {_BOUND_SYMBOL[bound_key]} {bound:g}"
            findings.append(Finding(
                SIM_ASSERT_FAIL, Severity.ERROR,
                f"{name} = {value:g} {desc}",
                refs=[name],
            ))
            break
    return findings, measured
