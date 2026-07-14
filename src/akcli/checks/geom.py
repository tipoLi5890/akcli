"""Wire/pin/label ATTACHMENT lint for KiCad schematics (``akcli check --nets``).

The classic "looks connected, isn't" traps are all geometric: a pin tip resting
mid-span on a wire (eeschema connects a pin only at a wire END or at a
junction — a bare touch is an open circuit), a label whose anchor floats next
to — not on — the thing it was meant to name, and a wire that corners exactly
on a pin tip (which DOES connect, to both legs, sometimes shorting two
intended nets). ERC sees the resulting netlist, not the near-miss geometry, so
it reports the *symptom* (dangling net) without the fixable cause.

Every test here is built from the SAME quantized-integer helpers ``netbuild``
uses (``_q`` / ``_on_seg``), so a finding can never disagree with what the net
engine actually inferred: NET_PIN_MIDSPAN_TOUCH fires exactly when netbuild
step 4a refuses the connection.

Scope: the root ``.kicad_sch`` only (same as the layout lint). All findings
are advisory — they never gate a write.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .. import netbuild
from ..model import NetPrimitives
from ..readers import kicad as _krd
from ..report import Finding, Severity, anchor

NET_PIN_MIDSPAN_TOUCH = "NET_PIN_MIDSPAN_TOUCH"    # pin tip mid-span, no junction
NET_LABEL_UNATTACHED = "NET_LABEL_UNATTACHED"      # label anchor on no pin, no wire
NET_WIRE_CORNER_ON_PIN = "NET_WIRE_CORNER_ON_PIN"  # wire corners exactly on a pin tip


def _fmt(pt: tuple[float, float]) -> str:
    def r(v: float) -> str:
        iv = round(v)
        return str(int(iv)) if abs(v - iv) < 0.01 else f"{v:.1f}"
    return f"({r(pt[0])},{r(pt[1])})"


def _load_prims(path: Path) -> NetPrimitives:
    """Raw primitives via the reader's public loader (fallback: same builder)."""
    reader = getattr(_krd, "read_primitives", None)
    if reader is not None:
        return reader(path)
    root = _krd._parse_root(path, "kicad_sch")
    _, prims = _krd._build(root)
    return prims


def run(path: str | Path) -> list[Finding]:
    """Lint one ``.kicad_sch`` for attachment near-misses; returns findings."""
    p = Path(path)
    if p.suffix.lower() != ".kicad_sch":
        return [Finding(
            NET_PIN_MIDSPAN_TOUCH, Severity.INFO,
            "wire-attachment lint supports .kicad_sch only; skipped",
            refs=[str(p)],
        )]
    return run_prims(_load_prims(p))


def run_prims(prims: NetPrimitives) -> list[Finding]:
    """Run the attachment checks over raw :class:`model.NetPrimitives`."""
    q = netbuild._q          # netbuild's own quantizer / point-on-segment —
    on_seg = netbuild._on_seg  # shared so the lint mirrors the engine exactly

    segs: dict[str, list[tuple[tuple[int, int], tuple[int, int]]]] = defaultdict(list)
    for w in prims.wires:
        segs[w.sheet].append((q(w.a), q(w.b)))
    junctions: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for j in prims.junctions:
        junctions[j.sheet].add(q(j.at))
    pin_tips: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for ph in prims.pins:
        pin_tips[ph.sheet].add(q(ph.at))

    findings: list[Finding] = []

    # (a) pin tip mid-span on a wire with no junction -> NOT connected
    # (netbuild step 4a: a pin joins at a segment ENDPOINT or a junction-marked
    # point only). An endpoint touch anywhere exempts the pin: the T-junction
    # rule then unions the crossing wires, so the pin is genuinely on the net.
    for ph in prims.pins:
        qp = q(ph.at)
        sheet_segs = segs.get(ph.sheet, [])
        if qp in junctions.get(ph.sheet, ()):
            continue
        if any(qp in (qa, qb) for qa, qb in sheet_segs):
            continue
        if any(on_seg(qp, qa, qb) for qa, qb in sheet_segs):
            des, num = ph.ref
            findings.append(Finding(
                NET_PIN_MIDSPAN_TOUCH, Severity.WARNING,
                f"{des} pin {num} tip at {_fmt(ph.at)} touches a wire mid-span "
                "with no junction — NOT connected (a pin joins only at a wire "
                "end or a junction); add a junction there or end the wire on "
                "the pin",
                refs=[f"{des}.{num}"],
                pos=ph.at,
                anchors=[anchor("pin", f"{des}.{num}", ph.at)],
            ))

    # (b) label anchored on nothing: no pin tip, no wire -> it names nothing.
    # "hier" labels are synthetic sheet-pin connectors, never user-visible.
    seen: set[tuple] = set()
    for lb in prims.labels:
        if lb.scope == "hier" or not lb.text:
            continue
        ql = q(lb.at)
        key = (lb.sheet, ql, lb.text)
        if key in seen:
            continue
        seen.add(key)
        if ql in pin_tips.get(lb.sheet, ()):
            continue
        if any(on_seg(ql, qa, qb) for qa, qb in segs.get(lb.sheet, [])):
            continue
        findings.append(Finding(
            NET_LABEL_UNATTACHED, Severity.WARNING,
            f"label '{lb.text}' at {_fmt(lb.at)} coincides with no pin tip and "
            "lies on no wire — it names nothing; anchor it on a pin tip or a "
            "wire",
            refs=[lb.text],
            pos=lb.at,
            anchors=[anchor("label", lb.text, lb.at)],
        ))

    # (c) wire cornering exactly on a pin tip: this DOES connect (both segment
    # endpoints land on the pin), joining the pin to BOTH legs — fine when
    # intended, a silent short when the corner just happened to land there.
    for ph in prims.pins:
        qp = q(ph.at)
        dirs: list[tuple[int, int]] = []
        for qa, qb in segs.get(ph.sheet, []):
            if qp == qa:
                dirs.append((qb[0] - qa[0], qb[1] - qa[1]))
            elif qp == qb:
                dirs.append((qa[0] - qb[0], qa[1] - qb[1]))
        corner = any(
            d1[0] * d2[1] - d1[1] * d2[0] != 0
            for i, d1 in enumerate(dirs) for d2 in dirs[i + 1:]
        )
        if corner:
            des, num = ph.ref
            findings.append(Finding(
                NET_WIRE_CORNER_ON_PIN, Severity.NOTE,
                f"a wire corners exactly on {des} pin {num} tip at "
                f"{_fmt(ph.at)} — the pin joins BOTH legs of the corner; if "
                "that is unintended, move the corner off the pin",
                refs=[f"{des}.{num}"],
                pos=ph.at,
                anchors=[anchor("pin", f"{des}.{num}", ph.at)],
            ))

    return findings
