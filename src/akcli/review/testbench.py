"""Auto-generated subcircuit SPICE testbenches from review findings (M7 backlog).

Closes the review → sim loop: a quantitative review finding (an RC corner, a
divider ratio) becomes a **runnable, self-contained testbench** — the relevant
subcircuit is cut out of the schematic (net cone around the finding's
components), stimuli and pass/fail bounds are synthesized from the finding's
own calc evidence, and ngspice delivers the verdict. The claim "fc = 15.9 kHz"
upgrades from *computed* to *simulated*.

Honesty rules (the same discipline as the rest of the review layer):

* A finding whose topology cannot be re-derived from the live schematic is
  **skipped with a reason**, never guessed at.
* The prediction is recomputed here from the schematic's component values —
  never copied blindly from the finding text — so a stale findings file
  against an edited schematic fails loudly instead of "passing".
* The extracted cone contains ONLY the finding's components: a testbench
  verdict is about the local network, and says so (``note``).

Generators (by finding code):

* ``REVIEW_RC_CUTOFF`` → AC sweep over the R/C cone, assert the −3 dB
  crossing lands on 1/(2πRC) (±8 % — meas interpolation + sweep grid).
* ``REVIEW_FB_DIVIDER`` / ``REVIEW_FB_DIVIDER_VREF_MISMATCH`` /
  ``REVIEW_DIVIDER_TAP_MISMATCH`` → drive the divider's top rail, assert the
  tap voltage equals Vtop·Rb/(Rt+Rb) (±2 %).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model import Net, Schematic
from ..sim.assertions import SimSpec
from . import topo

TESTBENCH_VERSION = "1.0"

_RC_TOL = 0.08       # meas WHEN interpolation on a dec-20 grid
_DIVIDER_TOL = 0.02  # exact algebra; slack only for solver residue


@dataclass
class Testbench:
    """One runnable subcircuit bench derived from one finding."""

    fingerprint: str
    finding_code: str
    kind: str                    # "rc_lowpass" | "divider_dc"
    refs: list[str]
    gnd: str                     # net treated as SPICE node 0
    schematic: Schematic         # the extracted cone
    spec: SimSpec
    expect: dict = field(default_factory=dict)   # name -> {value, tol}
    note: str = ""

    def describe(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "finding_code": self.finding_code,
            "kind": self.kind,
            "refs": list(self.refs),
            "gnd": self.gnd,
            "expect": dict(self.expect),
            "note": self.note,
        }


def _cone(sch: Schematic, refs: set[str], label: str) -> Schematic:
    """The sub-schematic containing only ``refs`` (nets keep their names)."""
    comps = [c for c in sch.components if c.designator in refs]
    nets: list[Net] = []
    for net in sch.nets:
        members = sorted(m for m in net.members if m[0] in refs)
        if members:
            nets.append(Net(name=net.name, members=members,
                            aliases=list(net.aliases),
                            source_names=list(net.source_names),
                            is_named=net.is_named))
    return Schematic(source_path=f"<testbench:{label}>",
                     source_format=sch.source_format,
                     components=comps, nets=nets)


def _net_display(net: Net | None) -> str | None:
    return net.name if net is not None and net.name else None


# --------------------------------------------------------------------------- #
# generators
# --------------------------------------------------------------------------- #
def _rc_lowpass(finding: dict, ctx: topo.ReviewCtx) -> Testbench | str:
    refs = [str(r) for r in (finding.get("refs") or [])]
    comps = [ctx.comps.get(r) for r in refs]
    if len(refs) != 2 or any(c is None for c in comps):
        return "refs do not resolve to two live components"
    r_ref = next((r for r, c in zip(refs, comps) if topo.is_resistor(c)), None)
    c_ref = next((r for r, c in zip(refs, comps) if topo.is_capacitor(c)), None)
    if not r_ref or not c_ref:
        return "refs are not one resistor + one capacitor"

    out_name = next((a.get("id") for a in (finding.get("anchors") or [])
                     if a.get("kind") == "net"), None)
    out_net = next((n for n in ctx.sch.nets if n.name == out_name), None)
    if out_net is None:
        return f"output net {out_name!r} no longer exists"

    in_net = topo.other_net(ctx, r_ref, out_net)
    gnd_net = topo.other_net(ctx, c_ref, out_net)
    in_name, gnd_name = _net_display(in_net), _net_display(gnd_net)
    if not in_name or not gnd_name:
        return "R input / C ground side is unnamed or dangling"

    r = topo.parse_value(ctx.comps[r_ref].value)
    c = topo.parse_value(ctx.comps[c_ref].value)
    if not r or not c or r <= 0 or c <= 0:
        return "R/C values no longer parse (recomputation refused)"
    fc = 1.0 / (6.283185307179586 * r * c)   # recomputed, never trusted

    spec = SimSpec(
        stimuli=[{"kind": "vsource", "name": "Vin",
                  "node": in_name, "node2": "0", "value": "DC 0 AC 1"}],
        analyses={"ac": f"dec 20 {fc / 100:g} {fc * 100:g}"},
        asserts=[{"name": "fcut", "analysis": "ac",
                  "when": f"vdb({out_name})=-3",
                  "approx": fc, "tol": _RC_TOL}],
    )
    return Testbench(
        fingerprint=finding.get("fingerprint") or "0" * 32,
        finding_code=finding.get("code") or "",
        kind="rc_lowpass", refs=[r_ref, c_ref], gnd=gnd_name,
        schematic=_cone(ctx.sch, {r_ref, c_ref}, f"{r_ref}+{c_ref}"),
        spec=spec,
        expect={"fcut": {"value": fc, "tol": _RC_TOL, "unit": "Hz"}},
        note=(f"cone = {r_ref}+{c_ref} only; verifies the local -3 dB corner, "
              "not the loaded in-circuit response"),
    )


def _divider_dc(finding: dict, ctx: topo.ReviewCtx) -> Testbench | str:
    refs = {str(r) for r in (finding.get("refs") or [])}
    d = next((d for d in topo.find_dividers(ctx)
              if {d.r_top, d.r_bottom} <= refs), None)
    if d is None:
        return "divider topology no longer re-derivable from the schematic"
    top_name, mid_name, gnd_name = (_net_display(d.top), _net_display(d.mid),
                                    _net_display(d.bottom))
    if not top_name or not mid_name or not gnd_name:
        return "divider top/mid/bottom net is unnamed"

    rt = topo.parse_value(ctx.comps[d.r_top].value)
    rb = topo.parse_value(ctx.comps[d.r_bottom].value)
    if not rt or not rb or rt <= 0 or rb <= 0:
        return "resistor values no longer parse (recomputation refused)"
    vtop = topo.net_implied_voltage(d.top) or 1.0
    vmid = vtop * rb / (rt + rb)              # recomputed, never trusted

    spec = SimSpec(
        stimuli=[{"kind": "vsource", "name": "Vtop",
                  "node": top_name, "node2": "0", "value": f"{vtop:g}"}],
        analyses={"tran": "10u 1m"},
        asserts=[{"name": "vmid", "analysis": "tran",
                  "meas": f"FIND v({mid_name}) AT=0.9m",
                  "approx": vmid, "tol": _DIVIDER_TOL}],
    )
    return Testbench(
        fingerprint=finding.get("fingerprint") or "0" * 32,
        finding_code=finding.get("code") or "",
        kind="divider_dc", refs=[d.r_top, d.r_bottom], gnd=gnd_name,
        schematic=_cone(ctx.sch, {d.r_top, d.r_bottom},
                        f"{d.r_top}+{d.r_bottom}"),
        spec=spec,
        expect={"vmid": {"value": vmid, "tol": _DIVIDER_TOL, "unit": "V"}},
        note=(f"cone = {d.r_top}+{d.r_bottom} only, driven at "
              f"{vtop:g} V; verifies the unloaded ratio"),
    )


_GENERATORS = {
    "REVIEW_RC_CUTOFF": _rc_lowpass,
    "REVIEW_FB_DIVIDER": _divider_dc,
    "REVIEW_FB_DIVIDER_VREF_MISMATCH": _divider_dc,
    "REVIEW_DIVIDER_TAP_MISMATCH": _divider_dc,
}


def generate(sch: Schematic,
             findings: list[dict]) -> tuple[list[Testbench], list[dict]]:
    """Benches for every generatable finding + ``skipped`` reasons.

    Deterministic order (fingerprint), one bench per finding. Findings with
    no generator are silently out of scope (they carry no simulable claim);
    findings WITH a generator that cannot re-derive their topology from the
    live schematic are reported in ``skipped`` — loud, per the house rule
    that a vacuous pass must never look like a real one.
    """
    ctx = topo.build_ctx(sch)
    benches: list[Testbench] = []
    skipped: list[dict] = []
    for f in sorted((f for f in findings if isinstance(f, dict)),
                    key=lambda f: (f.get("fingerprint") or "",
                                   f.get("code") or "")):
        gen = _GENERATORS.get(f.get("code") or "")
        if gen is None:
            continue
        result = gen(f, ctx)
        if isinstance(result, str):
            skipped.append({"fingerprint": f.get("fingerprint") or "",
                            "finding_code": f.get("code") or "",
                            "reason": result})
        else:
            benches.append(result)
    return benches, skipped


def run_bench(bench: Testbench, *, timeout: float = 60.0,
              workdir: str | None = None) -> dict:
    """Build the cone's deck, run ngspice, evaluate the bounds; one verdict."""
    import hashlib
    import tempfile
    from pathlib import Path

    from ..sim import assertions as _assertions
    from ..sim import deck as _deck
    from ..sim import engine as _engine

    d = _deck.build(bench.schematic, bench.spec, gnd=bench.gnd)
    verdict: dict = {**bench.describe(),
                     "deck_sha": hashlib.sha256(d.text.encode()).hexdigest()}
    with tempfile.TemporaryDirectory(prefix="akcli-testbench-") as tmp:
        res = _engine.run(d.text, _assertions.run_commands(bench.spec),
                          timeout=timeout,
                          workdir=Path(workdir) if workdir else Path(tmp))
    if not res.ok:
        verdict.update(ok=False, error=res.error or "engine failed",
                       measured={})
        return verdict
    measured_raw = _assertions.parse_meas_output(res.meas_lines)
    findings, measured = _assertions.evaluate(bench.spec, measured_raw)
    verdict.update(
        ok=not findings,
        measured=measured,
        findings=[{"code": f.code, "severity": f.severity.value,
                   "message": f.message} for f in findings],
    )
    return verdict
