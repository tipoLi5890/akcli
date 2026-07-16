"""End-to-end coverage for the KiCad write/draw half.

The round-trip test (draw an op-list -> re-read the written file) runs everywhere.
The ``kicad-cli`` test only runs where a real KiCad is installed (the CI KiCad job),
and confirms KiCad itself accepts the schematic ``akcli`` produced — closing the gap
that an Altium-only reviewer can't exercise.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from akcli.drivers import kicad_cli
from akcli.readers import kicad as kreader
from akcli.writers import kicad as kw

V8 = Path(__file__).parent / "fixtures" / "kicad" / "board_v8.kicad_sch"
DEVICE = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym"


def _oplist(*ops):
    return {"protocol_version": 1, "target_format": "kicad", "ops": list(ops)}


def _seed(tmp_path: Path) -> Path:
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_bytes(V8.read_bytes())
    return tgt


def _draw_derived_symbol(tmp_path: Path) -> Path:
    """Place an ``(extends)``-derived C_Polarized wired pin-to-pin to an R."""
    tgt = _seed(tmp_path)
    results = kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:C_Polarized",
             "designator": "C77", "x_mil": 4000, "y_mil": 4000, "value": "10u"},
            {"op": "place_component", "lib_id": "Device:R",
             "designator": "R77", "x_mil": 4000, "y_mil": 4600, "value": "1k"},
            {"op": "add_wire", "vertices": ["C77.2", "R77.1"]},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert all(r.status == "ok" for r in results)
    return tgt


def test_draw_then_reread_roundtrip(tmp_path):
    """draw -> apply -> re-read: the placed part is present and the file re-parses."""
    tgt = _seed(tmp_path)
    results = kw.apply(
        _oplist({"op": "place_component", "lib_id": "Device:C",
                 "designator": "C99", "x_mil": 4000, "y_mil": 4000, "value": "100n"}),
        str(tgt), apply=True,
    )
    assert all(r.status == "ok" for r in results)

    sch = kreader.read_sch(str(tgt))
    assert "C99" in {c.designator for c in sch.components}


def test_drawn_file_accepted_by_kicad_cli(tmp_path):
    """A real ``kicad-cli`` (CI KiCad job) must accept the file akcli wrote."""
    if not kicad_cli.available():
        pytest.skip("kicad-cli not installed (runs in the CI KiCad job)")
    tgt = _seed(tmp_path)
    kw.apply(
        _oplist({"op": "place_component", "lib_id": "Device:R",
                 "designator": "R99", "x_mil": 5000, "y_mil": 5000, "value": "10k"}),
        str(tgt), apply=True,
    )
    # KiCad's own ERC must parse the file we wrote (returns a report dict, not a crash).
    report = kicad_cli.erc(str(tgt))
    assert report is not None


# --------------------------------------------------------------------------- #
# extends-derived symbols must be flattened into the cache (regression)
# --------------------------------------------------------------------------- #
def test_derived_symbol_cache_is_flattened(tmp_path):
    """The written cache carries ONE standalone entry: no extends, no base.

    Regression: an unflattened ``(extends "Base")`` cache entry loses its pins
    in eeschema (KiCad does not resolve the bare base name against the
    qualified cached ``Device:Base``), leaving every wire to the part dangling.
    """
    tgt = _draw_derived_symbol(tmp_path)
    text = tgt.read_text(encoding="utf-8")
    assert "(extends" not in text
    # The derived entry exists and its unit sub-symbols are renamed to the
    # derived name (an AP1117-style leftover base unit name inside the entry is
    # exactly the broken case). The fixture's own plain Device:C cache entry
    # legitimately keeps its C_0_1/C_1_1 names, so scope the check to the block.
    from akcli.readers import sexpr

    libsyms = sexpr.parse(text).find("lib_symbols")
    derived = next(
        s for s in libsyms.find_all("symbol")
        if s.children[1].value == "Device:C_Polarized"
    )
    unit_names = {s.children[1].value for s in derived.find_all("symbol")}
    assert unit_names == {"C_Polarized_0_1", "C_Polarized_1_1"}

    # And the re-read netlist agrees the derived part's pins are connected.
    sch = kreader.read_sch(str(tgt))
    members = {tuple(m) for net in sch.nets for m in net.members}
    assert ("C77", "2") in members and ("R77", "1") in members


def test_derived_symbol_pins_connect_in_kicad_erc(tmp_path):
    """KiCad itself must see the derived part's pins on the wire.

    The broken (unflattened) cache made KiCad drop the derived symbol's pins:
    ERC reported ``unconnected_wire_endpoint`` on every wire to the part and the
    exported netlist omitted it entirely. We assert both KiCad-visible
    signatures. (``lib_symbol_mismatch`` is NOT asserted — the repo's minimal
    fixture symbols legitimately differ from any installed official library.)
    """
    if not kicad_cli.available():
        pytest.skip("kicad-cli not installed (runs in the CI KiCad job)")
    tgt = _draw_derived_symbol(tmp_path)

    wrapper = kicad_cli.erc(str(tgt))
    report = wrapper.get("report") if isinstance(wrapper, dict) else None
    if not isinstance(report, dict) or "sheets" not in report:
        pytest.skip("kicad-cli produced no JSON ERC report (KiCad < 8 fallback)")
    types = {
        v.get("type")
        for sheet in report.get("sheets", [])
        for v in sheet.get("violations", [])
    }
    assert "unconnected_wire_endpoint" not in types

    # Strongest, environment-independent proof: KiCad's own netlist export
    # places the derived part's wired pin on a net.
    net = kicad_cli.netlist(str(tgt))
    text = (net or {}).get("netlist") or ""
    assert '(ref "C77")' in text
    assert re.search(r'\(ref "C77"\)\s*\(pin "2"\)', text)


# --------------------------------------------------------------------------- #
# duplicated pin numbers across units (shared pads, e.g. dual DirectFETs)
# --------------------------------------------------------------------------- #
_SHARED_PAD_LIB = """
(kicad_symbol_lib (version 20231120) (generator "test")
  (symbol "DUALFET" (pin_numbers (hide yes)) (in_bom yes) (on_board yes)
    (property "Reference" "Q" (at 0 0 0))
    (symbol "DUALFET_1_1"
      (pin passive line (at -5.08 2.54 0) (length 2.54)
        (name "G1" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at -5.08 0 0) (length 2.54)
        (name "S1" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27))))))
    (symbol "DUALFET_2_1"
      (pin passive line (at -5.08 2.54 0) (length 2.54)
        (name "G2" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at -5.08 0 0) (length 2.54)
        (name "S2" (effects (font (size 1.27 1.27))))
        (number "3" (effects (font (size 1.27 1.27))))))))
