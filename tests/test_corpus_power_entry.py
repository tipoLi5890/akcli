"""Corpus board #2: power_entry — the power-protection review calibration board.

Two power entries drawn by akcli itself (see ``power_entry.ops.json``): a
PROTECTED battery path (VBAT → F1 500 mA fuse → D1 series diode → VSYS bulk +
decoupling) and a deliberately UNPROTECTED VBUS sense branch (decoupled,
divided, RC-filtered — no fuse, no reverse element). The review layer must
stay silent on the protected chain and fire exactly once per rule on the
unprotected one — token-suffixed derived nets (``VBUS_SENSE``/``VBUS_ADC``)
must NOT seed duplicate findings.
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli.ops import PROTOCOL_VERSION
from akcli.readers import kicad as kreader
from akcli.review import engine

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "tests" / "fixtures" / "corpus" / "power_entry.kicad_sch"


def test_reads_with_expected_census():
    sch = kreader.read_sch(str(BOARD))
    real = [c for c in sch.components if not c.designator.startswith("#")]
    assert len(real) == 9
    named = {n.name for n in sch.nets if n.name}
    assert named == {"VBAT", "VBAT_F", "VSYS", "GND",
                     "VBUS", "VBUS_SENSE", "VBUS_ADC"}


def test_reproducible_from_ops_json():
    ops = json.loads((BOARD.parent / "power_entry.ops.json").read_text(
        encoding="utf-8"))
    assert ops["protocol_version"] == PROTOCOL_VERSION
    assert ops["target_file"] == BOARD.name


def test_power_protection_review_calibration():
    sch = kreader.read_sch(str(BOARD))
    findings, _meta = engine.analyze(sch, profile="deep")
    codes = sorted(f.code for f in findings)
    assert codes == ["REVIEW_FUSE_MISSING", "REVIEW_RC_CUTOFF",
                     "REVIEW_REVPOL_UNPROTECTED"]
    by_code = {f.code: f for f in findings}
    # exactly the VBUS branch — never the protected VBAT chain, and never
    # a duplicate seeded by the derived VBUS_SENSE/VBUS_ADC names
    assert by_code["REVIEW_FUSE_MISSING"].refs[0] == "VBUS"
    assert by_code["REVIEW_REVPOL_UNPROTECTED"].refs[0] == "VBUS"
