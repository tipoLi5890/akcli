"""``(bus_alias ...)`` handling in the KiCad reader — kicad-cli-arbitrated.

Ground truth (KiCad 10.0.4, ``kicad-cli sch export netlist``; the end-to-end
fixtures that establish it live in ``tests/test_kicad_parity.py`` section (g)):
a ``(bus_alias "NAME" (members ...))`` has **no effect on the exported
netlist**. A bus labeled with the alias name behaves exactly like a bus
carrying a plain (member-less) label — the netlist is byte-identical whether or
not the alias is declared:

* a rip whose wire label is a declared member is netlisted by that label's own
  scope (a local label stays sheet-local — it does NOT become a global member
  the way a literal group bus ``{A B C}`` or a vector bus ``A[0..3]`` would);
* an unlabeled rip floats;
* a rip labeled with a NON-member is netlisted by its own label all the same;
* when the alias NAME is itself a vector (``A[0..3]``), the vector wins and the
  declared members are ignored (the label expands to ``A0..A3``).

The akcli reader reproduces every case for free: it expands vector labels
(:func:`netbuild.expand_bus_vector`) and treats every non-vector bus label —
alias names included — as member-less, and it never reads ``(bus_alias ...)``
into a primitive. These tests lock that behavior against the reader directly so
they run with no KiCad installed; the kicad-cli parity that justifies each
verdict is in ``tests/test_kicad_parity.py``.
"""

from __future__ import annotations

import uuid as _uuid
from pathlib import Path

from akcli.readers import kicad as kreader

_ROOT_UUID = "ba000000-0000-4000-8000-000000000000"
_SHEET_UUID = "ba000000-0000-4000-8000-000000000999"

_LIB_SYMBOLS = (
    '(lib_symbols (symbol "RR" (pin_numbers (hide yes)) (pin_names (offset 0))'
    ' (exclude_from_sim no) (in_bom yes) (on_board yes)'
    ' (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))'
    ' (property "Value" "RR" (at 0 0 0) (effects (font (size 1.27 1.27))))'
    ' (symbol "RR_1_1"'
    ' (pin passive line (at 0 3.81 270) (length 1.27)'
    ' (name "~" (effects (font (size 1.27 1.27))))'
    ' (number "1" (effects (font (size 1.27 1.27)))))'
    ' (pin passive line (at 0 -3.81 90) (length 1.27)'
    ' (name "~" (effects (font (size 1.27 1.27))))'
    ' (number "2" (effects (font (size 1.27 1.27))))))))'
)


def _mm(mil: float) -> str:
    s = f"{mil * 0.0254:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _counter():
    n = [0]

    def nxt() -> str:
        n[0] += 1
        return f"ba000000-0000-4000-8000-{n[0]:012d}"

    return nxt


class _Sheet:
    """A tiny raw ``.kicad_sch`` body builder (buses/labels/entries/rips)."""

    def __init__(self, uuid: str, path: str, nxt) -> None:
        self._u = nxt
        self._uuid = uuid
        self._path = path
        self.body: list[str] = []

    def bus_alias(self, name: str, members: list[str]) -> None:
        mem = " ".join(f'"{m}"' for m in members)
        self.body.append(f'(bus_alias "{name}" (members {mem}))')

    def wire(self, a, b, tag: str = "wire") -> None:
        self.body.append(
            f"({tag} (pts (xy {_mm(a[0])} {_mm(a[1])}) "
            f"(xy {_mm(b[0])} {_mm(b[1])}))"
            f' (stroke (width 0) (type default)) (uuid "{self._u()}"))'
        )

    def label(self, text, x, y, tag: str = "label", extra: str = "") -> None:
        self.body.append(
            f'({tag} "{text}" {extra}(at {_mm(x)} {_mm(y)} 0)'
            f' (effects (font (size 1.27 1.27))) (uuid "{self._u()}"))'
        )

    def entry(self, x, y) -> None:
        self.body.append(
            f"(bus_entry (at {_mm(x)} {_mm(y)}) (size 2.54 2.54)"
            f' (stroke (width 0) (type default)) (uuid "{self._u()}"))'
        )

    def sym(self, ref, x, y) -> None:
        self.body.append(
            f'(symbol (lib_id "RR") (at {_mm(x)} {_mm(y)} 0) (unit 1)'
            f' (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)'
            f' (uuid "{self._u()}")'
            f' (property "Reference" "{ref}" (at 0 0 0)'
            f' (effects (font (size 1.27 1.27))))'
            f' (property "Value" "RR" (at 0 0 0)'
            f' (effects (font (size 1.27 1.27))))'
            f' (pin "1" (uuid "{self._u()}")) (pin "2" (uuid "{self._u()}"))'
            f' (instances (project "ba"'
            f' (path "{self._path}" (reference "{ref}") (unit 1)))))'
        )

    def rip(self, bus_x, y, ref, lbl) -> None:
        """entry at (bus_x, y) -> wire right -> pin 1; optional wire label."""
        fx, fy = bus_x + 100, y + 100
        self.entry(bus_x, y)
        self.wire((fx, fy), (fx + 600, fy))
        if lbl is not None:
            self.label(lbl, fx + 300, fy)
        self.sym(ref, fx + 600, fy + 150)

    def render(self, extra: str = "") -> str:
        return (
            f'(kicad_sch (version 20231120) (generator "eeschema")'
            f' (uuid "{self._uuid}") (paper "A4")\n{_LIB_SYMBOLS}\n'
            + "\n".join(self.body)
            + "\n"
            + extra
            + "\n)\n"
        )


