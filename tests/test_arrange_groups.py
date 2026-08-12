"""``akcli arrange --groups`` — net-preserving functional-block re-layout.

Relocates each named group into its own shelf-packed region (rigid, carried
moves). The load-bearing guarantee, proven by a before/after netlist diff: with
label-on-pin connectivity the re-layout can NEVER change the netlist — and the
draw pipeline refuses to write if it somehow did.
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli import arrange, cli, netdiff
from akcli.readers import kicad as kreader
from akcli.writers import kicad as kw

DEVICE = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym"
POWER = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "power.kicad_sym"


def _oplist(*ops):
    return {"protocol_version": 2, "target_format": "kicad", "ops": list(ops)}


def _seed_two_blocks(tmp_path: Path) -> Path:
    """Two label-only divider blocks (A: R1-R2, B: R3-R4), initially interleaved."""
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    ops = []
    # interleave the two blocks in space so a rigid regroup has real work to do
    coords = {"R1": (2000, 1000), "R3": (2200, 1000),
              "R2": (2000, 1400), "R4": (2200, 1400)}
    for ref, (x, y) in coords.items():
        ops.append({"op": "place_component", "lib_id": "Device:R",
                    "designator": ref, "x_mil": x, "y_mil": y, "value": "1k"})
    ops += [
        {"op": "add_net_label", "name": "VINA", "at": "R1.1"},
        {"op": "add_net_label", "name": "MIDA", "at": "R1.2"},
        {"op": "add_net_label", "name": "MIDA", "at": "R2.1"},
        {"op": "add_net_label", "name": "GND", "at": "R2.2"},
        {"op": "add_net_label", "name": "VINB", "at": "R3.1"},
        {"op": "add_net_label", "name": "MIDB", "at": "R3.2"},
        {"op": "add_net_label", "name": "MIDB", "at": "R4.1"},
        {"op": "add_net_label", "name": "GND", "at": "R4.2"},
    ]
    rs = kw.apply(_oplist(*ops), str(tgt), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    return tgt


def _groups_file(tmp_path: Path) -> Path:
    f = tmp_path / "groups.toml"
    f.write_text('[groups]\nblock_a = ["R1", "R2"]\nblock_b = ["R3", "R4"]\n')
    return f


def _nets(tgt: Path):
    return kreader.read_sch(str(tgt)).nets


def _pos(tgt: Path):
    return {c.designator: (c.x_mil, c.y_mil)
            for c in kreader.read_sch(str(tgt)).components}


def test_plan_groups_separates_and_reports(tmp_path):
    tgt = _seed_two_blocks(tmp_path)
    groups = {"block_a": ["R1", "R2"], "block_b": ["R3", "R4"]}
    result = arrange.plan_groups(tgt, groups)
    assert {g["group"] for g in result["groups"]} == {"block_a", "block_b"}
    assert result["unplaced"] == []
    assert {m.ref for m in result["moves"]} <= {"R1", "R2", "R3", "R4"}


def test_arrange_groups_is_net_preserving_end_to_end(tmp_path, capsys):
    tgt = _seed_two_blocks(tmp_path)
    gf = _groups_file(tmp_path)
    before = _nets(tgt)
    capsys.readouterr()
    assert cli.main(["arrange", str(tgt), "--groups", str(gf), "--apply"]) == 0
    after = _nets(tgt)
    # THE guarantee: identical netlist after the re-layout.
    d = netdiff.diff(before, after)
    assert d.equivalent, netdiff.format_summary(d)

    # blocks are now spatially separated (B sits below A, past the gap)
    pos = _pos(tgt)
    a_bottom = max(pos["R1"][1], pos["R2"][1])
    b_top = min(pos["R3"][1], pos["R4"][1])
    assert b_top > a_bottom

    # went through the draw pipeline -> undo reverts it
    assert cli.main(["undo", str(tgt), "--apply"]) == 0
    capsys.readouterr()
    assert netdiff.diff(before, _nets(tgt)).equivalent


def test_arrange_groups_json_dry_run_writes_nothing(tmp_path, capsys):
    tgt = _seed_two_blocks(tmp_path)
    gf = _groups_file(tmp_path)
    before = _pos(tgt)
    capsys.readouterr()
    assert cli.main(["arrange", str(tgt), "--groups", str(gf), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["applied"] is False and doc["moves"]
    assert _pos(tgt) == before          # dry-run never writes


def test_power_port_rides_with_its_host(tmp_path, capsys):
    """A #PWR GND port on a component pin must relocate with that component."""
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "44444444-5555-6666-7777-888888888888") (paper "A4"))\n')
    rs = kw.apply(_oplist(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 2000, "y_mil": 1000, "value": "1k"},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
         "x_mil": 3000, "y_mil": 1000, "value": "1k"},
        {"op": "add_net_label", "name": "IN", "at": "R1.1"},
        {"op": "place_power_port", "lib_id": "power:GND", "net_name": "GND",
         "at": "R1.2"},
        {"op": "add_net_label", "name": "IN2", "at": "R2.1"},
        {"op": "place_power_port", "lib_id": "power:GND", "net_name": "GND",
         "at": "R2.2"},
    ), str(tgt), apply=True, sources=[str(DEVICE), str(POWER)])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    before = _nets(tgt)
    gf = tmp_path / "g.json"
    gf.write_text('{"block": ["R1"], "block2": ["R2"]}')
    capsys.readouterr()
    assert cli.main(["arrange", str(tgt), "--groups", str(gf), "--apply",
                     "--symbols", str(DEVICE), "--symbols", str(POWER)]) == 0
    # net-preserving means the GND port stayed glued to R1.2 through the move
    assert netdiff.diff(before, _nets(tgt)).equivalent


