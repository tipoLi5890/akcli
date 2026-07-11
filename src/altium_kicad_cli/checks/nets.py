"""Connectivity-hygiene checks (net-level, format-agnostic).

``run(sch, cfg) -> list[Finding]`` flags the classic "looks fine, isn't
connected" traps that graphical review misses:

* **NET_SINGLE_PIN** — a net with exactly one pin: either a floating label,
  a power port driving nothing, or a missing second connection. This is the
  net-level twin of KiCad's single-pin-net ERC.
* **NET_OFF_GRID** — component pins whose world coordinates are off the
  schematic grid. Off-grid pins are the canonical cause of wires that *touch*
  a pin on screen without ever joining its net.

Grid and coincidence policy
---------------------------
All comparisons are **exact integer nanometres** (``units.mil_to_nm``), never
rounded floats: two points COINCIDE only when their nm coordinates are equal
after the mil→nm rounding, which is the same quantisation the readers and the
writer gate use — so this check can neither claim a coincidence netbuild
rejects nor miss one it accepts. The grid comes from ``Config.grid_nm``
(``[project] grid`` — ``"50mil"`` default, metric values like ``"1.27mm"`` /
``"0.5mm"`` are exact in nm). A pin is off-grid when its nm residue exceeds
``_TOL_NM`` (0.5 mil): unit conversion noise is far below it, while a
genuinely misplaced pin (KiCad snaps exactly) is far above it.

Everything works on the normalized model, so Altium inputs are checked with
the same rules as KiCad ones.
"""

from __future__ import annotations

from .. import units
from ..config import DEFAULT_GRID_NM, Config
from ..model import Schematic
from ..report import Finding, Severity

NET_SINGLE_PIN = "NET_SINGLE_PIN"   # WARNING: 1-member net (floating / undriven)
NET_OFF_GRID = "NET_OFF_GRID"       # WARNING: pins off the schematic grid

_TOL_NM = units.mil_to_nm(0.5)      # past rounding noise = genuinely off-grid


def _off_grid(nm: int, grid_nm: int) -> bool:
    r = nm % grid_nm
    return min(r, grid_nm - r) > _TOL_NM


def _pt_nm(x_mil: float, y_mil: float) -> tuple[int, int]:
    return (units.mil_to_nm(x_mil), units.mil_to_nm(y_mil))


def run(sch: Schematic, cfg: Config | None = None) -> list[Finding]:
    findings: list[Finding] = []
    grid_nm = cfg.grid_nm if cfg is not None else DEFAULT_GRID_NM

    # pins carrying an explicit no-connect marker are intentionally single
    nc_points = {_pt_nm(x, y) for x, y in (sch.no_erc_points or [])}
    pin_at = {(c.designator, p.number): _pt_nm(p.x_mil, p.y_mil)
              for c in sch.components for p in c.pins}

    for net in sch.nets:
        if len(net.members) != 1:
            continue
        d, p = net.members[0]
        if pin_at.get((d, p)) in nc_points:
            continue
        what = ("power port drives nothing"
                if d.startswith("#") else "floating or missing connection")
        findings.append(Finding(
            NET_SINGLE_PIN, Severity.WARNING,
            f"net '{net.name}' has a single pin {d}.{p} — {what}",
            refs=[f"{d}.{p}"],
        ))

    grid_mil = grid_nm / units.NM_PER_MIL
    for comp in sch.components:
        off = [p for p in comp.pins
               if _off_grid(units.mil_to_nm(p.x_mil), grid_nm)
               or _off_grid(units.mil_to_nm(p.y_mil), grid_nm)]
        if not off:
            continue
        sample = ", ".join(
            f"{p.number}@({p.x_mil:g},{p.y_mil:g})" for p in off[:4])
        more = f" (+{len(off) - 4} more)" if len(off) > 4 else ""
        findings.append(Finding(
            NET_OFF_GRID, Severity.WARNING,
            f"{comp.designator}: {len(off)} pin(s) off the {grid_mil:g}-mil "
            f"grid: {sample}{more} — wires may touch without connecting",
            refs=[comp.designator],
        ))

    return findings
