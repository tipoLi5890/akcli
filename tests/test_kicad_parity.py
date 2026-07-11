"""eeschema ground-truth parity for the connectivity core (SPEC §3.3/§3.4).

Fixtures are authored programmatically — through the writers API wherever the
writer can express them, and through a tiny textual builder for the one shape
the writer refuses to produce (a T-touch WITHOUT its junction dot;
``auto_junctions`` always inserts the dot). Each fixture is exported through a
real ``kicad-cli sch export netlist`` and the resulting net -> (ref, pin)
partition is asserted set-equal to what ``netbuild.build_nets`` derives from
the akcli reader. Tests needing the binary skip when no kicad-cli is
installed; the transform truth-table tests always run (the table itself was
established with kicad-cli 10.0.4 and is restated here as data).

Ground-truth verdicts these tests lock (kicad-cli 10.0.4):

* **Transform**: a file angle of +90 rotates a symbol counter-clockwise ON
  SCREEN — ``(x, y) -> (y, -x)`` in the +Y-down frame — then the mirror is
  applied (``x``: negate Y, ``y``: negate X). Neither of akcli's two former
  transforms matched: the writer rotated the other way, the reader
  mirror-then-rotated.
* **Label scope**: a local label DOES merge with a same-name global label or
  power port anchored on the SAME sheet even when physically disconnected
  (netbuild rule 5b), and NEVER merges across sheets (a child's local ``X``
  netlists as ``/child/X``). Name comparison here is sheet-path-aware: an
  eeschema net ``/X`` is the root-sheet-local X and is never collapsed onto a
  global ``X``.
* **T-touch**: a wire END on another wire's mid-span connects ONLY through an
  explicit junction node; shared endpoints (any count) connect bare.
* **Pin mid-span**: a pin tip on a wire's interior connects only via a
  junction; a label connects anywhere along the wire.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid as _uuid
from pathlib import Path

import pytest

from altium_kicad_cli import config as config_mod
from altium_kicad_cli import model, units
from altium_kicad_cli.checks import nets as netcheck
from altium_kicad_cli.readers import kicad as kreader
from altium_kicad_cli.readers import sexpr
from altium_kicad_cli.writers import geometry
from altium_kicad_cli.writers import kicad as kw


# --------------------------------------------------------------------------- #
# kicad-cli discovery (PATH, then the macOS / Linux app locations)
# --------------------------------------------------------------------------- #
def _find_kicad_cli() -> str | None:
    hit = shutil.which("kicad-cli")
    if hit:
        return hit
    for cand in (
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        "/usr/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
    ):
        if Path(cand).is_file():
            return cand
    return None


KICAD_CLI = _find_kicad_cli()
needs_kicad = pytest.mark.skipif(
    KICAD_CLI is None, reason="kicad-cli not installed (runs in the CI KiCad job)"
)


def _export_nets(path: Path) -> dict[str, frozenset[tuple[str, str]]]:
    """eeschema's netlist for ``path``: {net_name: {(ref, pin), ...}}."""
    out = path.with_suffix(".net")
    proc = subprocess.run(
        [KICAD_CLI, "sch", "export", "netlist", "--output", str(out), str(path)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"kicad-cli failed: {proc.stderr}"
    root = sexpr.parse(out.read_text(encoding="utf-8"))
    nets: dict[str, frozenset[tuple[str, str]]] = {}
    for net in root.find("nets").find_all("net"):
        name = net.find("name").children[1].value
        nodes = frozenset(
            (n.find("ref").children[1].value, n.find("pin").children[1].value)
            for n in net.find_all("node")
        )
        if nodes:
            nets[name] = nodes
    return nets


def _ee_partition(netmap: dict) -> set[frozenset]:
    return set(netmap.values())


def _akcli_partition(sch: model.Schematic) -> set[frozenset]:
    """akcli nets as a membership partition, eeschema-comparable.

    Power symbols (``#``-prefixed refs) are dropped — eeschema excludes them
    from netlist export — and nets left empty by that drop vanish with them.
    """
    out: set[frozenset] = set()
    for net in sch.nets:
        members = frozenset(m for m in net.members if not m[0].startswith("#"))
        if members:
            out.add(members)
    return out


def _akcli_named(sch: model.Schematic) -> dict[str, frozenset]:
    return {
        n.name: frozenset(m for m in n.members if not m[0].startswith("#"))
        for n in sch.nets if n.is_named
    }


# --------------------------------------------------------------------------- #
# shared fixture library: XF (one asymmetric pin), RR (2-pin R), power:+3V3
# --------------------------------------------------------------------------- #
_LIB = """\
(kicad_symbol_lib (version 20231120) (generator akcli_test)
  (symbol "XF" (pin_numbers (hide yes)) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "X" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "XF" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "XF_1_1"
      (pin passive line (at 2.54 5.08 270) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))))
  (symbol "RR" (pin_numbers (hide yes)) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "RR" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "RR_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27)))))))
  (symbol "+3V3" (power) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "+3V3" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "+3V3_1_1"
      (pin power_in line (at 0 0 90) (length 0)
        (name "+3V3" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))))
)
"""


def _oplist(*ops):
    return {"protocol_version": 1, "target_format": "kicad", "ops": list(ops)}


def _seed(d: Path, name: str) -> Path:
    p = d / name
    p.write_text(
        f'(kicad_sch (version 20231120) (generator "akcli") '
        f'(uuid "{_uuid.uuid4()}") (paper "A4"))\n'
    )
    return p


def _draw(d: Path, name: str, *ops) -> Path:
    lib = d / "parity.kicad_sym"
    if not lib.exists():
        lib.write_text(_LIB)
    tgt = _seed(d, name)
    results = kw.apply(_oplist(*ops), str(tgt), apply=True, sources=[str(lib)])
    bad = [r for r in results if r.status != "ok"]
    assert not bad, f"writer refused fixture ops: {bad[0].error_code}: {bad[0].message}"
    return tgt


# --------------------------------------------------------------------------- #
# (2) transform truth table — kicad-cli-established, restated as data so the
# 12-combo lock runs even where KiCad is not installed.
# Library pin local (+100, +200) mil (+Y up); values are world offsets from the
# instance origin in the +Y-down frame.
# --------------------------------------------------------------------------- #
_XF_PIN_UP = (100, 200)
TRUTH = {
    (0, "none"): (100, -200), (0, "x"): (100, 200), (0, "y"): (-100, -200),
    (90, "none"): (-200, -100), (90, "x"): (-200, 100), (90, "y"): (200, -100),
    (180, "none"): (-100, 200), (180, "x"): (-100, -200), (180, "y"): (100, 200),
    (270, "none"): (200, 100), (270, "x"): (200, -100), (270, "y"): (-200, 100),
}
_COMBOS = sorted(TRUTH)


@pytest.mark.parametrize("rot,mirror", _COMBOS)
def test_transform_point_matches_eeschema_truth(rot, mirror):
    flipped = (_XF_PIN_UP[0], -_XF_PIN_UP[1])   # +Y up -> +Y down
    assert geometry.transform_point(flipped, rot, mirror) == TRUTH[(rot, mirror)]


@pytest.mark.parametrize("rot,mirror", _COMBOS)
def test_reader_pin_world_matches_eeschema_truth(rot, mirror):
    ex, ey = TRUTH[(rot, mirror)]
    wx, wy = kreader._pin_world(_XF_PIN_UP[0], _XF_PIN_UP[1], 5000, 4000, rot, mirror)
    assert (wx, wy) == (5000 + ex, 4000 + ey)


@pytest.mark.parametrize("rot,mirror", _COMBOS)
def test_writer_pin_world_matches_eeschema_truth(rot, mirror):
    sym = model.SymbolDef(
        name="XF", lib_id="XF",
        pins=[model.Pin(number="1", name=None, x_mil=100, y_mil=200)],
    )
    inst = model.Component(
        designator="X1", library_ref="XF", x_mil=5000, y_mil=4000,
        rotation=rot, mirror=mirror,
    )
    ex, ey = TRUTH[(rot, mirror)]
    assert geometry.pin_world(sym, inst, sym.pins[0]) == (
        units.mil_to_nm(5000 + ex), units.mil_to_nm(4000 + ey))


# --------------------------------------------------------------------------- #
# (a) pin world-coord matrix, arbitrated by a real eeschema netlist
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def transform_parity(tmp_path_factory):
    """Writer-authored 12-combo sheet + its eeschema netlist."""
    d = tmp_path_factory.mktemp("parity_xform")
    ops = []
    for i, (rot, mirror) in enumerate(_COMBOS):
        cx, cy = 2000 + (i % 4) * 1500, 2000 + (i // 4) * 2000
        ex, ey = TRUTH[(rot, mirror)]
        op = {"op": "place_component", "lib_id": "XF", "designator": f"X{i}",
              "x_mil": cx, "y_mil": cy, "rotation": rot}
        if mirror != "none":
            op["mirror"] = mirror
        ops.append(op)
        # anchor RR whose pin 1 sits 300 mil below the transformed XF pin
        ops.append({"op": "place_component", "lib_id": "RR",
                    "designator": f"A{i}",
                    "x_mil": cx + ex, "y_mil": cy + ey + 450})
        ops.append({"op": "add_wire", "vertices": [f"X{i}.1", f"A{i}.1"]})
    tgt = _draw(d, "xform.kicad_sch", *ops)
    return _export_nets(tgt), kreader.read_sch(tgt)


@needs_kicad
@pytest.mark.parametrize("rot,mirror", _COMBOS)
def test_pin_matrix_combo_connects_in_eeschema(transform_parity, rot, mirror):
    """eeschema must see the wire the writer drew to X{i}.1 land ON the pin."""
    ee, _sch = transform_parity
    i = _COMBOS.index((rot, mirror))
    expected = frozenset({(f"X{i}", "1"), (f"A{i}", "1")})
    assert expected in _ee_partition(ee), (
        f"rot={rot} mirror={mirror}: eeschema did not connect the pin "
        f"(transform diverges from eeschema)")


@needs_kicad
def test_pin_matrix_partition_parity(transform_parity):
    ee, sch = transform_parity
    assert _akcli_partition(sch) == _ee_partition(ee)


# --------------------------------------------------------------------------- #
# (b) label scope matrix
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def scope_parity(tmp_path_factory):
    d = tmp_path_factory.mktemp("parity_scope")

    def stub(i, ref):
        """R at column i with a wire stub up from pin 1."""
        x = 1000 * i
        return (
            {"op": "place_component", "lib_id": "RR", "designator": ref,
             "x_mil": x, "y_mil": 2000},
            {"op": "add_wire", "vertices": [f"{ref}.1", [x, 1700]]},
        )

    ops = [
        # SCOPE1: DISCONNECTED local vs global, same sheet, same name
        *stub(1, "R1"),
        {"op": "add_net_label", "name": "SCOPE1", "at": [1000, 1700],
         "scope": "local", "orientation": 90},
        *stub(2, "R2"),
        {"op": "add_net_label", "name": "SCOPE1", "at": [2000, 1700],
         "scope": "global", "orientation": 90},
        # +3V3: DISCONNECTED local label vs power port, same sheet
        *stub(3, "R3"),
        {"op": "add_net_label", "name": "+3V3", "at": [3000, 1700],
         "scope": "local", "orientation": 90},
        {"op": "place_component", "lib_id": "RR", "designator": "R4",
         "x_mil": 4000, "y_mil": 2000},
        {"op": "place_power_port", "lib_id": "+3V3", "net_name": "+3V3",
         "at": "R4.1"},
        # SCOPE2: two DISCONNECTED locals, same sheet, same name
        *stub(5, "R5"),
        {"op": "add_net_label", "name": "SCOPE2", "at": [5000, 1700],
         "scope": "local", "orientation": 90},
        *stub(6, "R6"),
        {"op": "add_net_label", "name": "SCOPE2", "at": [6000, 1700],
         "scope": "local", "orientation": 90},
        # SCOPE3: two DISCONNECTED globals
        *stub(7, "R7"),
        {"op": "add_net_label", "name": "SCOPE3", "at": [7000, 1700],
         "scope": "global", "orientation": 90},
        *stub(8, "R8"),
        {"op": "add_net_label", "name": "SCOPE3", "at": [8000, 1700],
         "scope": "global", "orientation": 90},
        # SCOPE4: CONNECTED local (mid-span!) + global on one wire
        {"op": "place_component", "lib_id": "RR", "designator": "R9",
         "x_mil": 9000, "y_mil": 2000},
        {"op": "add_wire", "vertices": ["R9.1", [9000, 1600]]},
        {"op": "add_net_label", "name": "SCOPE4", "at": [9000, 1700],
         "scope": "local", "orientation": 90},
        {"op": "add_net_label", "name": "SCOPE4", "at": [9000, 1600],
         "scope": "global", "orientation": 90},
    ]
    tgt = _draw(d, "scope.kicad_sch", *ops)
    return _export_nets(tgt), kreader.read_sch(tgt)


@needs_kicad
def test_scope_partition_parity(scope_parity):
    ee, sch = scope_parity
    assert _akcli_partition(sch) == _ee_partition(ee)


@needs_kicad
def test_scope_local_joins_same_sheet_global_even_disconnected(scope_parity):
    """Rule 5b's ground truth: eeschema merges, so must akcli."""
    ee, sch = scope_parity
    assert ee["SCOPE1"] == {("R1", "1"), ("R2", "1")}
    assert "/SCOPE1" not in ee            # the local name was absorbed
    assert _akcli_named(sch)["SCOPE1"] == ee["SCOPE1"]


@needs_kicad
def test_scope_local_joins_same_sheet_power_net_even_disconnected(scope_parity):
    ee, sch = scope_parity
    assert ee["+3V3"] == {("R3", "1"), ("R4", "1")}
    assert "/+3V3" not in ee
    assert _akcli_named(sch)["+3V3"] == ee["+3V3"]


@needs_kicad
def test_scope_two_locals_merge_and_stay_sheet_local(scope_parity):
    """Same-sheet same-name locals are ONE net — and it is a LOCAL net:
    eeschema names it '/SCOPE2'; normalization must never collapse that
    onto a global 'SCOPE2'."""
    ee, sch = scope_parity
    assert ee["/SCOPE2"] == {("R5", "1"), ("R6", "1")}
    assert "SCOPE2" not in ee
    assert _akcli_named(sch)["SCOPE2"] == ee["/SCOPE2"]


@needs_kicad
def test_scope_two_globals_merge(scope_parity):
    ee, sch = scope_parity
    assert ee["SCOPE3"] == {("R7", "1"), ("R8", "1")}
    assert _akcli_named(sch)["SCOPE3"] == ee["SCOPE3"]


@needs_kicad
def test_scope_connected_local_plus_global_takes_global_name(scope_parity):
    """Also locks the mid-span label connect: the local label sits on the
    wire's interior, not an endpoint."""
    ee, sch = scope_parity
    assert ee["SCOPE4"] == {("R9", "1")}
    assert "/SCOPE4" not in ee
    assert _akcli_named(sch)["SCOPE4"] == ee["SCOPE4"]


def test_local_label_does_not_merge_across_sheets():
    """Cross-sheet half of the 5b verdict (kicad-cli-verified on a hierarchy:
    a child's local '+3V3' netlists as '/child/+3V3', separate from the root
    power net). Locked at the netbuild layer — the writer cannot author
    hierarchical sheets."""
    prims = model.NetPrimitives(
        wires=[model.WireSeg(a=(1000, 1000), b=(2000, 1000), sheet="/child")],
        pins=[
            model.PinHandle(ref=("R21", "1"), at=(1000, 1000), sheet="/child"),
            model.PinHandle(ref=("R11", "1"), at=(1000, 1000), sheet=""),
        ],
        labels=[
            model.NetLabel(at=(1500, 1000), text="+3V3", scope="local",
                           sheet="/child"),
            model.NetLabel(at=(1000, 1000), text="+3V3", scope="power",
                           sheet=""),
        ],
    )
    from altium_kicad_cli.netbuild import build_nets
    nets = build_nets(prims, t_midspan_connects=False)
    assert sorted(sorted(n.members) for n in nets) == [
        [("R11", "1")], [("R21", "1")]]


# --------------------------------------------------------------------------- #
# (c) junction / X-cross / T patterns
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def junction_parity(tmp_path_factory):
    d = tmp_path_factory.mktemp("parity_junction")

    def r_at(ref, x, y, rot=0):
        op = {"op": "place_component", "lib_id": "RR", "designator": ref,
              "x_mil": x, "y_mil": y}
        if rot:
            op["rotation"] = rot
        return op

    # The two upper resistors are flipped 180 so their pin-2 body ends point
    # AWAY from the vertical wire; a pin-2 tip on the wire's span would earn
    # an auto-junction (eeschema-faithful) and legitimately join the net.
    ops = [
        # X-cross WITH an explicit junction: all four pins one net
        r_at("R1", 1000, 2150), r_at("R2", 3000, 2150),
        r_at("R3", 2000, 850, rot=180), r_at("R4", 2000, 3150),
        {"op": "add_wire", "vertices": ["R1.1", "R2.1"]},   # y = 2000
        {"op": "add_wire", "vertices": ["R3.1", "R4.1"]},   # x = 2000
        {"op": "add_junction", "at": [2000, 2000]},
        # X-cross WITHOUT a junction: two independent nets
        r_at("R5", 5000, 2150), r_at("R6", 7000, 2150),
        r_at("R7", 6000, 850, rot=180), r_at("R8", 6000, 3150),
        {"op": "add_wire", "vertices": ["R5.1", "R6.1"]},
        {"op": "add_wire", "vertices": ["R7.1", "R8.1"]},
        # T: arm wire ends on the trunk's mid-span; auto_junctions inserts the
        # dot the way eeschema's editor would -> all three pins one net
        r_at("R9", 1000, 6150), r_at("R10", 3000, 6150),
        r_at("R11", 2000, 7150),
        {"op": "add_wire", "vertices": ["R9.1", "R10.1"]},  # y = 6000
        {"op": "add_wire", "vertices": [[2000, 6000], "R11.1"]},
        # no_connect on a pin: stays a single-pin net on both sides
        r_at("R12", 5000, 6150),
        {"op": "add_no_connect", "pin": "R12.1"},
    ]
    tgt = _draw(d, "junction.kicad_sch", *ops)
    return _export_nets(tgt), kreader.read_sch(tgt)


@needs_kicad
def test_junction_partition_parity(junction_parity):
    ee, sch = junction_parity
    assert _akcli_partition(sch) == _ee_partition(ee)


@needs_kicad
def test_x_cross_with_junction_merges(junction_parity):
    ee, _ = junction_parity
    assert frozenset({("R1", "1"), ("R2", "1"), ("R3", "1"), ("R4", "1")}) \
        in _ee_partition(ee)


@needs_kicad
def test_x_cross_without_junction_does_not_merge(junction_parity):
    ee, _ = junction_parity
    part = _ee_partition(ee)
    assert frozenset({("R5", "1"), ("R6", "1")}) in part
    assert frozenset({("R7", "1"), ("R8", "1")}) in part


@needs_kicad
def test_t_with_auto_junction_merges(junction_parity):
    ee, _ = junction_parity
    assert frozenset({("R9", "1"), ("R10", "1"), ("R11", "1")}) \
        in _ee_partition(ee)


@needs_kicad
def test_no_connect_pin_stays_single(junction_parity):
    ee, sch = junction_parity
    assert frozenset({("R12", "1")}) in _ee_partition(ee)
    # and the reader carried the no-connect point for ERC suppression
    nc = {(round(x), round(y)) for x, y in sch.no_erc_points}
    assert (5000, 6000) in nc


# --------------------------------------------------------------------------- #
# (c') junction-less T / pin-mid-span — the shapes the writer refuses to
# author (auto_junctions would insert the dot), hand-rolled textually.
# --------------------------------------------------------------------------- #
_ROOT_UUID = "ab000000-0000-4000-8000-000000000000"


def _mm(mil: float) -> str:
    s = f"{mil * 0.0254:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _raw_sch(body: list[str]) -> str:
    lib = "\n".join(
        "\t" + line for line in _LIB.splitlines()[1:-1]
    )
    return (
        f'(kicad_sch\n\t(version 20231120)\n\t(generator "eeschema")\n'
        f'\t(uuid "{_ROOT_UUID}")\n\t(paper "A4")\n'
        f"\t(lib_symbols\n{lib})\n" + "\n".join(body) + "\n)\n"
    )


def _raw_symbol(n: int, ref: str, x: float, y: float) -> str:
    return f"""\
\t(symbol (lib_id "RR") (at {_mm(x)} {_mm(y)} 0) (unit 1)
\t\t(exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
\t\t(uuid "ab000000-0000-4000-8000-{n:012d}")
\t\t(property "Reference" "{ref}" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "RR" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t(pin "1" (uuid "ab000000-0000-4000-8000-{n + 1:012d}"))
\t\t(pin "2" (uuid "ab000000-0000-4000-8000-{n + 2:012d}"))
\t\t(instances (project "parity"
\t\t\t(path "/{_ROOT_UUID}" (reference "{ref}") (unit 1)))))"""


def _raw_wire(n: int, a, b) -> str:
    return (f'\t(wire (pts (xy {_mm(a[0])} {_mm(a[1])}) '
            f'(xy {_mm(b[0])} {_mm(b[1])}))\n'
            f'\t\t(stroke (width 0) (type default))\n'
            f'\t\t(uuid "ab000000-0000-4000-8000-{n:012d}"))')


@pytest.fixture(scope="module")
def bare_t_parity(tmp_path_factory):
    d = tmp_path_factory.mktemp("parity_bare_t")
    body = [
        # T WITHOUT a junction node: trunk R1.1--R2.1, arm ends mid-span
        _raw_symbol(100, "R1", 1000, 1150),   # pin1 (1000, 1000)
        _raw_symbol(200, "R2", 3000, 1150),   # pin1 (3000, 1000)
        _raw_symbol(300, "R3", 2000, 2150),   # pin1 (2000, 2000)
        _raw_wire(400, (1000, 1000), (3000, 1000)),
        _raw_wire(500, (2000, 1000), (2000, 2000)),
        # pin tip on a wire's INTERIOR, no junction: R5.1 stays floating
        _raw_symbol(600, "R4", 5000, 1150),   # pin1 (5000, 1000) = wire end
        _raw_symbol(700, "R5", 6000, 1150),   # pin1 (6000, 1000) = mid-span
        _raw_wire(800, (5000, 1000), (7000, 1000)),
    ]
    p = d / "bare_t.kicad_sch"
    p.write_text(_raw_sch(body))
    return _export_nets(p), kreader.read_sch(p)


@needs_kicad
def test_bare_t_partition_parity(bare_t_parity):
    ee, sch = bare_t_parity
    assert _akcli_partition(sch) == _ee_partition(ee)


@needs_kicad
def test_bare_t_does_not_connect_in_eeschema(bare_t_parity):
    """The verdict that retired unconditional netbuild rule 3 for KiCad."""
    ee, _ = bare_t_parity
    part = _ee_partition(ee)
    assert frozenset({("R1", "1"), ("R2", "1")}) in part
    assert frozenset({("R3", "1")}) in part


@needs_kicad
def test_pin_midspan_without_junction_floats(bare_t_parity):
    ee, _ = bare_t_parity
    part = _ee_partition(ee)
    assert frozenset({("R4", "1")}) in part
    assert frozenset({("R5", "1")}) in part


# --------------------------------------------------------------------------- #
# grid config -> NET_OFF_GRID (metric grids first-class; exact integer nm)
# --------------------------------------------------------------------------- #
def _one_pin_sch(x_mil: float, y_mil: float) -> model.Schematic:
    comp = model.Component(
        designator="U1", library_ref="X", x_mil=x_mil, y_mil=y_mil,
        pins=[model.Pin(number="1", name=None, x_mil=x_mil, y_mil=y_mil)],
    )
    return model.Schematic(
        source_path="<mem>", source_format="kicad",
        components=[comp], nets=[],
    )


def test_grid_config_parses_mil_and_mm(tmp_path):
    p = tmp_path / config_mod.CONFIG_FILENAME
    p.write_text('[project]\ngrid = "1.27mm"\n')
    assert config_mod.load_config(p).grid_nm == 1_270_000
    p.write_text('[project]\ngrid = 25\n')
    assert config_mod.load_config(p).grid_nm == 25 * units.NM_PER_MIL
    p.write_text('[project]\ngrid = "0.5mm"\n')
    assert config_mod.load_config(p).grid_nm == 500_000


def test_grid_config_rejects_junk(tmp_path):
    from altium_kicad_cli.errors import AkcliError
    p = tmp_path / config_mod.CONFIG_FILENAME
    p.write_text('[project]\ngrid = "fifty"\n')
    with pytest.raises(AkcliError):
        config_mod.load_config(p)
    p.write_text('[project]\ngrid = 0\n')
    with pytest.raises(AkcliError):
        config_mod.load_config(p)


def test_metric_grid_pin_off_default_but_on_metric_grid(tmp_path):
    # 1.5 mm = 59.0551... mil: off the 50-mil grid, exactly on a 0.5 mm grid.
    mil = units.nm_to_mil(units.mm_to_nm(1.5))
    sch = _one_pin_sch(mil, mil)
    assert any(f.code == netcheck.NET_OFF_GRID for f in netcheck.run(sch, None))
    p = tmp_path / config_mod.CONFIG_FILENAME
    p.write_text('[project]\ngrid = "0.5mm"\n')
    cfg = config_mod.load_config(p)
    assert not netcheck.run(sch, cfg)


def test_default_grid_pin_on_50mil_grid_is_clean():
    assert not netcheck.run(_one_pin_sch(1050, 2500), None)
