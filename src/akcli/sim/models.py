"""Device resolution ladder + datasheet fits + builtin behavioral library.

This module answers one question for the deck builder: *given a schematic
component, what SPICE primitive (if any) should it become?* The answer is a
:class:`DeviceCard`. Resolution follows a strict first-hit-wins ladder:

1. ``Sim.*`` symbol fields (KiCad 7+ convention) — the user's explicit intent.
2. A ``spec.models`` override keyed by designator or lib_id.
3. A prefix heuristic (R/C/L with a parseable value -> passive; D/Q with no
   model -> *unmodeled*, because a diode or transistor without a model card
   would be a silent lie; connectors/test-points -> *skip*).

Anything the ladder cannot honestly model is returned with ``status="unmodeled"``
rather than guessed at — the deck builder surfaces those to the user.

The engineering-notation -> SPICE-value conversion lives in :func:`spice_value`,
where the notorious ``M`` collision is resolved: in the KiCad values this tool
consumes ``M`` means *mega* (a "1M" resistor is 1 MΩ), but in SPICE ``M`` means
*milli* and mega must be written ``MEG``. We parse with the mega meaning and
render the SPICE ``MEG`` so the two never get crossed.

:func:`fit_diode` turns datasheet forward-voltage points into a ``.model`` line.
The default is deliberately conservative: a single table-anchored point plus an
ideality prior beats a two-point fit off eyeballed curve coordinates, which the
live session proved can put IS off by 1000x.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from ..calc.si import parse_value
from ..errors import fail

__all__ = ["DeviceCard", "spice_value", "resolve", "fit_diode",
           "load_builtin", "builtin_names"]

# Thermal voltage at ~300 K (kT/q); matches the live-session BAT54H fit.
VT = 0.02585

_BUILTIN_LIB = Path(__file__).with_name("builtin.lib")


# --- DeviceCard (shared interface contract) ---------------------------------
@dataclass
class DeviceCard:
    """The resolved SPICE identity of one component.

    ``letter`` is the SPICE element letter (``R``/``C``/``L``/``D``/``Q``/``X``/
    ``B``, or ``""`` when nothing is emitted). ``value`` is a normalized,
    SPICE-safe value string (e.g. ``"1MEG"``). ``model_name`` names the
    ``.model``/``.subckt`` this element references; ``model_card`` carries the
    full block to inject when one is required. ``pin_order`` lists symbol pin
    numbers in SPICE terminal order (``None`` = use the schematic pin order).
    ``status`` is ``"ok"`` | ``"skip"`` | ``"unmodeled"``.
    """

    letter: str = ""
    value: str | None = None
    model_name: str | None = None
    model_card: str | None = None
    pin_order: list[str] | None = None
    status: str = "unmodeled"
    note: str = ""
    # True when a polarity-sensitive element (D/Q) is emitted in raw schematic
    # pin-NUMBER order because pin NAMES could not identify the SPICE terminals;
    # the deck surfaces it as a SIM_PIN_ORDER_ASSUMED warning.
    pin_order_assumed: bool = False


# --- spice_value: engineering notation -> SPICE-safe value ------------------
# SPICE magnitude suffixes.  NOTE the asymmetry that motivates this whole
# helper: SPICE's ``M`` is MILLI and mega is ``MEG``.  We therefore render mega
# as ``MEG`` and milli as ``m`` regardless of how the input spelled them.
_SPICE_STEPS: list[tuple[float, str]] = [
    (1e12, "T"), (1e9, "G"), (1e6, "MEG"), (1e3, "k"), (1.0, ""),
    (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p"), (1e-15, "f"),
]

# Trailing spelled-out unit words we strip before parsing (case-insensitive).
_UNIT_WORD_RX = re.compile(
    r"(?i)\s*(?:ohms?|Ω|farads?|henr(?:ies|y)|volts?|amp(?:ere)?s?"
    r"|watts?|hertz|seconds?)$"
)
# Single-letter farad/henry — stripped only when a digit or SI prefix precedes,
# so femto ``f`` (lowercase) survives and ``MEG`` is never mangled.
_UNIT_F_RX = re.compile(r"(?<=[-0-9.pnuµmkKMG])F$")
_UNIT_H_RX = re.compile(r"(?i)(?<=[-0-9.pnuµmkg])H$")


def _strip_units(text: str) -> str:
    """Remove a trailing unit token (``ohm``/``F``/``H``/…) from ``text``."""
    s = _UNIT_WORD_RX.sub("", text).strip()
    s = _UNIT_F_RX.sub("", s)
    s = _UNIT_H_RX.sub("", s)
    return s.strip()


def _render_spice(val: float) -> str:
    """Render a float as a compact SPICE value string (``1e6 -> '1MEG'``)."""
    if val == 0:
        return "0"
    a = abs(val)
    for scale, suffix in _SPICE_STEPS:
        if a >= scale:
            mant = val / scale
            return f"{mant:.6g}{suffix}"
    # Smaller than femto (or NaN): fall back to bare scientific notation.
    return f"{val:g}"


def spice_value(text: str) -> str:
    """Convert engineering-notation ``text`` to a SPICE-safe value string.

    Accepts the forms engineers type — ``4700``, ``4.7k``, ``100n``, ``2M2``,
    ``4R7``, ``1e-7`` — and any of those with a trailing unit (``4.7kohm``,
    ``100nF``, ``10uH``). ``M`` is read as *mega* (KiCad convention) and
    rendered as SPICE ``MEG``; milli renders ``m``, micro ``u``, and so on.
    Bare numbers (``"4700"``, ``"1e-7"``) and values that cannot be parsed pass
    through unchanged.
    """
    raw = str(text).strip()
    if not raw:
        return raw
    s = _strip_units(raw)
    if not s:
        return raw
    # Already a plain number -> pass the (unit-stripped) text through verbatim.
    try:
        float(s)
        return s
    except ValueError:
        pass
    # SPICE-native / KiCad "meg" spelling -> mega, explicitly (parse_value
    # only understands single-letter suffixes and would choke on "MEG").
    m = re.match(r"(?i)^([0-9]*\.?[0-9]+)\s*meg$", s)
    if m:
        return _render_spice(float(m.group(1)) * 1e6)
    try:
        val = parse_value(s)
    except Exception:
        return raw
    return _render_spice(val)


# --- resolution ladder ------------------------------------------------------
# Sim.Device token (case-insensitive) -> SPICE element letter.
_DEVICE_LETTER: dict[str, str] = {
    "R": "R", "RES": "R", "RESISTOR": "R",
    "C": "C", "CAP": "C", "CAPACITOR": "C",
    "L": "L", "IND": "L", "INDUCTOR": "L",
    "D": "D", "DIODE": "D",
    "Q": "Q", "NPN": "Q", "PNP": "Q", "BJT": "Q",
    "X": "X", "SUBCKT": "X", "SUBCIRCUIT": "X", "SPICE": "X", "XSPICE": "X",
    "V": "V", "VSOURCE": "V", "I": "I", "ISOURCE": "I", "B": "B",
}
_PASSIVE = {"R", "C", "L"}

# Designator prefixes that should be silently skipped (no SPICE element).
_SKIP_PREFIXES = frozenset({"J", "P", "CN", "CON", "TP", "MP", "MK",
                            "H", "FID", "MH", "LOGO"})

_UNMODELED_NOTE = ("give it Sim.* fields, a spec.models entry, or fit a diode "
                   "from the datasheet with "
                   "akcli.sim.models.fit_diode (see docs/sim.md)")

# --- semantic (polarity-sensitive) terminal ordering ------------------------
# SPICE node order for these elements is POSITIONAL and semantic (D anode
# cathode; Q collector base emitter), but KiCad's stock symbols number diode
# pins K=1/A=2, so trusting pin-NUMBER order silently reverses polarity. For
# each SPICE terminal we list the pin NAMES (case-insensitive) that identify it;
# ``resolve`` reorders by NAME when the names are present and unambiguous.
_SEMANTIC_PINS: dict[str, list[tuple[str, ...]]] = {
    "D": [("a", "anode", "+"), ("k", "cathode", "-")],
    "Q": [("c", "collector"), ("b", "base"), ("e", "emitter")],
}

# .model type token -> SPICE element letter, for inferring a device from a bare
# model_card when a spec.models entry omits 'device'.
_MODEL_TYPE_LETTER: dict[str, str] = {
    "D": "D", "NPN": "Q", "PNP": "Q", "LPNP": "Q",
    "NMOS": "M", "PMOS": "M", "VDMOS": "M",
    "NJF": "J", "PJF": "J", "R": "R", "C": "C", "L": "L",
    "SW": "S", "CSW": "W",
}
_MODEL_TYPE_RX = re.compile(r"(?i)^\s*\.model\s+\S+\s+([A-Za-z]+)")


def _prefix(designator: str) -> str:
    """Leading alphabetic run of a designator, upper-cased (``"R12" -> "R"``)."""
    m = re.match(r"[A-Za-z]+", designator or "")
    return m.group(0).upper() if m else ""


def _get_ci(params: dict, key: str) -> str | None:
    """Case-insensitive fetch from a ``{param: value}`` dict."""
    if not params:
        return None
    if key in params:
        return params[key]
    kl = key.lower()
    for k, v in params.items():
        if str(k).lower() == kl:
            return v
    return None


def _parse_sim_pins(text: str) -> list[str] | None:
    """Parse a KiCad ``Sim.Pins`` string into SPICE terminal order.

    ``Sim.Pins`` maps each SPICE terminal to a symbol pin, e.g. ``"1=2 2=1"``
    (model terminal 1 = symbol pin 2, model terminal 2 = symbol pin 1). We
    return the symbol pin numbers ordered by model terminal — the order SPICE
    expects them wired: ``["2", "1"]`` for that example.
    """
    if not text:
        return None
    pairs: list[tuple[int, str]] = []
    for tok in re.split(r"[\s,]+", text.strip()):
        if not tok:
            continue
        if "=" not in tok:
            return None
        term, sym = tok.split("=", 1)
        try:
            pairs.append((int(term.strip()), sym.strip()))
        except ValueError:
            return None
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[0])
    return [sym for _, sym in pairs]


def _model_card_for(model_name: str | None) -> str | None:
    """Return the builtin ``.subckt`` block when ``model_name`` names one."""
    if model_name and model_name.upper() in builtin_names():
        return load_builtin(model_name)
    return None


def _from_sim_fields(comp, params: dict) -> DeviceCard | None:
    """Ladder rung (a): build a card from ``Sim.*`` symbol fields, or None."""
    has_sim = any(str(k).lower().startswith("sim.") for k in params)
    if not has_sim:
        return None
    if str(_get_ci(params, "Sim.Enable") or "").strip() in ("0", "false", "no"):
        return DeviceCard(status="skip", note="Sim.Enable=0")

    device = (_get_ci(params, "Sim.Device") or "").strip()
    name = (_get_ci(params, "Sim.Name") or "").strip() or None
    sim_params = (_get_ci(params, "Sim.Params") or "").strip() or None
    pins = _parse_sim_pins(_get_ci(params, "Sim.Pins") or "")

    letter = _DEVICE_LETTER.get(device.upper()) if device else None
    if letter is None:
        # Sim.* present but no recognized device — honor a Sim.Name subckt.
        if name:
            letter = "X"
        else:
            return DeviceCard(status="unmodeled",
                              note=f"Sim.Device {device!r} not recognized; "
                                   + _UNMODELED_NOTE)

    card = DeviceCard(letter=letter, model_name=name, pin_order=pins,
                      status="ok", note="from Sim.* fields")
    if letter in _PASSIVE:
        raw = sim_params or comp.value
        card.value = spice_value(raw) if raw else None
    elif letter == "X":
        card.model_card = _model_card_for(name)
    elif letter in ("D", "Q") and name is None and sim_params:
        # A native Sim.Device=D/NPN/PNP carrying inline Sim.Params but no
        # Sim.Name — exactly what `fit-diode --apply` writes — has the model
        # parameters yet no model to point the element at. Synthesize a
        # deterministic model name + a matching .model card so the deck emits a
        # *modeled* element (D4 a k AKCLI_D4 + .model AKCLI_D4 D(...)) instead of
        # a bare, unparseable one that ngspice rejects with 'circuit not parsed'.
        model_name = f"AKCLI_{comp.designator}"
        card.model_name = model_name
        card.model_card = _synth_device_model(letter, device, model_name,
                                              sim_params)
    return card


def _synth_device_model(letter: str, device: str, name: str,
                        params: str) -> str:
    """Synthesize a ``.model`` card for a native ``Sim.Device`` + ``Sim.Params``.

    ``letter`` is ``D`` or ``Q``; ``device`` is the raw ``Sim.Device`` token
    (used to choose ``NPN``/``PNP`` for a BJT; a bare ``Q``/``BJT`` defaults to
    ``NPN``). ``params`` is the verbatim ``Sim.Params`` string, e.g.
    ``"IS=1.5843e-08 N=1.0500"``.
    """
    if letter == "D":
        mtype = "D"
    else:
        mtype = "PNP" if (device or "").strip().upper() == "PNP" else "NPN"
    return f".model {name} {mtype}({params.strip()})"


def _infer_letter_from_card(model_card: object, model_name: object) -> str:
    """Infer a SPICE element letter from a model_card / subckt name, or ``""``.

    ``.model <n> D(...)`` -> ``D``; ``.subckt`` (or a ``model_name`` that names a
    builtin subckt) -> ``X``; other ``.model`` types map through their SPICE
    type token (``NPN`` -> ``Q``, ``NMOS`` -> ``M``, ...).
    """
    if model_name and str(model_name).upper() in builtin_names():
        return "X"
    if model_card:
        text = str(model_card)
        if text.lstrip().lower().startswith(".subckt"):
            return "X"
        m = _MODEL_TYPE_RX.match(text)
        if m:
            return _MODEL_TYPE_LETTER.get(m.group(1).upper(), "")
    return ""


def _from_spec_models(comp, entry: dict) -> DeviceCard:
    """Ladder rung (b): build a card from a ``spec.models`` override entry."""
    if entry.get("skip"):
        return DeviceCard(status="skip", note="spec.models skip")
    device = str(entry.get("device", "")).strip()
    name = entry.get("model_name") or entry.get("name")
    model_card = entry.get("model_card")
    letter = _DEVICE_LETTER.get(device.upper(), "")
    if not letter:
        # No/unknown 'device': infer from the model_card (or a subckt name)
        # rather than emit a bare designator that ngspice mis-parses by its
        # first letter — a wrong-device silent-garbage trap.
        letter = _infer_letter_from_card(model_card, name)
        if not letter:
            fail("BAD_CONFIG",
                 f"spec.models entry for {comp.designator!r} "
                 f"(model_name={name!r}): missing/unknown 'device' and its SPICE "
                 f"type could not be inferred from model_card; add a 'device' "
                 f"field (R/C/L/D/Q/X/...)")
    card = DeviceCard(letter=letter, model_name=name, status="ok",
                      note="from spec.models")
    card.model_card = entry.get("model_card") or _model_card_for(name)
    po = entry.get("pin_order")
    if po is not None:
        card.pin_order = [str(p) for p in po]
    if letter in _PASSIVE:
        raw = entry.get("params") or entry.get("value") or comp.value
        card.value = spice_value(raw) if raw else None
    return card


def _from_heuristic(comp) -> DeviceCard:
    """Ladder rung (c): prefix + value heuristic; honest ``unmodeled``/``skip``."""
    prefix = _prefix(comp.designator)
    if prefix in _SKIP_PREFIXES or comp.designator.startswith(("#", "$")):
        return DeviceCard(status="skip",
                          note=f"{prefix or comp.designator} is not a simulated "
                               "device (connector/mechanical/test-point)")
    if prefix in _PASSIVE:
        if not comp.value:
            return DeviceCard(status="unmodeled",
                              note=f"{prefix}{comp.designator[1:] or ''} has no "
                                   "value to model; " + _UNMODELED_NOTE)
        val = spice_value(comp.value)
        # A value we could not normalize (still carries letters) is not trusted.
        if val == str(comp.value).strip() and re.search(r"[A-Za-z]{2,}", val):
            return DeviceCard(status="unmodeled",
                              note=f"cannot parse value {comp.value!r}; "
                                   + _UNMODELED_NOTE)
        return DeviceCard(letter=prefix, value=val, status="ok",
                          note="passive from designator prefix + value")
    # A diode, transistor, IC, etc. NEEDS a model — never invent one silently.
    return DeviceCard(status="unmodeled",
                      note=f"{prefix or 'device'} needs an explicit model; "
                           + _UNMODELED_NOTE)


def _semantic_pin_order(comp, letter: str) -> tuple[list[str] | None, bool]:
    """Symbol pin numbers in SPICE terminal order, derived from pin NAMES.

    Returns ``(order, True)`` when every SPICE terminal of ``letter`` maps to
    exactly one uniquely-named pin (e.g. a diode with pins named ``A``/``K`` ->
    ``[anode, cathode]``). Returns ``(None, True)`` — *assumed* — when the names
    are missing or ambiguous, so the caller keeps schematic pin-number order but
    flags it. ``(None, False)`` means ``letter`` has no semantic ordering.
    """
    spec = _SEMANTIC_PINS.get(letter)
    if not spec:
        return None, False
    pins = list(getattr(comp, "pins", None) or [])
    if len(pins) != len(spec):
        return None, True
    by_name: dict[str, list[str]] = {}
    for p in pins:
        by_name.setdefault((p.name or "").strip().lower(), []).append(p.number)
    order: list[str] = []
    for aliases in spec:
        matches: list[str] = []
        for alias in aliases:
            matches.extend(by_name.get(alias, []))
        matches = list(dict.fromkeys(matches))
        if len(matches) != 1:
            return None, True
        order.append(matches[0])
    if len(set(order)) != len(order):
        return None, True
    return order, True


def _apply_semantic_pin_order(comp, card: DeviceCard) -> None:
    """Fill a polarity-sensitive card's pin_order from pin NAMES (in place).

    Only acts on emitted ``D``/``Q`` cards that did not already receive an
    explicit ``pin_order`` (Sim.Pins or spec.models). When the names cannot
    identify the terminals, marks ``pin_order_assumed`` so the deck warns.
    """
    if getattr(card, "status", "") != "ok":
        return
    if card.letter not in _SEMANTIC_PINS or card.pin_order is not None:
        return
    order, semantic = _semantic_pin_order(comp, card.letter)
    if not semantic:
        return
    if order is not None:
        card.pin_order = order
    else:
        card.pin_order_assumed = True


def resolve(comp, spec) -> DeviceCard:
    """Resolve one component to a :class:`DeviceCard` (first hit wins).

    Ladder: ``Sim.*`` symbol fields, then a ``spec.models`` override keyed by
    designator or lib_id, then a prefix/value heuristic. ``spec`` may be a
    ``SimSpec`` (with a ``.models`` dict), a plain dict, or ``None``.

    For emitted diodes/transistors whose terminal order was not given
    explicitly, pin NAMES (``A``/``K``, ``C``/``B``/``E``) are used to derive the
    semantic SPICE node order — KiCad numbers stock diode pins K=1/A=2, so
    trusting pin numbers would silently invert polarity.
    """
    params = getattr(comp, "parameters", None) or {}

    card = _from_sim_fields(comp, params)
    if card is None:
        models = _spec_models(spec)
        entry = None
        if models:
            entry = models.get(comp.designator)
            if entry is None and comp.library_ref is not None:
                entry = models.get(comp.library_ref)
        if isinstance(entry, dict):
            card = _from_spec_models(comp, entry)
        else:
            card = _from_heuristic(comp)

    _apply_semantic_pin_order(comp, card)
    return card


def _spec_models(spec) -> dict:
    """Extract the ``{key: entry}`` model overrides from ``spec`` (any shape)."""
    if spec is None:
        return {}
    models = getattr(spec, "models", None)
    if models is None and isinstance(spec, dict):
        models = spec.get("models")
    return models or {}


# --- fit_diode: datasheet forward-voltage points -> .model ------------------
def fit_diode(
    points: list[tuple[float, float]],
    n_prior: float | None = 1.05,
    rs_point: tuple[float, float] | None = None,
    cjo: float | None = None,
) -> dict:
    """Fit a Shockley diode ``.model`` from datasheet (V_F, I_F) points.

    ``points`` are ``(forward_voltage, forward_current)`` pairs. With a single
    point the ideality ``n_prior`` anchors the fit and IS is solved directly —
    the honest default, because a two-point fit off eyeballed curve coordinates
    routinely lands IS 1000x wrong. With two or more points, ``N`` and ``IS``
    are least-squares fit in log space with ``N`` clamped to ``[0.9, 2.5]``; a
    ``note`` warns when the fitted ``N`` disagrees with ``n_prior`` by >30%.

    ``rs_point`` (a high-current ``(V, I)`` point) solves the series resistance
    ``RS``. ``cjo`` sets the junction capacitance. Returns a dict with keys
    ``IS``, ``N``, ``RS``, ``CJO``, ``model_card`` and ``note``.
    """
    if not points:
        raise ValueError("fit_diode needs at least one (V, I) point")

    # A forward (V_F, I_F) point is physically positive on both axes. Without
    # this guard a typo'd sign (0.3@-1m -> negative IS), a V/I swap (1m@0.3 ->
    # IS=0.29 A) or a negated voltage silently produces a garbage .model card
    # that --apply would happily write onto the schematic. rs_point is validated
    # the same way below; the primary points must be too.
    for v1, i1 in points:
        if not (math.isfinite(v1) and v1 > 0):
            fail("BAD_CONFIG",
                 f"fit_diode: point forward voltage must be a positive finite "
                 f"number, got {v1!r} (a (V_F, I_F) pair, e.g. 0.3@1m)")
        if not (math.isfinite(i1) and i1 > 0):
            fail("BAD_CONFIG",
                 f"fit_diode: point forward current must be a positive finite "
                 f"number, got {i1!r} (a (V_F, I_F) pair, e.g. 0.3@1m)")

    note = ""
    if len(points) == 1:
        n = 1.05 if n_prior is None else float(n_prior)
        if not math.isfinite(n) or n <= 0:
            fail("BAD_CONFIG",
                 f"fit_diode: n_prior must be a positive finite number, "
                 f"got {n_prior!r}")
        if n < 0.9 or n > 2.5:
            clamped = min(2.5, max(0.9, n))
            note = (f"n_prior={n:.3f} clamped to {clamped:.3f} "
                    "(physical diode ideality is ~0.9-2.5)")
            n = clamped
        v1, i1 = points[0]
        is_sat = i1 / math.exp(v1 / (n * VT))
    else:
        # Least squares: ln(I) = ln(IS) + V / (N*VT); slope = 1/(N*VT).
        xs = [float(v) for v, _ in points]
        ys = [math.log(float(i)) for _, i in points]
        nseg = len(xs)
        mx = sum(xs) / nseg
        my = sum(ys) / nseg
        sxx = sum((x - mx) ** 2 for x in xs)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        slope = sxy / sxx if sxx else 0.0
        n_fit = 1.0 / (slope * VT) if slope > 0 else (n_prior or 1.05)
        n = min(2.5, max(0.9, n_fit))
        intercept = my - slope * mx
        is_sat = math.exp(intercept)
        if n != n_fit:
            note = (f"fitted N={n_fit:.3f} clamped to {n:.3f}; "
                    "curve points look eyeballed — prefer a table-anchored "
                    "single point with N prior")
        elif n_prior and abs(n_fit - n_prior) / n_prior > 0.30:
            note = (f"2-point fit N={n_fit:.3f} disagrees with prior "
                    f"N={n_prior:.3f} by >30%; curve points may be eyeballed — "
                    "a table-anchored single point + prior is more reliable")

    rs = 0.0
    if rs_point is not None:
        vhi, ihi = rs_point
        if not (math.isfinite(vhi) and vhi > 0):
            fail("BAD_CONFIG",
                 f"fit_diode: rs_point voltage must be positive, got {vhi!r}")
        if not (math.isfinite(ihi) and ihi > 0):
            fail("BAD_CONFIG",
                 f"fit_diode: rs_point current must be positive, got {ihi!r}")
        rs = max(0.0, (vhi - n * VT * math.log(ihi / is_sat)) / ihi)

    result: dict = {"IS": is_sat, "N": n, "RS": rs, "CJO": cjo,
                    "note": note}
    result["model_card"] = _render_diode_card(is_sat, n, rs, cjo)
    return result


def _render_diode_card(is_sat: float, n: float, rs: float,
                       cjo: float | None, name: str = "DFIT") -> str:
    """Render a Shockley ``.model`` line from fitted parameters."""
    parts = [f"IS={is_sat:.4e}", f"N={n:.4f}"]
    if rs:
        parts.append(f"RS={rs:.4g}")
    if cjo is not None:
        parts.append(f"CJO={spice_value(str(cjo))}")
    return f".model {name} D({' '.join(parts)})"


# --- builtin behavioral library --------------------------------------------
_BUILTIN_CACHE: dict[str, str] | None = None


def _load_builtin_blocks() -> dict[str, str]:
    """Parse ``builtin.lib`` into ``{UPPER_NAME: subckt-block-text}`` (cached)."""
    global _BUILTIN_CACHE
    if _BUILTIN_CACHE is not None:
        return _BUILTIN_CACHE
    blocks: dict[str, str] = {}
    text = _BUILTIN_LIB.read_text(encoding="ascii")
    cur_name: str | None = None
    cur_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith(".subckt"):
            toks = stripped.split()
            cur_name = toks[1] if len(toks) > 1 else None
            cur_lines = [line]
        elif cur_name is not None:
            cur_lines.append(line)
            if low.startswith(".ends"):
                blocks[cur_name.upper()] = "\n".join(cur_lines)
                cur_name = None
                cur_lines = []
    _BUILTIN_CACHE = blocks
    return blocks


def builtin_names() -> frozenset[str]:
    """Upper-cased names of the subcircuits defined in ``builtin.lib``."""
    return frozenset(_load_builtin_blocks())


def load_builtin(name: str) -> str | None:
    """Return the ``.subckt`` block text for ``name`` (case-insensitive)."""
    return _load_builtin_blocks().get(str(name).upper())
