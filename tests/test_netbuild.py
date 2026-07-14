"""Tests for netbuild.build_nets (SPEC §3.3).

These construct ``model.NetPrimitives`` DIRECTLY (synthetic wires / junctions /
labels / pins) — they do NOT depend on the Altium reader, which does not exist
in this phase. Coordinates mirror the committed reviewable fixtures
(``tests/fixtures/*.records.txt`` + ``*.expected.json``); the pin ``.at`` points
are the already-computed pin tips the reader would emit.

Foundation (model.py) is imported but never edited.
"""

from __future__ import annotations

from akcli import model
from akcli.netbuild import build_nets


# --- helpers -----------------------------------------------------------------

def _wire(ax, ay, bx, by, sheet=""):
    return model.WireSeg(a=(ax, ay), b=(bx, by), sheet=sheet)


def _pin(ref, x, y, sheet=""):
    return model.PinHandle(ref=ref, at=(x, y), sheet=sheet)


def _label(text, x, y, scope="local", sheet=""):
    return model.NetLabel(at=(x, y), text=text, scope=scope, sheet=sheet)


def _junction(x, y, sheet=""):
    return model.Junction(at=(x, y), sheet=sheet)


def _members(net):
    return sorted(net.members)


def _net_with(nets, member):
    return [n for n in nets if member in n.members]


# --- (5) shared-name merge: the STAT fix ------------------------------------

def test_shared_name_label_stitches_two_disjoint_clusters():
    """Two disjoint wire clusters carry the SAME label Text 'STAT' -> ONE net."""
    prims = model.NetPrimitives(
        wires=[
            _wire(1000, 1000, 2000, 1000),  # cluster 1
            _wire(1000, 2000, 2000, 2000),  # cluster 2 (geometrically disjoint)
        ],
        pins=[
            _pin(("U2", "1"), 1000, 1000),
            _pin(("R7", "1"), 2000, 1000),
            _pin(("U3", "2"), 1000, 2000),
            _pin(("R12", "1"), 2000, 2000),
        ],
        labels=[
            _label("STAT", 1500, 1000),
            _label("STAT", 1500, 2000),
        ],
    )
    nets = build_nets(prims)

    assert len(nets) == 1, "same Text must collapse the two clusters into ONE net"
    net = nets[0]
    assert _members(net) == [("R12", "1"), ("R7", "1"), ("U2", "1"), ("U3", "2")]
    assert net.name == "STAT"
    assert net.is_named is True
    # single distinct name -> full confidence, no aliases.
    assert net.confidence == 1.0
    assert net.aliases == []
    # the cross-cluster stitch is explained.
    assert any("STAT" in r for r in net.merge_reasons)
    # zero single-pin nets in this scenario.
    assert all(len(n.members) > 1 for n in nets)


def test_distinct_names_on_one_net_become_aliases():
    """STAT ≡ LED1_GPIO_RD: two DIFFERENT labels on a connected cluster -> aliases."""
    prims = model.NetPrimitives(
        wires=[_wire(1000, 1000, 2000, 1000)],
        pins=[
            _pin(("U2", "1"), 1000, 1000),
            _pin(("U3", "1"), 2000, 1000),
        ],
        labels=[
            _label("STAT", 1300, 1000),
            _label("LED1_GPIO_RD", 1700, 1000),
        ],
    )
    nets = build_nets(prims)

    assert len(nets) == 1
    net = nets[0]
    assert _members(net) == [("U2", "1"), ("U3", "1")]
    assert {net.name, *net.aliases} == {"STAT", "LED1_GPIO_RD"}
    assert sorted(net.source_names) == ["LED1_GPIO_RD", "STAT"]
    # multi-name net -> confidence lowered + explained.
    assert net.confidence < 1.0
    assert net.merge_reasons


# --- (5) two same-name GND power ports --------------------------------------

def test_two_gnd_power_ports_collapse_to_one_net():
    prims = model.NetPrimitives(
        wires=[
            _wire(1000, 1000, 2000, 1000),
            _wire(1000, 2000, 2000, 2000),
        ],
        pins=[
            _pin(("U1", "1"), 1000, 1000),
            _pin(("U2", "1"), 2000, 1000),
            _pin(("U3", "1"), 1000, 2000),
            _pin(("U4", "1"), 2000, 2000),
        ],
        labels=[
            _label("GND", 1500, 1000, scope="power"),
            _label("GND", 1500, 2000, scope="power"),
        ],
    )
    nets = build_nets(prims)

    assert len(nets) == 1
    net = nets[0]
    assert net.name == "GND"
    assert _members(net) == [("U1", "1"), ("U2", "1"), ("U3", "1"), ("U4", "1")]
    assert net.aliases == []
    assert net.confidence == 1.0


# --- (2) junction(29) cross --------------------------------------------------

