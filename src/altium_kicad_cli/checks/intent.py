"""Design-intent assertions: assert the netlist the designer MEANT.

In a real design session the same hand-rolled question is re-asked after every
edit: "is U1.4 still on SWCLK, and did nothing short SWCLK into SWDIO?". This
module makes that a first-class, file-driven check.

Intent file (JSON)::

    {"protocol_version": 1,
     "mode": "exact",                    # or "subset"; default "exact"
     "nets": {"SWCLK": ["U1.4", "J2.2"], ...}}

Member strings are ``"REF.PIN"`` split on the FIRST dot — designators never
contain dots, pin numbers may (``"U1.P0.25"`` parses as pin ``P0.25``).

``run(sch, spec)`` matches each intent net onto the actual net (``sch.nets``,
the shared netbuild output) containing the MOST of its listed pins — never by
display name, because auto-names churn and designers rename freely. It reports:

* ``INTENT_PIN_UNKNOWN``    (ERROR) — a listed REF.PIN does not exist in the schematic
* ``INTENT_NET_NOT_FOUND``  (ERROR) — no actual net contains any listed pin
* ``INTENT_MISSING_MEMBER`` (ERROR) — an intent pin is absent from the matched net
* ``INTENT_EXTRA_MEMBER``   (ERROR, exact mode only) — the matched net carries
  pins the intent omits; ``subset`` mode asserts containment and skips this
* ``INTENT_NETS_SHORTED``   (ERROR) — two intent nets resolve to the SAME actual net

``snapshot(sch)`` emits a valid intent document from the current netlist (named
nets only by default), enabling the snapshot -> edit -> assert workflow.

``load`` raises ``BAD_CONFIG`` naming the offending entry for shape errors and
``PROTOCOL_MISMATCH`` for a wrong ``protocol_version`` (mirrors ops.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import AkcliError, fail
from ..model import Net, PinRef, Schematic
from ..report import Finding, Severity

PROTOCOL_VERSION = 1
MODES = ("exact", "subset")

INTENT_PIN_UNKNOWN = "INTENT_PIN_UNKNOWN"
INTENT_NET_NOT_FOUND = "INTENT_NET_NOT_FOUND"
INTENT_MISSING_MEMBER = "INTENT_MISSING_MEMBER"
INTENT_EXTRA_MEMBER = "INTENT_EXTRA_MEMBER"
INTENT_NETS_SHORTED = "INTENT_NETS_SHORTED"

_TOP_KEYS = frozenset({"protocol_version", "mode", "nets"})


@dataclass
class IntentSpec:
    """A validated intent document: net name -> the pins it must contain."""

    nets: dict[str, list[PinRef]] = field(default_factory=dict)
    mode: str = "exact"
    protocol_version: int = PROTOCOL_VERSION


# --------------------------------------------------------------------------- #
# load / validate
# --------------------------------------------------------------------------- #
def _parse_member(net_name: str, raw: object, where: str) -> PinRef:
    if not isinstance(raw, str):
        fail("BAD_CONFIG",
             f"{where}: net '{net_name}': member {raw!r} must be a string "
             "'REF.PIN' (e.g. 'U1.2')")
    token = raw.strip()
    ref, dot, pin = token.partition(".")
    if not dot or not ref or not pin:
        fail("BAD_CONFIG",
             f"{where}: net '{net_name}': member {raw!r} must be 'REF.PIN' "
             "(e.g. 'U1.2')")
    return (ref, pin)


def load(path: str | Path) -> IntentSpec:
    """Load and validate an intent JSON file (see module docstring for shape).

    Raises ``AkcliError('BAD_CONFIG', ...)`` naming the offending entry, or
    ``AkcliError('PROTOCOL_MISMATCH', ...)`` on a wrong ``protocol_version``.
    ``FileNotFoundError`` propagates (the CLI maps it to exit 4).
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AkcliError("BAD_CONFIG", f"invalid intent JSON in {p}: {exc}") from exc

    where = str(p)
    if not isinstance(doc, dict):
        fail("BAD_CONFIG", f"{where}: intent root must be a JSON object")
    extra = set(doc) - _TOP_KEYS
    if extra:
        fail("BAD_CONFIG",
             f"{where}: unknown key(s): {', '.join(sorted(map(str, extra)))} "
             f"(expected {', '.join(sorted(_TOP_KEYS))})")

    pv = doc.get("protocol_version")
    if pv != PROTOCOL_VERSION:
        fail("PROTOCOL_MISMATCH",
             f"{where}: intent protocol_version {pv!r} != {PROTOCOL_VERSION}")

    mode = doc.get("mode", "exact")
    if mode not in MODES:
        fail("BAD_CONFIG",
             f"{where}: mode {mode!r} must be one of {', '.join(MODES)}")

    raw_nets = doc.get("nets")
    if not isinstance(raw_nets, dict):
        fail("BAD_CONFIG",
             f"{where}: 'nets' must be an object of "
             '{"NAME": ["REF.PIN", ...], ...}')

    nets: dict[str, list[PinRef]] = {}
    for name, members in raw_nets.items():
        if not str(name).strip():
            fail("BAD_CONFIG", f"{where}: net names must be non-empty")
        if not isinstance(members, list) or not members:
            fail("BAD_CONFIG",
                 f"{where}: net '{name}': members must be a non-empty array "
                 'of "REF.PIN" strings')
        seen: list[PinRef] = []
        for raw in members:
            ref = _parse_member(str(name), raw, where)
            if ref not in seen:  # silently dedupe repeats within one net
                seen.append(ref)
        nets[str(name)] = seen

    return IntentSpec(nets=nets, mode=mode, protocol_version=pv)


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def _net_label(net: Net) -> str:
    return net.name if net.name else f"<unnamed {net.stable_id}>"