"""


def test_placed_instance_carries_only_its_units_pins(tmp_path):
    """A placed instance exposes ITS unit's pins only (eeschema semantics).

    Emitting every unit's pins mapped all units onto one body: akcli merged
    unrelated gate pins into one net while eeschema saw two, and phantom pin
    points masked real dangles in the verifier."""
    lib = tmp_path / "dual.kicad_sym"
    lib.write_text(_SHARED_PAD_LIB)
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "11111111-2222-3333-4444-555555555555") (paper "A4"))\n')
    verify = []
    results = kw.apply(
        _oplist({"op": "place_component", "lib_id": "DUALFET",
                 "designator": "Q1", "x_mil": 2000, "y_mil": 2000}),
        str(tgt), apply=True, sources=[str(lib)], verify_out=verify,
    )
    assert results[0].status == "ok"
    assert not verify
    text = tgt.read_text()
    pins = re.findall(r'\(pin "(\d+)" \(uuid "([0-9a-f-]+)"\)\)', text)
    assert [n for n, _ in pins] == ["1", "2"]      # unit-1 pins only
    assert "(unit 1)" in text


def test_place_second_unit_and_wire_across_units(tmp_path):
    """`place_component` with "unit": 2 places gate B; REF.PIN then resolves
    per-unit, and both instances share the designator without a BOM dup."""
    lib = tmp_path / "dual.kicad_sym"
    lib.write_text(_SHARED_PAD_LIB)
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "11111111-2222-3333-4444-555555555555") (paper "A4"))\n')
    verify = []
    results = kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "DUALFET",
             "designator": "Q1", "x_mil": 2000, "y_mil": 2000},
            {"op": "place_component", "lib_id": "DUALFET",
             "designator": "Q1", "x_mil": 2000, "y_mil": 3000, "unit": 2},
            {"op": "add_wire", "vertices": ["Q1.2", "Q1.3"]},   # unit1 S1 -> unit2 S2
        ),
        str(tgt), apply=True, sources=[str(lib)], verify_out=verify,
    )
    assert [r.status for r in results] == ["ok", "ok", "ok"]
    assert not verify
    text = tgt.read_text()
    assert "(unit 1)" in text and "(unit 2)" in text
    # ONE merged component, pins from both units, wired across units.
    sch = kreader.read_sch(str(tgt))
    q1 = [c for c in sch.components if c.designator == "Q1"]
    assert len(q1) == 1
    assert sorted(p.number for p in q1[0].pins) == ["1", "1", "2", "3"]
    members = {tuple(m) for net in sch.nets for m in net.members}
    assert ("Q1", "2") in members and ("Q1", "3") in members


def test_wiring_a_pin_on_an_unplaced_unit_fails_loudly(tmp_path):
    """A pin living on an unplaced unit must refuse, not snap to another body."""
    lib = tmp_path / "dual.kicad_sym"
    lib.write_text(_SHARED_PAD_LIB)
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "11111111-2222-3333-4444-555555555555") (paper "A4"))\n')
    results = kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "DUALFET",
             "designator": "Q1", "x_mil": 2000, "y_mil": 2000},
            {"op": "add_wire", "vertices": ["Q1.3", [2400, 2000]]},  # pin 3 = unit 2
        ),
        str(tgt), apply=True, sources=[str(lib)],
    )
    assert results[1].status == "error"
    assert results[1].error_code == "VERIFY_FAILED"
    assert "unit 2" in results[1].message


# --------------------------------------------------------------------------- #
# auto-junction under a pin tapping a wire mid-span (eeschema semantics)
# --------------------------------------------------------------------------- #
def test_pin_on_wire_midspan_gets_auto_junction(tmp_path):
    """A placed pin whose tip lands on a wire's INTERIOR must get a junction.

    eeschema connects a pin tap only at a wire endpoint or a junction; without
    this, akcli's own netlist claimed connectivity that dangled in KiCad (the
    PWR_FLAG-on-a-rail case)."""
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "11111111-2222-3333-4444-555555555555") (paper "A4"))\n')
    # Fixture Device:R pins point up/down 150 mil from the body. Place R at
    # (2000,1000): pin 1 tip = (2000, 850). Wire passes THROUGH that point.
    results = kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R",
             "designator": "R1", "x_mil": 2000, "y_mil": 1000, "value": "1k"},
            {"op": "add_wire", "vertices": [[1800, 850], [2200, 850]]},
            {"op": "add_net_label", "name": "A", "at": [1800, 850], "scope": "global"},
            {"op": "add_net_label", "name": "A", "at": [2200, 850], "scope": "global"},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert all(r.status == "ok" for r in results)
    text = tgt.read_text()
    # 2000 mil = 50.8 mm, 850 mil = 21.59 mm
    assert re.search(r'\(junction \(at 50\.8 21\.59\)', text), "no auto-junction at the tap"
    # and the re-read netlist agrees the pin is on net A
    sch = kreader.read_sch(str(tgt))
    a = next(n for n in sch.nets if n.name == "A")
    assert ("R1", "1") in {tuple(m) for m in a.members}


# --------------------------------------------------------------------------- #
# byte idempotency: ONE apply must already be the fixed point
# --------------------------------------------------------------------------- #
def test_reapply_is_byte_identical_after_first_apply(tmp_path):
    """Replaying the same op-list must not reorder the document.

    Regression: idempotent replay used remove-then-APPEND, migrating every op
    node to the end while auto-junctions stayed put — the first re-apply
    reshuffled the file and byte-identity only converged on the second apply."""
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "11111111-2222-3333-4444-555555555555") (paper "A4"))\n')
    ops = _oplist(
        {"op": "place_component", "lib_id": "Device:R",
         "designator": "R1", "x_mil": 2000, "y_mil": 1000, "value": "1k"},
        {"op": "place_component", "lib_id": "Device:C",
         "designator": "C1", "x_mil": 2400, "y_mil": 1000, "value": "100n"},
        # T meet at (2000,850) -> exercises an auto-junction, the node class
        # that exposed the reorder.
        {"op": "add_wire", "vertices": ["R1.1", [2400, 850], "C1.1"]},
        {"op": "add_wire", "vertices": [[2000, 700], [2000, 850]]},
        {"op": "add_net_label", "name": "T", "at": [2000, 700], "scope": "global"},
        {"op": "add_text", "text": "idempotency probe", "at": [1500, 600]},
    )
    kw.apply(ops, str(tgt), apply=True, sources=[str(DEVICE)])
    first = tgt.read_bytes()
    kw.apply(ops, str(tgt), apply=True, sources=[str(DEVICE)])
    assert tgt.read_bytes() == first, "first re-apply is not byte-identical"


# --------------------------------------------------------------------------- #
# delete / move ops
# --------------------------------------------------------------------------- #
def _seed_rc(tmp_path):
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "11111111-2222-3333-4444-555555555555") (paper "A4"))\n')
    kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R",
             "designator": "R1", "x_mil": 2000, "y_mil": 1000, "value": "1k"},
            {"op": "place_component", "lib_id": "Device:C",
             "designator": "C1", "x_mil": 2400, "y_mil": 1000, "value": "100n"},
            {"op": "add_wire", "vertices": ["R1.1", [2400, 850], "C1.1"]},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    return tgt


def test_delete_component_removes_all_instances(tmp_path):
    tgt = _seed_rc(tmp_path)
    verify = []
    rs = kw.apply(
        _oplist({"op": "delete_component", "designator": "C1"}),
        str(tgt), apply=True, sources=[str(DEVICE)], verify_out=verify,
    )
    # The delete succeeded but the write is REFUSED: the wire into C1.1 is now
    # dangling — stale wiring must be cleaned up explicitly, never silently.
    assert rs[0].status == "ok"
    assert any(f.code == "DANGLING_ENDPOINT" for f in verify)
    sch = kreader.read_sch(str(tgt))
    assert "C1" in {c.designator for c in sch.components}  # refused -> untouched

    # Deleting the wire too makes the edit self-consistent -> applies.
    sch0 = kreader.read_sch(str(tgt))
    rs = kw.apply(
        _oplist({"op": "delete_component", "designator": "C1"},
                {"op": "delete_object", "uuid": _wire_uuid(tgt)}),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert [r.status for r in rs] == ["ok", "ok"]
    sch = kreader.read_sch(str(tgt))
    assert "C1" not in {c.designator for c in sch.components}
    assert "R1" in {c.designator for c in sch.components}


def _wire_uuid(tgt) -> str:
    text = tgt.read_text()
    m = re.search(r'\(wire .*?\(uuid "([0-9a-f-]+)"\)\)', text)
    return m.group(1)


def test_delete_absent_component_is_replay_safe_noop(tmp_path):
    tgt = _seed_rc(tmp_path)
    rs = kw.apply(
        _oplist({"op": "delete_component", "designator": "R99"}),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert rs[0].status == "ok"
    assert "already absent" in rs[0].message


def test_move_component_carries_its_properties(tmp_path):
    tgt = _seed_rc(tmp_path)
    rs = kw.apply(
        # Move R1 clear of the wire AND delete the wire so nothing dangles.
        _oplist({"op": "delete_object", "uuid": _wire_uuid(tgt)},
                {"op": "move_component", "designator": "R1",
                 "x_mil": 3000, "y_mil": 2000}),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert [r.status for r in rs] == ["ok", "ok"]
    sch = kreader.read_sch(str(tgt))
    r1 = next(c for c in sch.components if c.designator == "R1")
    assert (r1.x_mil, r1.y_mil) == (3000, 2000)
    # Reference/Value moved WITH the body (same delta), not left behind.
    text = tgt.read_text()
    sym = text[text.index('"Device:R"'):]
    m = re.search(r'\(property "Reference" "R1"\s*\(at ([\d.]+) ([\d.]+)', sym)
    assert m, "no Reference property"
    # body moved +1000mil x (25.4mm) / +1000mil y; ref must be near the new spot
    assert abs(float(m.group(1)) - 76.2) < 26 and abs(float(m.group(2)) - 50.8) < 26


def test_move_missing_component_fails_loudly(tmp_path):
    tgt = _seed_rc(tmp_path)
    rs = kw.apply(
        _oplist({"op": "move_component", "designator": "R99",
                 "x_mil": 1000, "y_mil": 1000}),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert rs[0].status == "error"
    assert rs[0].error_code == "VERIFY_FAILED"


# --------------------------------------------------------------------------- #
# autoplace collision avoidance
# --------------------------------------------------------------------------- #
def test_adjacent_labels_do_not_stack(tmp_path):
    """A power port's value next to a cap's ref must not share an anchor spot.

    Regression for the LDO render where '+3V3' sat on top of 'C2': visible
    Reference/Value anchors of neighboring parts must keep at least one label
    extent apart (deterministic bump, so replays stay byte-identical)."""
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "11111111-2222-3333-4444-555555555555") (paper "A4"))\n')
    ops = _oplist(
        {"op": "place_component", "lib_id": "Device:C", "designator": "C1",
         "x_mil": 2000, "y_mil": 1000, "value": "10u"},
        {"op": "place_component", "lib_id": "Device:C", "designator": "C2",
         "x_mil": 2100, "y_mil": 1000, "value": "10u"},   # crowds C1's labels
    )
    kw.apply(ops, str(tgt), apply=True, sources=[str(DEVICE)])
    text = tgt.read_text()
    import re as _re
    anchors = []
    for m in _re.finditer(
            r'\(property "(Reference|Value)" "[^"]*"\s*\(at ([\d.-]+) ([\d.-]+)',
            text):
        anchors.append((m.group(1), float(m.group(2)), float(m.group(3))))
    vis = [(x, y) for (_k, x, y) in anchors]
    for i in range(len(vis)):
        for j in range(i + 1, len(vis)):
            dx = abs(vis[i][0] - vis[j][0])
            dy = abs(vis[i][1] - vis[j][1])
            assert dx >= 2.54 - 1e-6 or dy >= 1.27 - 1e-6, (
                f"anchors {vis[i]} and {vis[j]} stack (dx={dx:.2f}, dy={dy:.2f})"
            )

    # and the bump is deterministic: replay stays byte-identical
    first = tgt.read_bytes()
    kw.apply(ops, str(tgt), apply=True, sources=[str(DEVICE)])
    assert tgt.read_bytes() == first


# --------------------------------------------------------------------------- #
# --no-erc: the advisory post-apply kicad-cli run is skipped, honestly
# --------------------------------------------------------------------------- #
def test_draw_no_erc_skips_advisory_run(tmp_path, monkeypatch, capsys):
    import json as _json

    from akcli.cli import main as _main
    from akcli.drivers import kicad_cli as _kc

    target = tmp_path / "board.kicad_sch"
    assert _main(["new", str(target)]) == 0
    ops = {"protocol_version": 1, "target_format": "kicad",
           "target_file": "board.kicad_sch",
           "ops": [{"op": "add_text", "at": [1000, 1000], "text": "x"}]}
    (tmp_path / "ops.json").write_text(_json.dumps(ops), encoding="utf-8")

    calls = []
    monkeypatch.setattr(_kc, "available", lambda: (calls.append("probe"), True)[1])
    monkeypatch.setattr(_kc, "erc", lambda p: calls.append("erc") or {})

    assert _main(["draw", str(target), "--ops", str(tmp_path / "ops.json"),
                  "--apply", "--no-erc"]) == 0
    assert calls == []  # neither probed nor run

    assert _main(["draw", str(target), "--ops", str(tmp_path / "ops.json"),
                  "--apply"]) == 0
    assert "erc" in calls  # without the flag the advisory run happens
