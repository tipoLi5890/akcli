"""Power-rail trace-width observation: IPC-2221 ampacity of the thinnest
segment.

Pure geometry + the IPC-2221 formula (the inverse of ``akcli calc
trackwidth``, whose envelope rides in the evidence as the round-trip
oracle). Copper weight and temperature rise are assumptions and say so; the
finding stays an INFO observation because the rail's real current is not on
the board — the comparison against a known load lands with the power-tree
milestone.
"""

from __future__ import annotations

from ....classify import implied_voltage, is_ground as _is_ground, is_power as _is_power
from ....report import Finding, Severity, anchor
from ... import Detector, Rule, register
from ... import geometry
from ...tables import TRACE_COPPER_OZ, TRACE_DTEMP_C

RULES = (
    Rule(
        code="REVIEW_TRACE_WIDTH",
        title="Power rail's thinnest track and its IPC-2221 ampacity",
        explain=(
            "For every power-named net with copper, the thinnest segment "
            "width and its continuous-current capacity per IPC-2221 "
            "(I = k·ΔT^0.44·A^0.725), assuming "
            f"{TRACE_COPPER_OZ:g} oz copper and ΔT = {TRACE_DTEMP_C:g} °C "
            "on an external layer — both stated in the evidence. An "
            "observation for the reviewer to hold against the rail's real "
            "load; the automated comparison arrives with the power tree."),
        default_severity="info", confidence="deterministic", version="1",
        reference="IPC-2221 (via `akcli calc trackwidth`)"),
)


def _is_power_net(name: str | None) -> bool:
    return bool(name) and not _is_ground(name) and (
        _is_power(name) or implied_voltage(name) is not None)


def run(ctx) -> list[Finding]:
    from ....calc import compute

    pcb = ctx.pcb
    if pcb is None:
        return []
    out: list[Finding] = []
    for net in sorted(n for n in getattr(pcb, "nets", []) or []
                      if _is_power_net(n)):
        width = geometry.min_track_width_mm(pcb, net)
        if width is None or width <= 0:
            continue
        amps = geometry.ipc2221_ampacity_a(
            width, dtemp_c=TRACE_DTEMP_C,
            thickness_mm=0.035 * TRACE_COPPER_OZ)
        # round-trip oracle: the calc's width for this current ≈ our width
        env = compute("trackwidth", {"i": round(amps, 4),
                                     "dtemp": TRACE_DTEMP_C})
        out.append(Finding(
            code="REVIEW_TRACE_WIDTH", severity=Severity.INFO,
            message=(f"rail {net!r}: thinnest track {width:.3g} mm carries "
                     f"≈{amps:.2g} A continuous (IPC-2221, "
                     f"{TRACE_COPPER_OZ:g} oz, ΔT {TRACE_DTEMP_C:g} °C)"),
            refs=[net], anchors=[anchor("net", net)],
            confidence="deterministic",
            evidence={"source": "calc", "calc": env,
                      "assumptions": [
                          f"{TRACE_COPPER_OZ:g} oz copper (35 µm)",
                          f"ΔT = {TRACE_DTEMP_C:g} °C, external layer",
                      ]},
            remediation=("hold this against the rail's real load current; "
                         "widen the segment if the margin is thin")))
    return out


register(Detector(name="pcb.trace_width", family="pcb", run=run, rules=RULES))
