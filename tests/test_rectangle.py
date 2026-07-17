"""``add_rectangle`` — top-level graphic rectangle (module borders / frames).

Pure annotation: netbuild never reads graphics, so a rectangle is
connectivity-neutral by construction; the uuid seed follows the wire
convention (coordinates), so re-apply is byte-identical. The top-level
``(rectangle ...)`` grammar (vs the fixture-verified symbol-body form) is
acceptance-gated on a real ``kicad-cli`` run in the CI KiCad job.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import cli, netdiff, ops
from akcli.drivers import kicad_cli
from akcli.readers import kicad as kreader
from akcli.readers import sexpr
from akcli.writers import kicad as kw

DEVICE = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym"


def _blank_sch(tmp_path: Path) -> Path:
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    return tgt


def _oplist(*ops_list, **envelope):
    return {"protocol_version": 1, "target_format": "kicad",
            "ops": list(ops_list), **envelope}


_RECT = {"op": "add_rectangle", "start": [500, 500], "end": [2500, 2000]}


def test_rectangle_node_shape_and_idempotency(tmp_path):
    tgt = _blank_sch(tmp_path)
    rs = kw.apply(_oplist(_RECT), str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    first = tgt.read_bytes()

    root = sexpr.parse(tgt.read_text(encoding="utf-8"))
    (rect,) = [c for c in root.children
               if c.is_list and c.children and c.children[0].value == "rectangle"]
    tags = [c.children[0].value for c in rect.children[1:] if c.is_list]
    assert tags == ["start", "end", "stroke", "fill", "uuid"]

    rs = kw.apply(_oplist(_RECT), str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    assert tgt.read_bytes() == first          # byte-identical re-apply


def test_rectangle_is_connectivity_neutral(tmp_path):
    tgt = _blank_sch(tmp_path)
    seed = _oplist(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000},
        {"op": "add_net_label", "name": "A", "at": "R1.1"},
        {"op": "add_net_label", "name": "B", "at": "R1.2"},
    )
    rs = kw.apply(seed, str(tgt), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs)
    before = kreader.read_sch(str(tgt)).nets
    # a rectangle drawn right across the part changes no net
    rs = kw.apply(_oplist({"op": "add_rectangle", "start": [800, 800],
                           "end": [1200, 1200]}), str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    d = netdiff.diff(before, kreader.read_sch(str(tgt)).nets)
    assert d.equivalent, netdiff.format_summary(d)


def test_rectangle_stroke_width_and_fill(tmp_path):
    tgt = _blank_sch(tmp_path)
    rs = kw.apply(_oplist({"op": "add_rectangle", "start": [0, 0],
                           "end": [1000, 1000], "stroke_width_mil": 10,
                           "fill": "background"}), str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    text = tgt.read_text(encoding="utf-8")
    assert "(fill (type background))" in text.replace("\n", " ").replace("  ", " ") \
        or '(type background)' in text
    assert "0.254" in text                    # 10 mil in mm


def test_rectangle_delete_by_uuid_and_kind(tmp_path):
    tgt = _blank_sch(tmp_path)
    rs = kw.apply(_oplist(_RECT), str(tgt), apply=True)
    (uid,) = rs[0].created_uuids
    # by uuid
    rs = kw.apply(_oplist({"op": "delete_object", "uuid": uid}),
                  str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    assert "rectangle" not in tgt.read_text(encoding="utf-8")
    # by kind (exactly-one semantics)
    kw.apply(_oplist(_RECT), str(tgt), apply=True)
    rs = kw.apply(_oplist({"op": "delete_object",
                           "match": {"kind": "rectangle"}}), str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    assert "rectangle" not in tgt.read_text(encoding="utf-8")


def test_rectangle_group_local_coordinates():
    d = _oplist({"op": "add_rectangle", "group": "M", "start": [0, 0],
                 "end": [1000, 800]}, groups={"M": {"origin": [2000, 3000]}})
    r = ops.resolve_groups(d)
    assert r["ops"][0]["start"] == [2000, 3000]
    assert r["ops"][0]["end"] == [3000, 3800]


def test_rectangle_validator_rules():
    errs = ops.validate_oplist(_oplist({"op": "add_rectangle", "start": [0, 0]}))
    assert any("required field 'end'" in e.message for e in errs)
    errs = ops.validate_oplist(_oplist({"op": "add_rectangle", "start": [0, 0],
                                        "end": [1, 1], "fill": "purple"}))
    assert any('"none", "outline", "background"' in e.message for e in errs)


def test_rectangle_template_and_ops_list(capsys):
    assert cli.main(["ops", "template", "add_rectangle"]) == 0
    doc = json.loads(capsys.readouterr().out)
    (op,) = doc["ops"]
    assert {"start", "end"} <= set(op)
    assert cli.main(["ops", "list"]) == 0
    assert "add_rectangle" in capsys.readouterr().out


def test_rectangle_accepted_by_kicad_cli(tmp_path):
    """The TOP-LEVEL rectangle grammar must parse in a real KiCad (CI job)."""
    if not kicad_cli.available():
        pytest.skip("kicad-cli not installed (runs in the CI KiCad job)")
    tgt = _blank_sch(tmp_path)
    rs = kw.apply(_oplist(_RECT), str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    report = kicad_cli.erc(str(tgt))
    assert report is not None