# --------------------------------------------------------------------------- #
# 2D side-by-side packing (--page-width / [arrange] page_width)
# --------------------------------------------------------------------------- #
def _block(result, name):
    return next(g for g in result["groups"] if g["group"] == name)


def test_page_width_packs_groups_side_by_side(tmp_path):
    tgt = _seed_two_blocks(tmp_path)
    groups = {"block_a": ["R1", "R2"], "block_b": ["R3", "R4"]}
    result = arrange.plan_groups(tgt, groups, group_gap=1000.0,
                                 page_width=20000.0)
    a, b = _block(result, "block_a"), _block(result, "block_b")
    # same band, and EXACTLY the requested channel between the blocks
    assert a["at"][1] == b["at"][1]
    assert b["at"][0] - (a["at"][0] + a["size"][0]) == 1000.0


def test_page_width_wraps_to_the_next_band(tmp_path):
    tgt = _seed_two_blocks(tmp_path)
    groups = {"block_a": ["R1", "R2"], "block_b": ["R3", "R4"]}
    result = arrange.plan_groups(tgt, groups, group_gap=1000.0,
                                 page_width=1.0)   # nothing fits beside A
    a, b = _block(result, "block_a"), _block(result, "block_b")
    assert b["at"][0] == a["at"][0]
    assert b["at"][1] - (a["at"][1] + a["size"][1]) == 1000.0


def test_page_width_is_net_preserving_end_to_end(tmp_path, capsys):
    tgt = _seed_two_blocks(tmp_path)
    gf = _groups_file(tmp_path)
    before = _nets(tgt)
    capsys.readouterr()
    assert cli.main(["arrange", str(tgt), "--groups", str(gf),
                     "--page-width", "20000", "--group-gap", "1000",
                     "--apply"]) == 0
    assert netdiff.diff(before, _nets(tgt)).equivalent


