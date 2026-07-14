"""Bus netlist semantics (stage 2) at the netbuild layer.

Every verdict here was arbitrated with a real ``kicad-cli sch export netlist``
(KiCad 10.x) before being locked — the end-to-end fixtures live in
``tests/test_kicad_parity.py`` (sections e/f); these tests restate the same
semantics directly against :func:`netbuild.build_nets` so they run with no
KiCad installed:

* a labeled bus carries its vector members (``D[0..7]`` -> ``D0..D7``,
  inclusive at both endpoints in either order);
* a (bus_entry) conducts between its two ends; each end attaches to a wire at
  a wire ENDPOINT or a junction-marked point — a bare mid-span touch does not
  attach (the ripped wire floats, verified);
* the bus member a rip joins is selected by the WIRE-side cluster's own
  label; an unlabeled rip stays unconnected to members; a plain label sitting
  ON the bus selects nothing;
* bus clusters merge by name with net-label scope rules: local bus names are
  sheet-scoped, global bus names cross sheets (THE case plain label merging
  cannot reproduce), and hier connectors stitch parent sheet-pin buses to the
  child's hierarchical bus label;
* bus-to-bus continuation through a shared endpoint extends the labeled bus.
"""

from __future__ import annotations

from akcli import model
from akcli.netbuild import build_nets, expand_bus_vector


# --------------------------------------------------------------------------- #
# vector expansion
# --------------------------------------------------------------------------- #
def test_expand_vector_forward():
    assert expand_bus_vector("D[0..7]") == [f"D{i}" for i in range(8)]


def test_expand_vector_reverse_inclusive():
    assert expand_bus_vector("K[3..0]") == ["K3", "K2", "K1", "K0"]


def test_expand_vector_single_and_prefix():
    assert expand_bus_vector("A_[5..5]") == ["A_5"]


def test_expand_vector_non_vector_is_empty():
    assert expand_bus_vector("CTRL") == []
    assert expand_bus_vector("D[0..7") == []
    assert expand_bus_vector("D[a..b]") == []


# --------------------------------------------------------------------------- #
# primitive builders
# --------------------------------------------------------------------------- #
def _bus(prims, a, b, sheet=""):
    prims.buses.append(model.WireSeg(a=a, b=b, sheet=sheet))


def _bus_label(prims, text, at, sheet="", scope="local"):
    prims.labels.append(model.NetLabel(at=at, text=text, scope=scope, sheet=sheet))


def _rip(prims, ref, bus_at, label=None, sheet=""):
    """entry at ``bus_at`` -> wire 600 mil right -> pin; optional wire label."""
    x, y = bus_at
    fx, fy = x + 100, y + 100
    prims.bus_entries.append(model.BusEntry(a=(x, y), b=(fx, fy), sheet=sheet))
    prims.wires.append(model.WireSeg(a=(fx, fy), b=(fx + 600, fy), sheet=sheet))
    if label is not None:
        prims.labels.append(
            model.NetLabel(at=(fx + 300, fy), text=label, scope="local", sheet=sheet)
        )
    prims.pins.append(
        model.PinHandle(ref=(ref, "1"), at=(fx + 600, fy), sheet=sheet)
    )


def _members(nets):
    return {frozenset(n.members) for n in nets}


def _net_of(nets, ref):
    return next(n for n in nets if (ref, "1") in n.members)


def _build(prims):
    return build_nets(prims, t_midspan_connects=False)


# --------------------------------------------------------------------------- #
# member selection on one sheet
# --------------------------------------------------------------------------- #
def test_unlabeled_rip_stays_unconnected():
    prims = model.NetPrimitives()
    _bus(prims, (100, 0), (100, 1000))
    _bus_label(prims, "D[0..7]", (100, 100))
    _rip(prims, "R1", (100, 300), "D3")
    _rip(prims, "R2", (100, 600), None)
    nets = _build(prims)
    assert frozenset({("R2", "1")}) in _members(nets)
    assert _net_of(nets, "R1").name == "D3"


