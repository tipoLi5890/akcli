"""``akcli check --integrity`` — the write gate's document checks, read-only.

Previously :func:`akcli.writers.connectivity.verify` ran only post-write inside
``draw --apply``; files akcli did not just write (imports, hand edits in
eeschema) never saw DANGLING_ENDPOINT / DUPLICATE_UUID / UNRESOLVED_LIB_ID /
INVALID_INSTANCES_PATH / NO_CONNECT_CONFLICT / DUPLICATE_DESIGNATOR at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli.cli import main

CORPUS = Path(__file__).parent / "fixtures" / "corpus"


def _dangling(tmp_path: Path) -> Path:
    p = tmp_path / "d.kicad_sch"
    p.write_text(
        '(kicad_sch (version 20231120) (generator "t")\n'
        '\t(uuid "aaaaaaaa-0000-4000-8000-000000000000")\n'
        '\t(wire (pts (xy 10 10) (xy 20 10)) (stroke (width 0) (type default))\n'
        '\t\t(uuid "bbbbbbbb-0000-4000-8000-000000000001"))\n'
        ')\n', encoding="utf-8")
    return p


def test_integrity_flags_dangling_wire(tmp_path, capsys):
    rc = main(["check", str(_dangling(tmp_path)), "--integrity", "--json"])
    doc = json.loads(capsys.readouterr().out)
    codes = {f["code"] for f in doc["findings"]}
    assert "DANGLING_ENDPOINT" in codes
    assert rc != 0


def test_integrity_in_default_check_family(tmp_path, capsys):
    """Bare ``check`` on a .kicad_sch includes the integrity family."""
    rc = main(["check", str(_dangling(tmp_path)), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert "DANGLING_ENDPOINT" in {f["code"] for f in doc["findings"]}
    assert rc != 0


def test_integrity_clean_on_corpus(capsys):
    rc = main(["check", str(CORPUS / "power_entry.kicad_sch"),
               "--integrity", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["findings"] == []
    assert rc == 0