def _partition(sch) -> set[frozenset]:
    out: set[frozenset] = set()
    for net in sch.nets:
        members = frozenset(m for m in net.members if not m[0].startswith("#"))
        if members:
            out.add(members)
    return out


def _named(sch) -> dict[str, frozenset]:
    return {n.name: frozenset(n.members) for n in sch.nets if n.is_named}


# --------------------------------------------------------------------------- #
# single sheet: an alias-labeled bus is member-less (each rip = its own net).
# --------------------------------------------------------------------------- #
def _single_sheet(tmp_path: Path, *, declare_alias: bool) -> Path:
    s = _Sheet(_ROOT_UUID, f"/{_ROOT_UUID}", _counter())
    if declare_alias:
        s.bus_alias("CTRL", ["EN", "RST", "D[0..3]"])
    s.wire((4000, 2000), (4000, 6000), tag="bus")
    s.label("CTRL", 4000, 2200)
    s.rip(4000, 2500, "R1", "EN")   # declared plain member
    s.rip(4000, 3000, "R2", "D2")   # declared vector member
    s.rip(4000, 3500, "R3", None)   # unlabeled
    s.rip(4000, 4000, "R4", "XX")   # non-member
    p = tmp_path / "single.kicad_sch"
    p.write_text(s.render())
    return p


def test_alias_labeled_bus_is_memberless(tmp_path):
    sch = kreader.read_sch(_single_sheet(tmp_path, declare_alias=True))
    part = _partition(sch)
    # every rip is its own single-pin net, member or not, labeled or not.
    assert frozenset({("R1", "1")}) in part
    assert frozenset({("R2", "1")}) in part
    assert frozenset({("R3", "1")}) in part
    assert frozenset({("R4", "1")}) in part
    named = _named(sch)
    assert named.get("EN") == frozenset({("R1", "1")})
    assert named.get("D2") == frozenset({("R2", "1")})


def test_alias_declaration_has_no_netlist_effect(tmp_path):
    # THE parity fact: declaring the alias changes nothing in the netlist.
    with_alias = _partition(kreader.read_sch(_single_sheet(tmp_path, declare_alias=True)))
    d2 = tmp_path / "noalias"
    d2.mkdir()
    without = _partition(kreader.read_sch(_single_sheet(d2, declare_alias=False)))
    assert with_alias == without