def test_plain_label_on_bus_selects_no_member():
    # "F1" sits ON the bus, not on the ripped wire: eeschema leaves the
    # unlabeled rip floating and does not merge it with a detached F1 wire.
    prims = model.NetPrimitives()
    _bus(prims, (100, 0), (100, 1000))
    _bus_label(prims, "F[0..3]", (100, 100))
    _bus_label(prims, "F1", (100, 800))
    _rip(prims, "R7", (100, 300), None)
    prims.wires.append(model.WireSeg(a=(2000, 500), b=(2600, 500)))
    prims.labels.append(model.NetLabel(at=(2000, 500), text="F1", scope="local"))
    prims.pins.append(model.PinHandle(ref=("R8", "1"), at=(2600, 500)))
    nets = _build(prims)
    assert frozenset({("R7", "1")}) in _members(nets)
    assert _net_of(nets, "R8").members == [("R8", "1")]


def test_rip_label_not_a_member_keeps_own_net():
    prims = model.NetPrimitives()
    _bus(prims, (100, 0), (100, 1000))
    _bus_label(prims, "D[0..7]", (100, 100))
    _rip(prims, "R1", (100, 300), "Z9")
    nets = _build(prims)
    net = _net_of(nets, "R1")
    assert net.name == "Z9" and net.members == [("R1", "1")]


# --------------------------------------------------------------------------- #
# bus entry conduction (wire layer)
# --------------------------------------------------------------------------- #
def test_entry_conducts_between_wire_endpoints():
    prims = model.NetPrimitives()
    prims.wires.append(model.WireSeg(a=(0, 0), b=(500, 0)))
    prims.pins.append(model.PinHandle(ref=("R11", "1"), at=(0, 0)))
    prims.bus_entries.append(model.BusEntry(a=(500, 0), b=(600, 100)))
    prims.wires.append(model.WireSeg(a=(600, 100), b=(1000, 100)))
    prims.pins.append(model.PinHandle(ref=("R12", "1"), at=(1000, 100)))
    nets = _build(prims)
    assert frozenset({("R11", "1"), ("R12", "1")}) in _members(nets)


def test_entry_end_on_wire_midspan_does_not_attach():
    prims = model.NetPrimitives()
    prims.wires.append(model.WireSeg(a=(0, 0), b=(1000, 0)))
    prims.pins.append(model.PinHandle(ref=("R21", "1"), at=(0, 0)))
    prims.pins.append(model.PinHandle(ref=("R23", "1"), at=(1000, 0)))
    prims.bus_entries.append(model.BusEntry(a=(500, 0), b=(600, 100)))
    prims.wires.append(model.WireSeg(a=(600, 100), b=(1200, 100)))
    prims.pins.append(model.PinHandle(ref=("R22", "1"), at=(1200, 100)))
    nets = _build(prims)
    parts = _members(nets)
    assert frozenset({("R21", "1"), ("R23", "1")}) in parts
    assert frozenset({("R22", "1")}) in parts


def test_entry_end_on_wire_midspan_with_junction_attaches():
    prims = model.NetPrimitives()
    prims.wires.append(model.WireSeg(a=(0, 0), b=(1000, 0)))
    prims.pins.append(model.PinHandle(ref=("R21", "1"), at=(0, 0)))
    prims.junctions.append(model.Junction(at=(500, 0)))
    prims.bus_entries.append(model.BusEntry(a=(500, 0), b=(600, 100)))
    prims.wires.append(model.WireSeg(a=(600, 100), b=(1200, 100)))
    prims.pins.append(model.PinHandle(ref=("R22", "1"), at=(1200, 100)))
    nets = _build(prims)
    assert frozenset({("R21", "1"), ("R22", "1")}) in _members(nets)


# --------------------------------------------------------------------------- #
# bus label scope across sheets — the semantics plain label-merging misses
# --------------------------------------------------------------------------- #
def _two_sheet_bus(scope):
    prims = model.NetPrimitives()
    for sheet, ref in (("", "R1"), ("/c", "R2")):
        _bus(prims, (100, 0), (100, 1000), sheet)
        _bus_label(prims, "D[0..7]", (100, 100), sheet, scope=scope)
        _rip(prims, ref, (100, 500), "D3", sheet)
    return _build(prims)


def test_global_bus_label_merges_members_across_sheets():
    nets = _two_sheet_bus("global")
    net = _net_of(nets, "R1")
    assert sorted(net.members) == [("R1", "1"), ("R2", "1")]
    assert net.name == "D3"
    assert any("bus member merge: D3" in r for r in net.merge_reasons)


