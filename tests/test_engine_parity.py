"""Two connectivity engines, one set of hard rules — pin their agreement.

akcli authors the "are these two things electrically joined?" question twice:

* :mod:`akcli.netbuild` (via :func:`akcli.readers.kicad.read_sch`) builds the
  **netlist** from primitives on **quantized-mil** integer coordinates;
* :mod:`akcli.writers.connectivity` runs the **write gate** (``verify``) on
  **integer-nanometre** coordinates.

The unit systems differ, the rules were written independently, and nothing in
the code forces the two to agree — so a fix on one side can silently drift from
the other ("the netlist says connected, the gate says dangling", or worse the
reverse). This module is the seam contract: the shared hard rules are asserted
on both engines at once, over the committed corpus and over minimal synthetic
documents authored right here (so the geometry under test is exact, not
incidental to a fixture board).

Rules pinned:

1. **Corpus boards agree.** Every ``tests/fixtures/corpus/*.kicad_sch`` is
   ``verify``-clean AND builds a netlist without anomalies (no crash, no
   member-less/duplicate membership), and where a committed
   ``tests/golden/nets-<board>.json`` snapshot exists the member sets match it.
2. **T-junction (endpoint on a mid-span, junction present) = connected** in
   both: netbuild merges the three pins into one net; ``verify`` reports no
   ``DANGLING_ENDPOINT``.
3. **Near miss (endpoint 1 mil off the mid-span, no junction) = not connected**
   in both: netbuild keeps the nets apart AND ``verify`` flags the free end.
   Case 3 is the important half — the failure mode is one engine forgiving a
   near-coincidence the other does not.
4. **Bare T (exactly on the mid-span, no junction) = not connected** in both:
   the eeschema rule. ``verify`` used to accept it, which made
   ``check --integrity`` and ``nets`` give opposite answers on hand-drawn or
   imported boards (the ``draw`` path hid it — ``auto_junctions`` runs first).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli.readers import kicad, sexpr
from akcli.writers import connectivity

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "corpus"
GOLDEN = ROOT / "tests" / "golden"

CORPUS_BOARDS = sorted(CORPUS.glob("*.kicad_sch"))

# --------------------------------------------------------------------------- #
# synthetic document builder (exact geometry, minimal document)
# --------------------------------------------------------------------------- #
_ROOT_UUID = "8a000000-0000-4000-8000-000000000000"

# A 2-pin resistor: pin "1" connects at local (0, +3.81) -> world y - 3.81,
# pin "2" at local (0, -3.81) -> world y + 3.81 (KiCad's y is flipped).
_LIB_SYMBOLS = (
    '\t(lib_symbols\n'
    '\t\t(symbol "Device:R"\n'
    '\t\t\t(pin_numbers (hide yes))\n'
    '\t\t\t(pin_names (offset 0))\n'
    '\t\t\t(exclude_from_sim no)\n'
    '\t\t\t(in_bom yes)\n'
    '\t\t\t(on_board yes)\n'
    '\t\t\t(property "Reference" "R"\n'
    '\t\t\t\t(at 2.032 0 90)\n'
    '\t\t\t\t(effects (font (size 1.27 1.27))))\n'
    '\t\t\t(property "Value" "R"\n'
    '\t\t\t\t(at 0 0 90)\n'
    '\t\t\t\t(effects (font (size 1.27 1.27))))\n'
    '\t\t\t(symbol "R_1_1"\n'
    '\t\t\t\t(pin passive line\n'
    '\t\t\t\t\t(at 0 3.81 270)\n'
    '\t\t\t\t\t(length 1.27)\n'
    '\t\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))\n'
    '\t\t\t\t\t(number "1" (effects (font (size 1.27 1.27)))))\n'
    '\t\t\t\t(pin passive line\n'
    '\t\t\t\t\t(at 0 -3.81 90)\n'
    '\t\t\t\t\t(length 1.27)\n'
    '\t\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))\n'
    '\t\t\t\t\t(number "2" (effects (font (size 1.27 1.27))))))))\n'
)


def _symbol(ref: str, x: float, y: float, n: int) -> str:
    return (
        '\t(symbol\n'
        '\t\t(lib_id "Device:R")\n'
        f'\t\t(at {x} {y} 0)\n'
        '\t\t(unit 1)\n'
        f'\t\t(uuid "8a000000-0000-4000-8000-0000000001{n:02d}")\n'
        f'\t\t(property "Reference" "{ref}"\n'
        f'\t\t\t(at {x} {y} 0)\n'
        '\t\t\t(effects (font (size 1.27 1.27))))\n'
        '\t\t(property "Value" "10k"\n'
        f'\t\t\t(at {x} {y} 0)\n'
        '\t\t\t(effects (font (size 1.27 1.27))))\n'
        '\t\t(instances\n'
        '\t\t\t(project ""\n'
        f'\t\t\t\t(path "/{_ROOT_UUID}"\n'
        f'\t\t\t\t\t(reference "{ref}") (unit 1)))))\n'
    )


def _wire(x1: float, y1: float, x2: float, y2: float, n: int) -> str:
    return (f'\t(wire (pts (xy {x1} {y1}) (xy {x2} {y2}))\n'
            '\t\t(stroke (width 0) (type default))\n'
            f'\t\t(uuid "8a000000-0000-4000-8000-0000000002{n:02d}"))\n')


def _junction(x: float, y: float, n: int) -> str:
    return (f'\t(junction (at {x} {y}) (diameter 0) (color 0 0 0 0)\n'
            f'\t\t(uuid "8a000000-0000-4000-8000-0000000003{n:02d}"))\n')


# Geometry of both synthetic cases:
#
#   R1 pin2 (50.8, 54.61) ---- wire A ---- (76.2, 54.61) R2 pin2
#                          T at (63.5, 54.61)
#                                |  wire B
#                          (63.5, 62.23) R3 pin1
#
# The T case puts wire B's end exactly on wire A's mid-span with a junction;
# the near-miss case offsets it by one mil (0.0254 mm) and drops the junction.
_MID_X = 63.5
_SPAN_Y = 54.61
_ONE_MIL_MM = 0.0254


def _sch(stem_y: float, *, with_junction: bool) -> str:
    parts = [
        '(kicad_sch (version 20231120) (generator "akcli")\n',
        f'\t(uuid "{_ROOT_UUID}")\n',
        _LIB_SYMBOLS,
        _symbol("R1", 50.8, 50.8, 1),
        _symbol("R2", 76.2, 50.8, 2),
        _symbol("R3", _MID_X, 66.04, 3),
        _wire(50.8, _SPAN_Y, 76.2, _SPAN_Y, 1),      # wire A (the spanned wire)
        _wire(_MID_X, stem_y, _MID_X, 62.23, 2),     # wire B (the stem)
    ]
    if with_junction:
        parts.append(_junction(_MID_X, _SPAN_Y, 1))
    parts.append(')\n')
    return "".join(parts)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def _verify_codes(text: str) -> list[str]:
    return [f.code for f in connectivity.verify(sexpr.parse(text))]


def _member_sets(path: Path) -> set[frozenset]:
    return {frozenset(net.members) for net in kicad.read_sch(str(path)).nets}


def _net_of(path: Path, member: tuple[str, str]) -> frozenset:
    hits = [s for s in _member_sets(path) if member in s]
    assert len(hits) == 1, f"{member} appears in {len(hits)} nets, expected 1"
    return hits[0]


# --------------------------------------------------------------------------- #
# (a) corpus: both engines clean, netbuild matches its committed snapshot
# --------------------------------------------------------------------------- #
def test_corpus_is_not_empty():
    assert CORPUS_BOARDS, "corpus fixtures vanished — parity has nothing to pin"


@pytest.mark.parametrize("board", CORPUS_BOARDS, ids=lambda p: p.stem)
def test_corpus_write_gate_is_clean(board: Path):
    """The nm engine finds nothing on any committed board."""
    findings = _verify_codes(board.read_text(encoding="utf-8"))
    assert findings == [], f"{board.name}: {findings}"


@pytest.mark.parametrize("board", CORPUS_BOARDS, ids=lambda p: p.stem)
def test_corpus_netlist_has_no_anomalies(board: Path):
    """The mil engine builds a sane netlist on the same boards.

    "Sane" = it does not raise, it yields nets, every net has at least one
    member, and no pin is claimed by two nets (a pin in two nets means the two
    engines cannot possibly agree on what is joined).
    """
    nets = kicad.read_sch(str(board)).nets
    assert nets, f"{board.name}: netbuild produced no nets"
    seen: dict[tuple[str, str], int] = {}
    for i, net in enumerate(nets):
        assert net.members, f"{board.name}: net {i} has no members"
        for m in net.members:
            assert m not in seen, (
                f"{board.name}: pin {m} is in nets {seen[m]} and {i}")
            seen[m] = i
    # verify-clean must not coexist with a multi-member net that netbuild only
    # holds together through a rule the gate would call dangling: the gate is
    # asserted clean in the sibling test, so any board reaching here has both
    # engines agreeing that every wire end terminates on something.
    assert _verify_codes(board.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("board", CORPUS_BOARDS, ids=lambda p: p.stem)
def test_corpus_netlist_matches_golden_snapshot(board: Path):
    """Where a nets golden exists, netbuild's member sets still match it."""
    snap = GOLDEN / f"nets-{board.stem}.json"
    if not snap.is_file():
        pytest.skip(f"no committed snapshot {snap.name}")
    doc = json.loads(snap.read_text(encoding="utf-8"))
    expected = {frozenset(tuple(m.split(".", 1)) for m in n["members"])
                for n in doc["nets"]}
    got = _member_sets(board)
    # the golden carries the nets the `nets` command reports; every one of them
    # must still be exactly the membership netbuild derives today.
    assert expected <= got, (
        f"{board.name}: golden nets not reproduced; missing="
        f"{sorted(sorted(s) for s in expected - got)}")


