"""Schematic <-> PCB equivalence (`akcli verify sch board.kicad_pcb`).

Acceptance criteria from the enhancement plan: deliberately removing a board
footprint, mis-netting a pad, or swapping a footprint must each be located to
the exact designator/pad — and the clean pair must PASS. Power-port
pseudo-components (``#PWR``/``#FLG``) must be ignored (validated against a real
88-component board).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli.checks import schpcb
from akcli.cli import main
from akcli.readers import kicad

_SCH = """(kicad_sch (version 20230121) (generator eeschema)
  (uuid 44444444-4444-4444-4444-444444444444)
  (paper "A4")
  (lib_symbols
    (symbol "Device:R" (pin_numbers hide) (in_bom yes) (on_board yes)
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (wire (pts (xy 100 103.81) (xy 100 106.19)))
  (label "MID" (at 100 105 0))
  (symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
    (uuid 55555555-5555-5555-5555-555555555551)
    (property "Reference" "R1" (at 102 100 0))
    (property "Value" "10k" (at 104 100 0))
    (property "Footprint" "jlc:R_0402" (at 100 100 0))
    (pin "1" (uuid 55555555-0000-0000-0000-000000000001))
    (pin "2" (uuid 55555555-0000-0000-0000-000000000002)))
  (symbol (lib_id "Device:R") (at 100 110 0) (unit 1)
    (uuid 55555555-5555-5555-5555-555555555552)
    (property "Reference" "R2" (at 102 110 0))
    (property "Value" "10k" (at 104 110 0))
    (property "Footprint" "jlc:R_0402" (at 100 110 0))
    (pin "1" (uuid 55555555-0000-0000-0000-000000000003))
    (pin "2" (uuid 55555555-0000-0000-0000-000000000004)))
)"""

_PCB = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (net 0 "")
  (net 1 "MID")
  (net 2 "A")
  (net 3 "B")
  (footprint "jlc:R_0402" (layer "F.Cu") (at 50 50 0)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "A"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "MID")))
  (footprint "jlc:R_0402" (layer "F.Cu") (at 60 50 0)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "MID"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "B")))
)"""


@pytest.fixture()
def pair(tmp_path):
    sch = tmp_path / "x.kicad_sch"
    pcb = tmp_path / "x.kicad_pcb"
    sch.write_text(_SCH)
    pcb.write_text(_PCB)
    return sch, pcb


def _run(sch_path: Path, pcb_path: Path):
    return schpcb.run(kicad.read_sch(str(sch_path)), kicad.read_pcb(str(pcb_path)))


def _codes(findings) -> set[str]:
    return {f.code for f in findings}


def test_clean_pair_passes(pair):
    sch, pcb = pair
    assert _run(sch, pcb) == []


def test_removed_footprint_located(pair):
    sch, pcb = pair
    pcb.write_text("\n".join(
        line for line in _PCB.splitlines() if True)  # keep text form
        .replace('(property "Reference" "R2"', '(property "Reference" "RX"'))
    findings = _run(sch, pcb)
    codes = _codes(findings)
    assert "SCHPCB_MISSING_ON_PCB" in codes
    assert any("R2" in f.message for f in findings)
    assert "SCHPCB_EXTRA_ON_PCB" in codes          # RX is board-only now


def test_misnetted_pad_located_as_split_and_merge(pair):
    sch, pcb = pair
    # R2.1 belongs on MID; wire it to B instead -> MID splits, B merges
    pcb.write_text(_PCB.replace(
        '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "MID"))',
        '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "B"))'))
    findings = _run(sch, pcb)
    split = [f for f in findings if f.code == "SCHPCB_NET_SPLIT"]
    merge = [f for f in findings if f.code == "SCHPCB_NET_MERGE"]
    assert split and "MID" in split[0].message and "R2.1" in split[0].message
    assert merge and "R2.1" in merge[0].message


def test_swapped_footprint_located(pair):
    sch, pcb = pair
    pcb.write_text(_PCB.replace(
        '(footprint "jlc:R_0402" (layer "F.Cu") (at 60 50 0)',
        '(footprint "jlc:R_0603" (layer "F.Cu") (at 60 50 0)'))
    findings = _run(sch, pcb)
    hits = [f for f in findings if f.code == "SCHPCB_FOOTPRINT_MISMATCH"]
    assert len(hits) == 1 and "R2" in hits[0].message


def test_missing_pad_located(pair):
    sch, pcb = pair
    pcb.write_text(_PCB.replace(
        '    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "B")))',
        "  )"))
    findings = _run(sch, pcb)
    hits = [f for f in findings if f.code == "SCHPCB_PAD_MISSING"]
    assert len(hits) == 1 and "R2.2" in hits[0].message


def test_power_pseudo_components_ignored(pair, tmp_path):
    sch, pcb = pair
    text = sch.read_text().replace(
        '(property "Reference" "R2"', '(property "Reference" "#PWR01"')
    sch.write_text(text)
    findings = _run(sch, pcb)
    assert all("#PWR01" not in f.message
               for f in findings if f.code == "SCHPCB_MISSING_ON_PCB")


def test_cli_verify_dispatches_on_kicad_pcb(pair, capsys):
    sch, pcb = pair
    assert main(["verify", str(sch), str(pcb), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["mode"] == "sch-pcb"
    assert doc["equivalent"] is True


def test_cli_verify_fails_on_mismatch(pair, capsys):
    sch, pcb = pair
    pcb.write_text(_PCB.replace('(net 1 "MID"))', '(net 3 "B"))'))
    assert main(["verify", str(sch), str(pcb)]) == 1
