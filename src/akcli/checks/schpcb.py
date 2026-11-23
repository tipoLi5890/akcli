"""Schematic <-> PCB equivalence (``akcli verify sch.kicad_sch board.kicad_pcb``).

Compares the schematic's normalized netlist against the board's pad-level net
bindings (schema 1.2 deep PCB read). Net NAMES are not trusted — conversions
and auto-named nets differ legitimately — the comparison is the **partition**:
two pins joined on the schematic must be joined on the board and vice versa.

Finding codes:

* ``SCHPCB_MISSING_ON_PCB`` / ``SCHPCB_EXTRA_ON_PCB`` — refdes set drift (an
  extra footprint KiCad itself marks ``board_only`` — "not in schematic":
  mounting holes, fiducials, mechanical parts — is expected, so it is a NOTE);
* ``SCHPCB_FOOTPRINT_MISMATCH`` / ``SCHPCB_VALUE_MISMATCH`` — assignment drift;
* ``SCHPCB_DNP_MISMATCH`` / ``SCHPCB_BOM_FLAG_MISMATCH`` — the schematic's
  ``dnp`` / ``in_bom`` flags disagree with the board footprint's ``dnp`` /
  ``exclude_from_bom`` attributes (WARNING; KiCad schematic vs KiCad board
  only — the readers of other formats cannot know these flags);
* ``SCHPCB_PAD_MISSING`` — a schematic pin with no pad on the board footprint;
* ``SCHPCB_NET_SPLIT`` — one schematic net spans >1 board nets;
* ``SCHPCB_NET_MERGE`` — one board net shorts >1 schematic nets;
* ``SCHPCB_UNNETTED_PAD`` — a board pad carrying no net while its schematic
  pin belongs to a multi-pin net (NOTE: single-pin nets are expected NC pads).

``refs`` are what a config ``[[waiver]]`` scopes on, in the checkers' usual
spelling: the designator for a component finding, ``REF.PIN`` for a pad, the
schematic net name for a net finding (a merge lists the board net first, then
every schematic net it shorts).
"""

from __future__ import annotations

from collections import defaultdict

from ..model import Pcb, Schematic
from ..report import Finding, Severity, anchor

__all__ = ["run"]

_LIMIT = 10          # max pins listed per net finding message


def _fmt_pins(pins) -> str:
    ordered = sorted(f"{d}.{p}" for d, p in pins)
    listed = ", ".join(ordered[:_LIMIT])
    if len(ordered) > _LIMIT:
        listed += f", … (+{len(ordered) - _LIMIT})"
    return listed