def test_junction_dot_merges_crossing_wires():
    """A RECORD-29 dot at a bare crossing merges both wires -> ONE net of 4."""
    prims = model.NetPrimitives(
        wires=[
            _wire(1000, 2000, 3000, 2000),  # horizontal
            _wire(2000, 1000, 2000, 3000),  # vertical
        ],
        junctions=[_junction(2000, 2000)],
        pins=[
            _pin(("U1", "1"), 1000, 2000),
            _pin(("U2", "1"), 3000, 2000),
            _pin(("U3", "1"), 2000, 1000),
            _pin(("U4", "1"), 2000, 3000),
        ],
    )
    nets = build_nets(prims)

    assert len(nets) == 1
    assert _members(nets[0]) == [
        ("U1", "1"), ("U2", "1"), ("U3", "1"), ("U4", "1")
    ]
    assert nets[0].is_named is False
    assert nets[0].name is None


def test_bare_crossing_without_dot_is_not_connected():
    """Same geometry MINUS the dot: a bare crossing is NOT a connection."""
    prims = model.NetPrimitives(
        wires=[
            _wire(1000, 2000, 3000, 2000),
            _wire(2000, 1000, 2000, 3000),
        ],
        # no junctions
        pins=[
            _pin(("U1", "1"), 1000, 2000),
            _pin(("U2", "1"), 3000, 2000),
            _pin(("U3", "1"), 2000, 1000),
            _pin(("U4", "1"), 2000, 3000),
        ],
    )
    nets = build_nets(prims)

    assert len(nets) == 2
    horiz = _net_with(nets, ("U1", "1"))[0]
    vert = _net_with(nets, ("U3", "1"))[0]
    assert _members(horiz) == [("U1", "1"), ("U2", "1")]
    assert _members(vert) == [("U3", "1"), ("U4", "1")]


# --- (3) T-junction ----------------------------------------------------------

def test_t_junction_endpoint_on_midspan_merges():
    """A wire endpoint on another wire's mid-span connects with NO explicit dot."""
    prims = model.NetPrimitives(
        wires=[
            _wire(1000, 2000, 3000, 2000),  # horizontal trunk
            _wire(2000, 2000, 2000, 4000),  # vertical, top vertex on the trunk mid-span
        ],
        pins=[
            _pin(("U1", "1"), 1000, 2000),
            _pin(("U2", "1"), 3000, 2000),
            _pin(("U3", "1"), 2000, 4000),
        ],
    )
    nets = build_nets(prims)

    assert len(nets) == 1
    assert _members(nets[0]) == [("U1", "1"), ("U2", "1"), ("U3", "1")]


# --- (4)/no-ERC: open pin remains its own single-pin net --------------------

def test_no_erc_open_pin_is_isolated_single_pin_net():
    """The open pin (with a No-ERC marker at its tip) stays its own single-pin net.

    netbuild does not suppress it from the netlist — the No-ERC point is carried
    on the primitives for the downstream ERC check to honor.
    """
    prims = model.NetPrimitives(
        wires=[_wire(1000, 1000, 2000, 1000)],
        pins=[
            _pin(("U1", "1"), 1000, 1000),
            _pin(("U2", "1"), 2000, 1000),
            _pin(("U1", "2"), 1000, 1500),  # deliberately open
        ],
        no_erc=[(1000, 1500)],
    )
    nets = build_nets(prims)

    assert len(nets) == 2
    connected = _net_with(nets, ("U1", "1"))[0]
    assert _members(connected) == [("U1", "1"), ("U2", "1")]
    open_net = _net_with(nets, ("U1", "2"))[0]
    assert _members(open_net) == [("U1", "2")]
    assert open_net.is_named is False
    # the No-ERC point is preserved for ERC suppression (untouched by netbuild).
    assert (1000, 1500) in prims.no_erc


def test_single_pin_nets_gated_off():
    """emit_single_pin_nets=False drops the open single-pin net."""
    prims = model.NetPrimitives(
        wires=[_wire(1000, 1000, 2000, 1000)],
        pins=[
            _pin(("U1", "1"), 1000, 1000),
            _pin(("U2", "1"), 2000, 1000),
            _pin(("U1", "2"), 1000, 1500),
        ],
        emit_single_pin_nets=False,
    )
    nets = build_nets(prims)
    assert len(nets) == 1
    assert _members(nets[0]) == [("U1", "1"), ("U2", "1")]


# --- (7) stable, coordinate-INDEPENDENT ids ---------------------------------

def test_stable_id_is_membership_not_coordinate_derived():
    """Same membership at different coordinates -> identical stable_id."""
    a = model.NetPrimitives(
        wires=[_wire(1000, 1000, 2000, 1000)],
        pins=[_pin(("U1", "1"), 1000, 1000), _pin(("U2", "1"), 2000, 1000)],
    )
    b = model.NetPrimitives(
        wires=[_wire(7000, 9000, 8000, 9000)],  # totally different coords
        pins=[_pin(("U1", "1"), 7000, 9000), _pin(("U2", "1"), 8000, 9000)],
    )
    na = build_nets(a)[0]
    nb = build_nets(b)[0]
    assert na.stable_id == nb.stable_id
    assert na.stable_id.startswith("net_")


