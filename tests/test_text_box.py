"""``add_text_box`` — bordered multi-line note box (fixture-verified grammar).

tests/fixtures/kicad/text_box.kicad_sch is the ground truth: a text_box
accepted by a real kicad-cli (export svg + ERC clean). The emitter must
reproduce that child order; acceptance is re-gated on kicad-cli in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akcli import ops
from akcli.drivers import kicad_cli
from akcli.readers import sexpr
from akcli.writers import kicad as kw

FIXTURE = (Path(__file__).parent / "fixtures" / "kicad" / "graphics"
           / "text_box.kicad_sch")


def _blank_sch(tmp_path: Path) -> Path:
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    return tgt


def _doc(*ops_list, **envelope):
    return {"protocol_version": 1, "target_format": "kicad",
            "ops": list(ops_list), **envelope}


_TBX = {"op": "add_text_box", "text": "Module notes\nline two",
        "at": [1000, 1000], "size": [2000, 1000]}


def _top_nodes(path: Path, tag: str):
    root = sexpr.parse(path.read_text(encoding="utf-8"))
    return [c for c in root.children or []
            if c.is_list and c.children and c.children[0].value == tag]


def test_emitted_shape_matches_fixture(tmp_path):
    """Same child tag order as the kicad-cli-accepted ground-truth fixture."""
    (want,) = _top_nodes(FIXTURE, "text_box")
    want_tags = [c.children[0].value for c in want.children[1:] if c.is_list]

    tgt = _blank_sch(tmp_path)
    rs = kw.apply(_doc(_TBX), str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    (got,) = _top_nodes(tgt, "text_box")
    got_tags = [c.children[0].value for c in got.children[1:] if c.is_list]
    assert got_tags == want_tags


def test_idempotent_and_keyed_refresh(tmp_path):
    tgt = _blank_sch(tmp_path)
    rs = kw.apply(_doc(_TBX), str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    first = tgt.read_bytes()
    rs = kw.apply(_doc(_TBX), str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    assert tgt.read_bytes() == first

    # keyed: same key at a NEW position replaces in place
    keyed = dict(_TBX, key="notes:pwr")
    kw.apply(_doc(keyed), str(tgt), apply=True)
    moved = dict(keyed, at=[5000, 5000], text="updated")
    kw.apply(_doc(moved), str(tgt), apply=True)
    boxes = _top_nodes(tgt, "text_box")
    assert len(boxes) == 2                      # unkeyed original + ONE keyed


def test_group_local_at_translates():
    d = _doc(dict(_TBX, group="M", at=[0, 0]),
             groups={"M": {"origin": [3000, 2000]}})
    assert ops.resolve_groups(d)["ops"][0]["at"] == [3000, 2000]


def test_delete_by_kind_and_name(tmp_path):
    tgt = _blank_sch(tmp_path)
    kw.apply(_doc(_TBX), str(tgt), apply=True)
    rs = kw.apply(_doc({"op": "delete_object",
                        "match": {"kind": "text_box"}}), str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    assert not _top_nodes(tgt, "text_box")


def test_accepted_by_kicad_cli(tmp_path):
    if not kicad_cli.available():
        pytest.skip("kicad-cli not installed (runs in the CI KiCad job)")
    tgt = _blank_sch(tmp_path)
    rs = kw.apply(_doc(_TBX), str(tgt), apply=True)
    assert all(r.status == "ok" for r in rs)
    assert kicad_cli.erc(str(tgt)) is not None
