"""``move_component`` rigid-body carry (``carry_labels`` / ``carry_wires``).

The atomic operation a group re-layout is built from: moving a placed part must
be able to take the net labels (and wires) anchored on its pins along with it,
or every relocation silently strands the labels that name its nets. With the
label-on-pin connectivity pattern (no cross-part wires) a carried move is
PROVABLY net-preserving — proven here by a before/after netlist diff.
"""

from __future__ import annotations

from pathlib import Path

from akcli import netdiff
from akcli.readers import kicad as kreader
from akcli.writers import kicad as kw

DEVICE = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym"


def _oplist(*ops):
    return {"protocol_version": 2, "target_format": "kicad", "ops": list(ops)}


def _seed_label_chain(tmp_path: Path) -> Path:
    """R1 — R2 in series, connected ONLY by labels on their pins.

    Nets: VIN={R1.1}, MID={R1.2, R2.1}, GND={R2.2}. No wires — so a rigid move
    of either part cannot change the netlist as long as its labels come along.
    """
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "11111111-2222-3333-4444-555555555555") (paper "A4"))\n')
    rs = kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R",
             "designator": "R1", "x_mil": 2000, "y_mil": 1000, "value": "1k"},
            {"op": "place_component", "lib_id": "Device:R",
             "designator": "R2", "x_mil": 2000, "y_mil": 2000, "value": "2k"},
            {"op": "add_net_label", "name": "VIN", "at": "R1.1"},
            {"op": "add_net_label", "name": "MID", "at": "R1.2"},
            {"op": "add_net_label", "name": "MID", "at": "R2.1"},
            {"op": "add_net_label", "name": "GND", "at": "R2.2"},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    return tgt


def _nets(tgt: Path):
    return kreader.read_sch(str(tgt)).nets


def test_carry_labels_is_net_preserving(tmp_path):
    tgt = _seed_label_chain(tmp_path)
    before = _nets(tgt)
    verify: list = []
    rs = kw.apply(
        _oplist({"op": "move_component", "designator": "R1",
                 "x_mil": 5000, "y_mil": 3000, "carry_labels": True}),
        str(tgt), apply=True, sources=[str(DEVICE)], verify_out=verify,
    )
    assert rs[0].status == "ok", rs[0].message
    assert not [f for f in verify if f.severity.value in ("error", "critical")]
    after = _nets(tgt)
    # The whole point: same netlist, byte-for-byte partition, after the move.
    assert netdiff.diff(before, after).equivalent, netdiff.format_summary(
        netdiff.diff(before, after))
    # And the part really moved.
    r1 = next(c for c in kreader.read_sch(str(tgt)).components
              if c.designator == "R1")
    assert (r1.x_mil, r1.y_mil) == (5000, 3000)


def test_carry_labels_reports_carried_count(tmp_path):
    tgt = _seed_label_chain(tmp_path)
    rs = kw.apply(
        _oplist({"op": "move_component", "designator": "R1",
                 "x_mil": 5000, "y_mil": 3000, "carry_labels": True}),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    # R1 owns two pin labels (VIN on .1, MID on .2); both are carried.
    assert "carried 2 label(s)" in rs[0].message


def test_move_without_carry_changes_the_netlist(tmp_path):
    """The regression the feature exists to prevent: a plain move strands labels.

    Moving R1 without ``carry_labels`` leaves VIN/MID behind on the old pin
    coordinates, so R1's pins go unlabeled and the netlist changes — the exact
    silent-strand failure the feedback flagged.
    """
    tgt = _seed_label_chain(tmp_path)
    before = _nets(tgt)
    # Dry-run so the (net-changing) move is evaluated without needing a clean
    # write: the in-memory verify still reflects the stranded labels.
    kw.apply(
        _oplist({"op": "move_component", "designator": "R1",
                 "x_mil": 5000, "y_mil": 3000}),  # no carry
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    after = _nets(tgt)
    assert not netdiff.diff(before, after).equivalent


def _seed_rc_wire(tmp_path: Path) -> Path:
    """R1.1 — C1.1 joined by a real pin-to-pin wire (net N1), plus GND labels."""
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "22222222-3333-4444-5555-666666666666") (paper "A4"))\n')
    rs = kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R",
             "designator": "R1", "x_mil": 2000, "y_mil": 1000, "value": "1k"},
            {"op": "place_component", "lib_id": "Device:C",
             "designator": "C1", "x_mil": 3000, "y_mil": 1000, "value": "100n"},
            {"op": "add_wire", "vertices": ["R1.1", "C1.1"]},
            {"op": "add_net_label", "name": "GND", "at": "R1.2"},
            {"op": "add_net_label", "name": "GND", "at": "C1.2"},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    return tgt


def test_carry_wires_keeps_a_pin_to_pin_wire_connected(tmp_path):
    tgt = _seed_rc_wire(tmp_path)
    before = _nets(tgt)
    verify: list = []
    rs = kw.apply(
        _oplist({"op": "move_component", "designator": "R1",
                 "x_mil": 2000, "y_mil": 4000,
                 "carry_labels": True, "carry_wires": True}),
        str(tgt), apply=True, sources=[str(DEVICE)], verify_out=verify,
    )
    assert rs[0].status == "ok", rs[0].message
    assert not [f for f in verify if f.severity.value in ("error", "critical")]
    # The wire stretched to follow R1.1 but still ties R1.1—C1.1 (net intact).
    after = _nets(tgt)
    assert netdiff.diff(before, after).equivalent, netdiff.format_summary(
        netdiff.diff(before, after))
    assert "carried" in rs[0].message and "wire(s)" in rs[0].message


def test_plain_move_still_has_no_carry_message(tmp_path):
    """Backward-compat: a move without the flags behaves exactly as before."""
    tgt = _seed_label_chain(tmp_path)
    rs = kw.apply(
        _oplist({"op": "delete_object",
                 "match": {"kind": "label", "name": "VIN"}},
                {"op": "move_component", "designator": "R1",
                 "x_mil": 5000, "y_mil": 3000}),
        str(tgt), apply=False, sources=[str(DEVICE)],
    )
    move_res = rs[-1]
    assert move_res.op == "move_component"
    assert move_res.message == ""            # no carry note on a plain move