# --------------------------------------------------------------------------- #
# (b) T-junction: connected in BOTH engines
# --------------------------------------------------------------------------- #
def test_t_junction_is_connected_in_both_engines(tmp_path: Path):
    text = _sch(_SPAN_Y, with_junction=True)
    path = _write(tmp_path, "t_junction.kicad_sch", text)

    # nm engine: nothing dangles (and nothing else is wrong either)
    assert _verify_codes(text) == []

    # mil engine: the stem's pin joins the spanned wire's two pins
    assert _net_of(path, ("R3", "1")) == frozenset(
        {("R1", "2"), ("R2", "2"), ("R3", "1")})


# --------------------------------------------------------------------------- #
# (c) near miss: NOT connected in BOTH engines
# --------------------------------------------------------------------------- #
def test_near_miss_is_disconnected_in_both_engines(tmp_path: Path):
    text = _sch(_SPAN_Y + _ONE_MIL_MM, with_junction=False)
    path = _write(tmp_path, "near_miss.kicad_sch", text)

    # nm engine: the stem's free end is flagged
    findings = connectivity.verify(sexpr.parse(text))
    dangling = [f for f in findings
                if f.code == connectivity.DANGLING_ENDPOINT]
    assert len(dangling) == 1, [f.code for f in findings]
    assert dangling[0].refs == [f"({_MID_X} {_SPAN_Y + _ONE_MIL_MM})"]

    # mil engine: no merge — the stem pin is alone, the span keeps its two pins
    assert _net_of(path, ("R3", "1")) == frozenset({("R3", "1")})
    assert _net_of(path, ("R1", "2")) == frozenset({("R1", "2"), ("R2", "2")})


