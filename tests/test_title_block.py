"""``set_title_block`` — title/date/rev/company/comment1..9 editing.

Before this op the title block was write-once (``new --title``) and otherwise
untouchable. Find-or-create keeps the node where eeschema expects it (after
``paper``, before ``lib_symbols``); no uuid, so idempotency is
find-or-replace-by-tag with a replay-safe note when nothing changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akcli import netdiff, ops
from akcli.drivers import kicad_cli
from akcli.readers import kicad as kreader
from akcli.readers import sexpr
from akcli.writers import kicad as kw

DEVICE = str(Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym")


def _blank_sch(tmp_path: Path, title: str | None = None) -> Path:
    tgt = tmp_path / "board.kicad_sch"
    tb = f'(title_block (title "{title}")) ' if title else ""
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") '
                   f'(paper "A4") {tb}(lib_symbols))\n')
    return tgt


def _doc(*ops_list):
    return {"protocol_version": 1, "target_format": "kicad", "ops": list(ops_list)}


def _tb(tgt: Path):
    return sexpr.parse(tgt.read_text(encoding="utf-8")).find("title_block")


def test_creates_title_block_in_the_right_place(tmp_path):
    tgt = _blank_sch(tmp_path)
    rs = kw.apply(_doc({"op": "set_title_block", "title": "Demo Board",
                        "rev": "A", "company": "ACME",
                        "comment1": "first note"}), str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    root = sexpr.parse(tgt.read_text(encoding="utf-8"))
    tags = [c.children[0].value for c in root.children or [] if c.is_list]
    # after paper, before lib_symbols
    assert tags.index("paper") < tags.index("title_block") < tags.index("lib_symbols")
    tb = root.find("title_block")
    assert tb.find("title").children[1].value == "Demo Board"
    assert tb.find("rev").children[1].value == "A"
    comment = tb.find("comment")
    assert (comment.children[1].value, comment.children[2].value) == ("1", "first note")


def test_edits_existing_field_and_reports_noop(tmp_path):
    tgt = _blank_sch(tmp_path, title="Old")
    rs = kw.apply(_doc({"op": "set_title_block", "title": "New"}),
                  str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    assert _tb(tgt).find("title").children[1].value == "New"
    first = tgt.read_bytes()

    # replay: same value -> replay-safe note, byte-identical file
    rs = kw.apply(_doc({"op": "set_title_block", "title": "New"}),
                  str(tgt), apply=True)
    assert rs[0].status == "ok"
    assert "nothing to change" in rs[0].message
    assert tgt.read_bytes() == first


def test_requires_at_least_one_field():
    errs = ops.validate_oplist(_doc({"op": "set_title_block"}))
    assert any("at least one field" in e.message for e in errs)
    assert not ops.validate_oplist(_doc({"op": "set_title_block", "rev": "B"}))


def test_netdiff_neutral(tmp_path):
    tgt = _blank_sch(tmp_path)
    seed = _doc(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000},
        {"op": "add_net_label", "name": "A", "at": "R1.1"},
        {"op": "add_net_label", "name": "B", "at": "R1.2"},
    )
    rs = kw.apply(seed, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs)
    before = kreader.read_sch(str(tgt)).nets
    rs = kw.apply(_doc({"op": "set_title_block", "title": "T", "date": "2026-07-17"}),
                  str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    d = netdiff.diff(before, kreader.read_sch(str(tgt)).nets)
    assert d.equivalent, netdiff.format_summary(d)


def test_title_block_accepted_by_kicad_cli(tmp_path):
    if not kicad_cli.available():
        pytest.skip("kicad-cli not installed (runs in the CI KiCad job)")
    tgt = _blank_sch(tmp_path)
    rs = kw.apply(_doc({"op": "set_title_block", "title": "T", "rev": "A",
                        "company": "ACME", "date": "2026-07-17",
                        "comment1": "c1", "comment9": "c9"}),
                  str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    assert kicad_cli.erc(str(tgt)) is not None