def test_arrange_config_pins_group_layout_policy(tmp_path, capsys):
    # [arrange] in akcli.toml supplies the defaults; flags stay optional
    tgt = _seed_two_blocks(tmp_path)
    gf = _groups_file(tmp_path)
    (tmp_path / "akcli.toml").write_text(
        "[arrange]\ngroup_gap = 1000\npage_width = 20000\n",
        encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["arrange", str(tgt), "--groups", str(gf), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    a, b = doc["groups"][0], doc["groups"][1]
    assert a["at"][1] == b["at"][1]                       # side by side
    assert b["at"][0] - (a["at"][0] + a["size"][0]) == 1000.0


# --------------------------------------------------------------------------- #
# net-preservation gate (the --groups contract, enforced at apply time)
# --------------------------------------------------------------------------- #
def _seed_coincident_pair(tmp_path: Path) -> Path:
    """R1.2 tip exactly on R2.1 tip: a net held together only by geometry.

    Separating the two into different group blocks MUST split that net —
    the deterministic stand-in for real boards wired across group borders.
    """
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    rs = kw.apply(_oplist(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 2000, "y_mil": 1000, "value": "1k"},
        # Device:R pin tips sit 150 mil above/below the anchor -> R1.2 tip at
        # (2000,1150) == R2.1 tip when R2 anchors at (2000,1300)
        {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
         "x_mil": 2000, "y_mil": 1300, "value": "1k"},
        {"op": "add_net_label", "name": "VIN", "at": "R1.1"},
        {"op": "add_net_label", "name": "GND", "at": "R2.2"},
    ), str(tgt), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    return tgt


def test_apply_refuses_a_net_changing_regroup(tmp_path, capsys):
    tgt = _seed_coincident_pair(tmp_path)
    gf = tmp_path / "groups.toml"
    gf.write_text('[groups]\nblock_a = ["R1"]\nblock_b = ["R2"]\n')
    before = tgt.read_bytes()
    capsys.readouterr()
    rc = cli.main(["arrange", str(tgt), "--groups", str(gf), "--apply"])
    err = capsys.readouterr().err
    assert rc == 6
    assert "REFUSED" in err and "netlist" in err
    assert tgt.read_bytes() == before            # nothing written

    # explicit risk acceptance still works
    assert cli.main(["arrange", str(tgt), "--groups", str(gf), "--apply",
                     "--allow-net-changes"]) == 0
    assert tgt.read_bytes() != before


# --------------------------------------------------------------------------- #
# --propose-labels: the REFUSED -> repair-draft -> re-run loop
# --------------------------------------------------------------------------- #
def _seed_named_bridge(tmp_path: Path) -> Path:
    """Coincident R1.2/R2.1 tips carrying net MID via a label on R1.2 only."""
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    rs = kw.apply(_oplist(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 2000, "y_mil": 1000, "value": "1k"},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
         "x_mil": 2000, "y_mil": 1300, "value": "1k"},
        {"op": "add_net_label", "name": "VIN", "at": "R1.1"},
        {"op": "add_net_label", "name": "MID", "at": "R1.2"},
        {"op": "add_net_label", "name": "GND", "at": "R2.2"},
    ), str(tgt), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    return tgt


def test_propose_labels_repairs_a_refused_regroup(tmp_path, capsys):
    tgt = _seed_named_bridge(tmp_path)
    gf = tmp_path / "groups.toml"
    gf.write_text('[groups]\nblock_a = ["R1"]\nblock_b = ["R2"]\n')

    # the coincidence-held MID net makes a direct re-pack unsafe -> REFUSED
    capsys.readouterr()
    assert cli.main(["arrange", str(tgt), "--groups", str(gf), "--apply"]) == 6
    assert "REFUSED" in capsys.readouterr().err

    # the draft: one MID label per grouped member pin (VIN/GND are
    # single-bundle and stay out of it)
    draft = tmp_path / "labels.json"
    assert cli.main(["arrange", str(tgt), "--groups", str(gf),
                     "--propose-labels", str(draft)]) == 0
    doc = json.loads(draft.read_text(encoding="utf-8"))
    labels = [o for o in doc["ops"] if o["op"] == "add_net_label"]
    assert {(o["name"], o["at"]) for o in labels} == \
        {("MID", "R1.2"), ("MID", "R2.1")}

    # apply the draft, then the SAME re-pack goes through net-preserved
    capsys.readouterr()
    assert cli.main(["draw", str(tgt), "--ops", str(draft), "--apply"]) == 0
    before = _nets(tgt)
    assert cli.main(["arrange", str(tgt), "--groups", str(gf),
                     "--apply"]) == 0
    assert netdiff.diff(before, _nets(tgt)).equivalent


def test_coincident_label_stays_with_the_stayer(tmp_path):
    # writer rule: at a shared pin tip with one label per pin, a carried move
    # takes only its own share — the staying pin keeps its name.
    tgt = _seed_named_bridge(tmp_path)
    rs = kw.apply(_oplist(
        {"op": "add_net_label", "name": "MID", "at": "R2.1"},
    ), str(tgt), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs)
    rs = kw.apply(_oplist(
        {"op": "move_component", "designator": "R1", "x_mil": 6000,
         "y_mil": 1000, "carry_labels": True, "carry_wires": True},
    ), str(tgt), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs)
    nets = {n.name: sorted(map(tuple, n.members))
            for n in _nets(tgt) if n.name == "MID"}
    assert nets["MID"] == [("R1", "2"), ("R2", "1")]   # both still named MID