# --------------------------------------------------------------------------- #
# (d) on-grid T with NO junction: NOT connected in BOTH engines
# --------------------------------------------------------------------------- #
def test_bare_t_junction_is_disconnected_in_both_engines(tmp_path: Path):
    """Exactly coincident, no junction — eeschema keeps the nets apart.

    The third case is the one the two engines used to disagree on: netbuild
    runs the eeschema dialect (``t_midspan_connects=False``) while ``verify``
    accepted a mid-span touch as attached, so ``check --integrity`` called a
    hand-drawn board clean while ``akcli nets`` reported two nets. (Files akcli
    itself draws never reach here: ``auto_junctions`` inserts the junction
    first — case (b) above.)
    """
    text = _sch(_SPAN_Y, with_junction=False)
    path = _write(tmp_path, "bare_t.kicad_sch", text)

    # nm engine: the stem's end is on the span, but nothing joins them
    dangling = [f for f in connectivity.verify(sexpr.parse(text))
                if f.code == connectivity.DANGLING_ENDPOINT]
    assert len(dangling) == 1, [f.code for f in dangling]
    assert dangling[0].refs == [f"({_MID_X} {_SPAN_Y})"]

    # mil engine: no merge either
    assert _net_of(path, ("R3", "1")) == frozenset({("R3", "1")})
    assert _net_of(path, ("R1", "2")) == frozenset({("R1", "2"), ("R2", "2")})


def test_one_mil_is_the_only_difference_between_the_two_cases():
    """Guard the fixtures themselves: the cases differ only as documented.

    If a future edit changes the T fixture's geometry without changing the
    near-miss fixture the same way, the parity claim above stops being about a
    1-mil offset — and this assertion is what notices.
    """
    assert _sch(_SPAN_Y, with_junction=True) != _sch(_SPAN_Y + _ONE_MIL_MM,
                                                    with_junction=False)
    # ignoring the junction, the two documents differ in exactly one line: the
    # stem wire's start y, by one mil.
    on_grid = _sch(_SPAN_Y, with_junction=False).splitlines()
    near = _sch(_SPAN_Y + _ONE_MIL_MM, with_junction=False).splitlines()
    assert len(on_grid) == len(near)
    diff = [(a, b) for a, b in zip(on_grid, near) if a != b]
    assert len(diff) == 1, diff
    assert diff[0] == (f'\t(wire (pts (xy {_MID_X} {_SPAN_Y}) '
                       f'(xy {_MID_X} 62.23))',
                       f'\t(wire (pts (xy {_MID_X} {_SPAN_Y + _ONE_MIL_MM}) '
                       f'(xy {_MID_X} 62.23))')