def run(sch: Schematic, pcb: Pcb) -> list[Finding]:
    findings: list[Finding] = []
    out = findings.append

    # '#'-prefixed designators (power ports, PWR_FLAG) are schematic-only
    # pseudo-components; KiCad never forwards them to the board.
    sch_by_ref = {c.designator: c for c in sch.components
                  if not c.designator.startswith("#")}
    pcb_by_ref = {f.designator: f for f in pcb.footprints if f.designator}

    # --- refdes presence ---------------------------------------------------- #
    for ref in sorted(set(sch_by_ref) - set(pcb_by_ref)):
        out(Finding("SCHPCB_MISSING_ON_PCB", Severity.ERROR,
                    f"{ref}: on the schematic but not on the board",
                    refs=[ref], anchors=[anchor("component", ref)]))
    for ref in sorted(set(pcb_by_ref) - set(sch_by_ref)):
        if pcb_by_ref[ref].board_only:
            # KiCad's own marker for a footprint that HAS no schematic symbol
            # (mounting hole, fiducial, NPTH mechanical part): expected.
            out(Finding("SCHPCB_EXTRA_ON_PCB", Severity.NOTE,
                        f"{ref}: on the board but not on the schematic — "
                        "expected: the footprint is marked board-only in "
                        "KiCad (\"Not in schematic\")",
                        refs=[ref], anchors=[anchor("component", ref)]))
            continue
        out(Finding("SCHPCB_EXTRA_ON_PCB", Severity.WARNING,
                    f"{ref}: on the board but not on the schematic (a "
                    "board-only mechanical part should be marked \"Not in "
                    "schematic\" in its KiCad footprint properties; "
                    "otherwise waive it)",
                    refs=[ref], anchors=[anchor("component", ref)]))

    # --- footprint / value assignment --------------------------------------- #
    for ref in sorted(set(sch_by_ref) & set(pcb_by_ref)):
        c, f = sch_by_ref[ref], pcb_by_ref[ref]
        if c.footprint and f.footprint_name and c.footprint != f.footprint_name:
            out(Finding("SCHPCB_FOOTPRINT_MISMATCH", Severity.ERROR,
                        f"{ref}: schematic assigns {c.footprint!r}, board has "
                        f"{f.footprint_name!r}",
                        refs=[ref], anchors=[anchor("component", ref)]))
        if (c.value or "") != (f.value or "") and c.value and f.value:
            out(Finding("SCHPCB_VALUE_MISMATCH", Severity.WARNING,
                        f"{ref}: schematic value {c.value!r}, board value "
                        f"{f.value!r}",
                        refs=[ref], anchors=[anchor("component", ref)]))

    # --- assembly flags ------------------------------------------------------ #
    # Only when BOTH sides can know: the KiCad readers parse `(dnp ...)` /
    # `(in_bom ...)` and `(attr ... dnp exclude_from_bom)`; every other reader
    # leaves the model defaults, which must never be compared as facts.
    if sch.source_format == "kicad" and pcb.source_format == "kicad":
        for ref in sorted(set(sch_by_ref) & set(pcb_by_ref)):
            c, f = sch_by_ref[ref], pcb_by_ref[ref]
            if c.dnp != f.dnp:
                out(Finding("SCHPCB_DNP_MISMATCH", Severity.WARNING,
                            f"{ref}: {'DNP' if c.dnp else 'fitted'} on the "
                            f"schematic, {'DNP' if f.dnp else 'fitted'} on the "
                            "board — BOM and assembly outputs would disagree "
                            "(update the PCB from the schematic)",
                            refs=[ref], anchors=[anchor("component", ref)]))
            if c.in_bom == f.exclude_from_bom:
                out(Finding("SCHPCB_BOM_FLAG_MISMATCH", Severity.WARNING,
                            f"{ref}: "
                            f"{'in' if c.in_bom else 'excluded from'} the BOM "
                            "on the schematic, "
                            f"{'excluded from' if f.exclude_from_bom else 'in'} "
                            "the BOM on the board footprint (update the PCB "
                            "from the schematic)",
                            refs=[ref], anchors=[anchor("component", ref)]))

    # --- pad-level net partition -------------------------------------------- #
    pcb_net_of: dict[tuple[str, str], str | None] = {}
    pcb_pads_of_ref: dict[str, set[str]] = defaultdict(set)
    for pad in pcb.pads:
        key = (pad.get("component") or "", pad.get("number") or "")
        if not key[0] or not key[1]:
            continue
        # a pad number can repeat (thermal pads split into several pads);
        # any netted instance wins over None
        if key not in pcb_net_of or (pcb_net_of[key] is None and pad.get("net")):
            pcb_net_of[key] = pad.get("net")
        pcb_pads_of_ref[key[0]].add(key[1])

    if not pcb.pads:
        out(Finding("SCHPCB_NO_PAD_DATA", Severity.WARNING,
                    "board has no pad-level data — net equivalence not checked "
                    "(pre-1.2 export or empty board)"))
        return findings

    sch_net_of: dict[tuple[str, str], int] = {}
    for i, net in enumerate(sch.nets):
        for ref_pin in net.members:
            sch_net_of[ref_pin] = i

    common_refs = set(sch_by_ref) & set(pcb_by_ref)

    # schematic pins whose component exists on the board but the pad doesn't
    for c in sch.components:
        if c.designator not in common_refs:
            continue
        have = pcb_pads_of_ref.get(c.designator, set())
        for pin in c.pins:
            if pin.number not in have:
                pin_ref = f"{c.designator}.{pin.number}"
                out(Finding("SCHPCB_PAD_MISSING", Severity.ERROR,
                            f"{pin_ref}: schematic pin has no "
                            "pad on the board footprint",
                            refs=[pin_ref], anchors=[anchor("pin", pin_ref)]))

    # net split: one schematic net -> multiple board nets
    for net in sch.nets:
        members = [m for m in net.members if m[0] in common_refs and m in pcb_net_of]
        groups: dict[str, list] = defaultdict(list)
        unnetted = []
        for m in members:
            pnet = pcb_net_of[m]
            if pnet is None:
                unnetted.append(m)
            else:
                groups[pnet].append(m)
        label = net.name or f"<unnamed {net.stable_id}>"
        if len(groups) > 1:
            detail = "; ".join(
                f"{pnet!r}: {_fmt_pins(pins)}" for pnet, pins in sorted(groups.items()))
            out(Finding("SCHPCB_NET_SPLIT", Severity.ERROR,
                        f"schematic net {label} is split across "
                        f"{len(groups)} board nets — {detail}",
                        refs=[label], anchors=[anchor("net", label)]))
        if unnetted and len(net.members) > 1:
            out(Finding("SCHPCB_UNNETTED_PAD", Severity.WARNING,
                        f"schematic net {label}: board pads carry no net: "
                        f"{_fmt_pins(unnetted)}",
                        refs=[label], anchors=[anchor("net", label)]))

    # net merge: one board net -> multiple schematic nets
    board_groups: dict[str, set[int]] = defaultdict(set)
    board_members: dict[str, list] = defaultdict(list)
    for ref_pin, pnet in pcb_net_of.items():
        if pnet is None or ref_pin not in sch_net_of:
            continue
        board_groups[pnet].add(sch_net_of[ref_pin])
        board_members[pnet].append(ref_pin)
    for pnet, snets in sorted(board_groups.items()):
        if len(snets) > 1:
            names = sorted((sch.nets[i].name or f"<unnamed {sch.nets[i].stable_id}>")
                           for i in snets)
            out(Finding("SCHPCB_NET_MERGE", Severity.ERROR,
                        f"board net {pnet!r} shorts {len(snets)} schematic nets "
                        f"({', '.join(names[:6])}) — pads {_fmt_pins(board_members[pnet])}",
                        refs=[pnet, *names], anchors=[anchor("net", pnet)]))
    return findings