def _name_set(net: Net) -> set[str]:
    """Case-folded set of every name a net answers to (name + aliases + sources)."""
    names = [net.name, *net.aliases, *net.source_names]
    return {n.strip().upper() for n in names if n}


def _refs(members) -> list[str]:
    return [f"{d}.{p}" for d, p in members]


def run(sch: Schematic, spec: IntentSpec) -> list[Finding]:
    """Assert ``spec`` against the schematic's built netlist. Pure, deterministic.

    Returns one aggregated finding per intent net per problem class (refs carry
    the individual pins), plus one finding per shorted intent-net group.
    """
    findings: list[Finding] = []

    known_pins: set[PinRef] = {
        (c.designator, p.number) for c in sch.components for p in c.pins
    }
    pin_to_net: dict[PinRef, int] = {}
    for i, net in enumerate(sch.nets):
        for m in net.members:
            pin_to_net[tuple(m)] = i

    resolved: dict[str, int] = {}  # intent net name -> matched sch.nets index
    for name in sorted(spec.nets):
        want = spec.nets[name]

        unknown = sorted(p for p in want if p not in known_pins)
        if unknown:
            findings.append(Finding(
                INTENT_PIN_UNKNOWN, Severity.ERROR,
                f"intent net '{name}': pin(s) not in schematic: "
                + ", ".join(_refs(unknown)),
                refs=_refs(unknown),
            ))

        # Best-containing actual net: most listed pins, then (tie-break) a net
        # already answering to the intent name, then the stable sch.nets order.
        hits: dict[int, int] = {}
        for pr in want:
            idx = pin_to_net.get(pr)
            if idx is not None:
                hits[idx] = hits.get(idx, 0) + 1
        if not hits:
            findings.append(Finding(
                INTENT_NET_NOT_FOUND, Severity.ERROR,
                f"intent net '{name}': no actual net contains any of its "
                f"pin(s): {', '.join(_refs(want))}",
                refs=_refs(want),
            ))
            continue
        best = min(
            hits,
            key=lambda i: (
                -hits[i],
                0 if name.strip().upper() in _name_set(sch.nets[i]) else 1,
                i,
            ),
        )
        resolved[name] = best
        actual = sch.nets[best]
        label = _net_label(actual)
        actual_members = {tuple(m) for m in actual.members}

        missing = sorted(p for p in want if p not in actual_members and p not in unknown)
        if missing:
            findings.append(Finding(
                INTENT_MISSING_MEMBER, Severity.ERROR,
                f"intent net '{name}' (matched actual net '{label}'): "
                f"missing member(s): {', '.join(_refs(missing))}",
                refs=_refs(missing),
            ))

        if spec.mode == "exact":
            extra = sorted(actual_members - set(want))
            if extra:
                findings.append(Finding(
                    INTENT_EXTRA_MEMBER, Severity.ERROR,
                    f"intent net '{name}' (matched actual net '{label}'): "
                    f"extra member(s) not in intent: {', '.join(_refs(extra))}",
                    refs=_refs(extra),
                ))

    # Two intent nets landing on ONE actual net is a short against intent.
    by_actual: dict[int, list[str]] = {}
    for name, idx in resolved.items():
        by_actual.setdefault(idx, []).append(name)
    for idx in sorted(by_actual):
        names = sorted(by_actual[idx])
        if len(names) < 2:
            continue
        quoted = ", ".join(f"'{n}'" for n in names)
        findings.append(Finding(
            INTENT_NETS_SHORTED, Severity.ERROR,
            f"intent nets {quoted} resolve to the same actual net "
            f"'{_net_label(sch.nets[idx])}' — shorted against intent",
            refs=names,
        ))

    return findings


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #
def snapshot(sch: Schematic, include_unnamed: bool = False) -> dict:
    """Emit a valid intent document from the current netlist.

    Named nets only by default (auto-named nets churn on every edit);
    ``include_unnamed=True`` adds them keyed by their membership-stable
    ``stable_id``. A duplicate display name (e.g. the same local label on two
    sheets) is disambiguated as ``NAME@stable_id`` — matching is by membership,
    so the key is only a report label. ``snapshot -> run`` yields no findings.
    """
    nets: dict[str, list[str]] = {}
    for net in sch.nets:
        named = bool(net.is_named and net.name)
        if not named and not include_unnamed:
            continue
        key = net.name if named else net.stable_id
        if key in nets:
            key = f"{key}@{net.stable_id}"
        nets[key] = _refs(net.members)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "mode": "exact",
        "nets": {k: nets[k] for k in sorted(nets)},
    }
