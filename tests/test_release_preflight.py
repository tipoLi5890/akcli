"""`akcli release preflight` — gate orchestration + traceable manifest."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from akcli.cli import main

# the same known-good sch/pcb pair the sch-pcb tests use
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
  (wire (pts (xy 101.6 105.41) (xy 101.6 113.03)))
  (label "MID" (at 101.6 109.22 0))
  (wire (pts (xy 101.6 97.79) (xy 96.52 97.79)))
  (wire (pts (xy 96.52 97.79) (xy 96.52 120.65)))
  (wire (pts (xy 96.52 120.65) (xy 101.6 120.65)))
  (label "A" (at 96.52 109.22 0))
  (symbol (lib_id "Device:R") (at 101.6 101.6 0) (unit 1)
    (uuid 55555555-5555-5555-5555-555555555551)
    (property "Reference" "R1" (at 104 101.6 0))
    (property "Value" "10k" (at 106 101.6 0))
    (property "Footprint" "jlc:R_0402" (at 101.6 101.6 0))
    (pin "1" (uuid 55555555-0000-0000-0000-000000000001))
    (pin "2" (uuid 55555555-0000-0000-0000-000000000002)))
  (symbol (lib_id "Device:R") (at 101.6 116.84 0) (unit 1)
    (uuid 55555555-5555-5555-5555-555555555552)
    (property "Reference" "R2" (at 104 116.84 0))
    (property "Value" "10k" (at 106 116.84 0))
    (property "Footprint" "jlc:R_0402" (at 101.6 116.84 0))
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
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "A")))
)"""

_PROFILE = """
id = "test-profile"
vendor = "JLCPCB"
[source]
urls = ["https://example.com/rules"]
retrieved_at = "2026-07-14"
[via]
min_drill_mm = 0.30
"""

_ORDER = """
delivery_format = "single"
design_count = 1
rush = false
surface_finish = "HASL_LF"
via_covering = "tented"
board_material = "FR4"
copper_weight_oz = 1
"""


@pytest.fixture()
def proj(tmp_path):
    (tmp_path / "x.kicad_sch").write_text(_SCH)
    (tmp_path / "x.kicad_pcb").write_text(_PCB)
    (tmp_path / "profile.toml").write_text(_PROFILE)
    (tmp_path / "order.toml").write_text(_ORDER)
    return tmp_path


def test_preflight_pass_writes_manifest(proj, capsys):
    out = proj / "manifest.json"
    code = main(["release", "preflight",
                 "--sch", str(proj / "x.kicad_sch"),
                 "--pcb", str(proj / "x.kicad_pcb"),
                 "--fab-profile", str(proj / "profile.toml"),
                 "--order", str(proj / "order.toml"),
                 "--out", str(out), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert doc["result"] == "PASS"
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["sch-pcb"]["status"] == "pass"
    assert by["fab"]["status"] == "pass"
    assert by["order"]["status"] == "pass"
    assert by["intent"]["status"] == "skipped"       # skipped is explicit
    assert by["git"]["status"] == "skipped"          # tmp dir is not a repo
    assert doc["inputs"]["sch"]["sha256"]
    assert json.loads(out.read_text())["result"] == "PASS"


def test_preflight_fails_on_schpcb_mismatch(proj, capsys):
    (proj / "x.kicad_pcb").write_text(_PCB.replace(
        '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "MID"))',
        '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "A"))'))
    code = main(["release", "preflight",
                 "--sch", str(proj / "x.kicad_sch"),
                 "--pcb", str(proj / "x.kicad_pcb"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["sch-pcb"]["status"] == "fail"
    assert any(f["code"].startswith("SCHPCB_NET")
               for f in by["sch-pcb"]["findings"])


def _git(*argv: str, cwd: Path) -> None:
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True)


def test_preflight_dirty_worktree_fails_unless_allowed(proj, capsys):
    _git("init", "-q", cwd=proj)
    _git("add", "-A", cwd=proj)
    _git("-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init", cwd=proj)
    (proj / "x.kicad_sch").write_text(_SCH + "\n")   # dirty it

    code = main(["release", "preflight",
                 "--sch", str(proj / "x.kicad_sch"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["git"]["status"] == "fail"
    assert doc["git"]["dirty"] is True

    code = main(["release", "preflight",
                 "--sch", str(proj / "x.kicad_sch"), "--allow-dirty", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert doc["git"]["dirty"] is True               # recorded, not hidden
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["git"].get("allow_dirty") is True