# --------------------------------------------------------------------------- #
# cross sheet: alias members do NOT merge (unlike a group/vector bus). A global
# alias label does not globalize its members.
# --------------------------------------------------------------------------- #
def _cross_sheet(tmp_path: Path, alias_name: str, members: list[str],
                 root_rips, child_rips) -> Path:
    nxt = _counter()
    root = _Sheet(_ROOT_UUID, f"/{_ROOT_UUID}", nxt)
    root.bus_alias(alias_name, members)
    root.wire((4000, 2000), (4000, 7000), tag="bus")
    root.label(alias_name, 4000, 2200, tag="global_label", extra="(shape input) ")
    y = 2500
    for ref, lbl in root_rips:
        root.rip(4000, y, ref, lbl)
        y += 600
    sheet_block = (
        f'(sheet (at {_mm(14000)} {_mm(2000)}) (size {_mm(1000)} {_mm(800)})'
        f' (stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0))'
        f' (uuid "{_SHEET_UUID}")'
        f' (property "Sheetname" "child" (at 0 0 0)'
        f' (effects (font (size 1.27 1.27))))'
        f' (property "Sheetfile" "child.kicad_sch" (at 0 0 0)'
        f' (effects (font (size 1.27 1.27))))'
        f' (instances (project "ba"'
        f' (path "/{_ROOT_UUID}" (page "2")))))'
    )
    (tmp_path / "bus_root.kicad_sch").write_text(root.render(extra=sheet_block))

    childpath = f"/{_ROOT_UUID}/{_SHEET_UUID}"
    child = _Sheet(str(_uuid.uuid4()), childpath, nxt)
    child.bus_alias(alias_name, members)
    child.wire((4000, 2000), (4000, 7000), tag="bus")
    child.label(alias_name, 4000, 2200, tag="global_label", extra="(shape input) ")
    y = 2500
    for ref, lbl in child_rips:
        child.rip(4000, y, ref, lbl)
        y += 600
    (tmp_path / "child.kicad_sch").write_text(child.render())
    return tmp_path / "bus_root.kicad_sch"


def test_alias_members_stay_sheet_local(tmp_path):
    tgt = _cross_sheet(
        tmp_path, "CTRL", ["EN", "RST", "D[0..3]"],
        [("R1", "EN"), ("R3", "D2")],
        [("R2", "EN"), ("R4", "D2")],
    )
    part = _partition(kreader.read_sch(tgt))
    # A group/vector bus would merge EN and D2 across sheets; the alias does not.
    assert frozenset({("R1", "1")}) in part
    assert frozenset({("R2", "1")}) in part
    assert frozenset({("R3", "1")}) in part
    assert frozenset({("R4", "1")}) in part
    assert frozenset({("R1", "1"), ("R2", "1")}) not in part


def test_alias_name_that_is_a_vector_expands_as_a_vector(tmp_path):
    # Collision: alias NAME "A[0..3]" also parses as a vector. Vector wins —
    # A2 (a vector member) merges across sheets; X (a declared alias member)
    # does not, because the alias is ignored.
    tgt = _cross_sheet(
        tmp_path, "A[0..3]", ["X", "Y"],
        [("R1", "A2"), ("R3", "X")],
        [("R2", "A2"), ("R4", "X")],
    )
    part = _partition(kreader.read_sch(tgt))
    assert frozenset({("R1", "1"), ("R2", "1")}) in part   # vector member merges
    assert frozenset({("R3", "1")}) in part                # alias member local
    assert frozenset({("R4", "1")}) in part


# --------------------------------------------------------------------------- #
# an unused / standalone alias is a harmless no-op that reads cleanly.
# --------------------------------------------------------------------------- #
def test_unused_alias_is_a_harmless_no_op(tmp_path):
    s = _Sheet(_ROOT_UUID, f"/{_ROOT_UUID}", _counter())
    s.bus_alias("UNUSED", ["A", "B", "C[0..1]"])
    # a lone resistor on a plain wire; the alias names no bus here.
    s.wire((1000, 1000), (1600, 1000))
    s.label("SIG", 1000, 1000)
    s.sym("R1", 1600, 1150)
    p = tmp_path / "unused.kicad_sch"
    p.write_text(s.render())
    sch = kreader.read_sch(p)
    # reads without error, no primitive leaks from the alias declaration.
    assert not sch.warnings
    assert frozenset({("R1", "1")}) in _partition(sch)
    # the alias member names never surface as nets.
    assert "A" not in _named(sch)
    assert "C0" not in _named(sch)


def test_alias_with_only_vector_members_parses(tmp_path):
    # a bus_alias whose members are all vectors must not trip the reader.
    s = _Sheet(_ROOT_UUID, f"/{_ROOT_UUID}", _counter())
    s.bus_alias("WIDE", ["D[0..7]", "A[0..3]"])
    s.wire((1000, 1000), (1600, 1000))
    s.sym("R1", 1600, 1150)
    p = tmp_path / "wide.kicad_sch"
    p.write_text(s.render())
    sch = kreader.read_sch(p)
    assert not sch.warnings