def test_order_invariance_of_input_primitives():
    """Shuffled primitive order -> identical canonical membership."""
    pins = [
        _pin(("U2", "1"), 1000, 1000),
        _pin(("R7", "1"), 2000, 1000),
        _pin(("U3", "2"), 1000, 2000),
        _pin(("R12", "1"), 2000, 2000),
    ]
    base_kwargs = dict(
        wires=[_wire(1000, 1000, 2000, 1000), _wire(1000, 2000, 2000, 2000)],
        labels=[_label("STAT", 1500, 1000), _label("STAT", 1500, 2000)],
    )
    n1 = build_nets(model.NetPrimitives(pins=list(pins), **base_kwargs))
    n2 = build_nets(model.NetPrimitives(pins=list(reversed(pins)), **base_kwargs))
    assert [_members(n) for n in n1] == [_members(n) for n in n2]


# --- (8) multi-sheet union by Port/global name; local labels stay sheet-local

def test_multi_sheet_port_unions_across_sheets():
    """A global-scope Port name on two sheets joins both clusters into ONE net."""
    prims = model.NetPrimitives(
        wires=[
            _wire(1000, 1000, 2000, 1000, sheet="sheetA"),
            _wire(1000, 1000, 2000, 1000, sheet="sheetB"),
        ],
        pins=[
            _pin(("U1", "1"), 1000, 1000, sheet="sheetA"),
            _pin(("U2", "1"), 2000, 1000, sheet="sheetB"),
        ],
        labels=[
            _label("BUS", 1500, 1000, scope="port", sheet="sheetA"),
            _label("BUS", 1500, 1000, scope="port", sheet="sheetB"),
        ],
    )
    nets = build_nets(prims)
    assert len(nets) == 1
    assert nets[0].name == "BUS"
    assert _members(nets[0]) == [("U1", "1"), ("U2", "1")]


def test_local_labels_same_name_different_sheets_do_not_merge():
    """Net labels are sheet-LOCAL: same Text on two sheets are TWO nets."""
    prims = model.NetPrimitives(
        wires=[
            _wire(1000, 1000, 2000, 1000, sheet="sheetA"),
            _wire(1000, 1000, 2000, 1000, sheet="sheetB"),
        ],
        pins=[
            _pin(("U1", "1"), 1000, 1000, sheet="sheetA"),
            _pin(("U2", "1"), 2000, 1000, sheet="sheetB"),
        ],
        labels=[
            _label("SIG", 1500, 1000, scope="local", sheet="sheetA"),
            _label("SIG", 1500, 1000, scope="local", sheet="sheetB"),
        ],
    )
    nets = build_nets(prims)
    assert len(nets) == 2
    assert all(n.name == "SIG" for n in nets)
    assert sorted(_members(n) for n in nets) == [[("U1", "1")], [("U2", "1")]]


# --- naming priority: power port outranks net label when flag set -----------

def test_power_priority_flag_selects_power_port_name():
    prims = model.NetPrimitives(
        wires=[_wire(1000, 1000, 2000, 1000)],
        pins=[_pin(("U1", "1"), 1000, 1000), _pin(("U2", "1"), 2000, 1000)],
        labels=[
            _label("NET_LABEL_NAME", 1300, 1000, scope="local"),
            _label("V3V3", 1700, 1000, scope="power"),
        ],
        power_priority=True,
    )
    nets = build_nets(prims)
    assert len(nets) == 1
    assert nets[0].name == "V3V3"
    assert "NET_LABEL_NAME" in nets[0].aliases
    assert nets[0].confidence < 1.0


# --- pin taps: mid-span requires a junction (eeschema semantics) -------------

def test_pin_on_wire_midspan_without_junction_does_not_connect():
    """A pin tip touching a wire's INTERIOR with no junction stays floating.

    eeschema connects a pin only at a wire endpoint or a junction-marked point;
    treating a bare mid-span touch as connected made `akcli net` claim
    connectivity KiCad rejects (the PWR_FLAG-on-a-rail case).
    """
    prims = model.NetPrimitives(
        wires=[_wire(1000, 1000, 3000, 1000)],
        pins=[
            _pin(("R1", "1"), 1000, 1000),   # at wire endpoint -> connects
            _pin(("Q9", "1"), 2000, 1000),   # strictly mid-span -> floats
        ],
        labels=[_label("RAIL", 1000, 1000)],
    )
    nets = build_nets(prims)
    hits = _net_with(nets, ("R1", "1"))
    assert len(hits) == 1
    assert ("Q9", "1") not in hits[0].members


def test_pin_on_wire_midspan_with_junction_connects():
    """The same mid-span tap WITH a junction record joins the net."""
    prims = model.NetPrimitives(
        wires=[_wire(1000, 1000, 3000, 1000)],
        pins=[
            _pin(("R1", "1"), 1000, 1000),
            _pin(("Q9", "1"), 2000, 1000),
        ],
        junctions=[_junction(2000, 1000)],
        labels=[_label("RAIL", 1000, 1000)],
    )
    nets = build_nets(prims)
    hits = _net_with(nets, ("R1", "1"))
    assert len(hits) == 1
    assert ("Q9", "1") in hits[0].members