def test_local_bus_label_stays_sheet_local():
    nets = _two_sheet_bus("local")
    parts = _members(nets)
    assert frozenset({("R1", "1")}) in parts
    assert frozenset({("R2", "1")}) in parts


def test_reversed_vector_endpoints_merge_across_sheets():
    prims = model.NetPrimitives()
    for sheet, hi, lo in (("", "R7", "R9"), ("/c", "R8", "R10")):
        _bus(prims, (100, 0), (100, 2000), sheet)
        _bus_label(prims, "K[3..0]", (100, 100), sheet, scope="global")
        _rip(prims, hi, (100, 500), "K3", sheet)
        _rip(prims, lo, (100, 1200), "K0", sheet)
    parts = _members(_build(prims))
    assert frozenset({("R7", "1"), ("R8", "1")}) in parts
    assert frozenset({("R9", "1"), ("R10", "1")}) in parts


def test_bus_continuation_through_shared_endpoint():
    # Sheet "": label on segment 1 only, rip off segment 2 — continuation
    # must carry the members (observable because the merge is cross-sheet).
    prims = model.NetPrimitives()
    _bus(prims, (100, 0), (100, 500))
    _bus(prims, (100, 500), (100, 1000))
    _bus_label(prims, "H[0..3]", (100, 100), scope="global")
    _rip(prims, "R13", (100, 700), "H2")
    _bus(prims, (100, 0), (100, 1000), "/c")
    _bus_label(prims, "H[0..3]", (100, 100), "/c", scope="global")
    _rip(prims, "R14", (100, 500), "H2", "/c")
    nets = _build(prims)
    assert sorted(_net_of(nets, "R13").members) == [("R13", "1"), ("R14", "1")]


def test_disconnected_buses_do_not_merge_without_shared_name():
    # Two differently-named global buses: D3 rip vs E3 rip stay apart.
    prims = model.NetPrimitives()
    _bus(prims, (100, 0), (100, 1000))
    _bus_label(prims, "D[0..7]", (100, 100), scope="global")
    _rip(prims, "R1", (100, 500), "D3")
    _bus(prims, (100, 0), (100, 1000), "/c")
    _bus_label(prims, "E[0..7]", (100, 100), "/c", scope="global")
    _rip(prims, "R2", (100, 500), "D3", "/c")
    parts = _members(_build(prims))
    assert frozenset({("R1", "1")}) in parts
    assert frozenset({("R2", "1")}) in parts


def test_hier_connector_stitches_parent_and_child_bus():
    # Parent: bus runs to the sheet pin (synthetic hier connector on the bus).
    # Child: hierarchical label = hier connector + local vector label on ITS
    # bus (exactly what readers/kicad.py emits). P1 rips merge parent<->child.
    key = "\x02hier:/s1:P[0..3]"
    prims = model.NetPrimitives()
    _bus(prims, (0, 200), (500, 200))
    _bus_label(prims, key, (500, 200), scope="hier")
    _rip(prims, "R3", (200, 200), "P1")
    _bus(prims, (0, 200), (0, 800), "/s1")
    _bus_label(prims, key, (0, 200), "/s1", scope="hier")
    _bus_label(prims, "P[0..3]", (0, 200), "/s1", scope="local")
    _rip(prims, "R4", (0, 500), "P1", "/s1")
    nets = _build(prims)
    assert sorted(_net_of(nets, "R3").members) == [("R3", "1"), ("R4", "1")]


# --------------------------------------------------------------------------- #
# wires never join a bus directly
# --------------------------------------------------------------------------- #
def test_wire_endpoint_on_bus_does_not_connect():
    prims = model.NetPrimitives()
    _bus(prims, (100, 0), (100, 1000))
    _bus_label(prims, "D[0..7]", (100, 100))
    _rip(prims, "R1", (100, 300), "D3")
    # wire labeled D3 ENDING exactly on the bus — no entry, no connection
    # beyond the plain same-sheet label merge (which already joins D3 by name;
    # assert the un-labeled variant instead to isolate the geometric rule).
    prims.wires.append(model.WireSeg(a=(100, 800), b=(700, 800)))
    prims.pins.append(model.PinHandle(ref=("R9", "1"), at=(700, 800)))
    nets = _build(prims)
    assert frozenset({("R9", "1")}) in _members(nets)
