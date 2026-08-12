"""Ground-truth arbitration for the KiCad netlist exporter (``exporters.py``).

The netlist emitter used to be the only output surface with no ``kicad-cli``
arbitration: its tests asserted shape and determinism, never agreement with
KiCad itself. That is what let the missing UUID identity (``tstamps`` /
``sheetpath``) go unnoticed — KiCad 6+ keys the footprint<->symbol association
by UUID path, so a refdes-only netlist loses every association on a renumber.

Two layers, so the invariant is checked everywhere and *pinned* by real KiCad:

* :func:`test_netlist_matches_committed_kicad_fixture` runs **everywhere**
  against ``tests/fixtures/kicad/netlist_power_entry.kicad10.net`` — a real
  ``kicad-cli 10.0.4`` export committed as ground truth.
* the ``kicad-cli`` tests re-derive that ground truth live (CI KiCad job / any
  dev box with KiCad installed) so the committed fixture cannot silently rot,
  and widen the comparison to every corpus board.

Comparison is **semantic**, never textual: KiCad's own layout, ``(date)``,
``(tool)`` and sheet-name localization are out of scope. What must agree:

1. the set of ``(comp (ref ...))`` designators,
2. the net partition as sets of ``(ref, pin)`` members,
3. the full UUID tstamp path per ref (``sheetpath`` tstamps + comp tstamps),
4. the net NAME per net — pcbnew keys board nets by name, so a netlist that
   disagrees with eeschema renames the whole board on the next update. eeschema
   qualifies a sheet-scoped name with its sheet path (``/GND``, ``/Amp/SIG``)
   and writes a global label / power port bare (``VOUT``, ``GND``); getting that
   wrong is invisible to a partition-only comparison, which is exactly how akcli
   shipped bare ``GND`` against KiCad's ``/GND``.

Two documented normalizations:

* ``#``-prefixed refs (power ports, ``PWR_FLAG``, every virtual symbol) are
  dropped from BOTH sides. KiCad omits them from the netlist entirely —
  components *and* nodes — while akcli's model keeps them, because its net
  engine derives net NAMES from those power ports; a net left with no
  non-virtual member (a bare ``PWR_FLAG`` stub) therefore drops out on both
  sides.
* **Auto-named** nets are compared as a *class*, not by text: KiCad invents
  ``Net-(R1-Pad2)`` / ``unconnected-(R2-Pad2)`` from whichever pin its own
  traversal picked as the driver — not derivable from a normalized model — so
  akcli emits its membership-derived ``stable_id`` instead. Both sides must
  agree that the net has no *designed* name; the text is allowed to differ.

Closed gap (0.21): akcli's canonical pick now follows eeschema's driver
priority (GLOBAL > POWER_PIN > LOCAL_LABEL > HIER_LABEL) for KiCad sources;
``board_v8``'s ``MID``/``VOUT`` node — the standing example — now names
``VOUT`` on both sides (see ``tests/fixtures/kicad/README.md``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import pytest

from akcli import exporters
from akcli.drivers import kicad_cli
from akcli.readers import kicad as kreader
from akcli.sexpr import parser as sexpr

CORPUS = Path(__file__).parent / "fixtures" / "corpus"
KICAD_FIX = Path(__file__).parent / "fixtures" / "kicad"
GROUND_TRUTH = KICAD_FIX / "netlist_power_entry.kicad10.net"
GROUND_TRUTH_BOARD = CORPUS / "power_entry.kicad_sch"
# Hierarchical ground truth: two instances of one child sheet, one grandchild,
# and every flavour of name (root/child/grandchild local label, hierarchical
# label with a bare parent pin, global label, power port) — see the fixture
# README. Every net there carries exactly ONE name.
HIER_GROUND_TRUTH = KICAD_FIX / "netlist_hier_root.kicad10.net"
HIER_BOARD = KICAD_FIX / "hier" / "root.kicad_sch"

CORPUS_BOARDS = sorted(CORPUS.glob("*.kicad_sch"))

# KiCad's auto-names for nets with no designed name (driver = a plain pin).
_KICAD_AUTO_NAME = re.compile(r"^(?:Net-\(|unconnected-\()")
# akcli's membership-derived stand-in for the same nets (model.Net.stable_id).
_AKCLI_AUTO_NAME = re.compile(r"^net_[0-9a-f]{12}$")


# --------------------------------------------------------------------------- #
# netlist parsing (both sides go through readers/sexpr.py)
# --------------------------------------------------------------------------- #
def _sub(node: sexpr.SNode, tag: str) -> str | None:
    """Value of the first argument of child ``(tag <value>)``, or ``None``."""
    child = node.find(tag)
    if child is None or len(child) < 2:
        return None
    return child[1].value


def _tstamp_path(comp: sexpr.SNode) -> str:
    """Full UUID path of a ``(comp ...)``: sheet-instance path + symbol uuid.

    KiCad writes the sheet path (``"/"`` at the root, ``"/<uuid>/"`` below) and
    the symbol uuid separately; the concatenation is the path pcbnew stores on
    each footprint. Both sides are normalized to ``/<sheet-uuids...>/<uuid>``.
    """
    sheetpath = comp.find("sheetpath")
    sheet = (_sub(sheetpath, "tstamps") if sheetpath is not None else None) or "/"
    own = _sub(comp, "tstamps") or ""
    return "/" + "/".join(p for p in (sheet + "/" + own).split("/") if p)


def _virtual(ref: str) -> bool:
    """``#``-prefixed refs: power ports / PWR_FLAG / virtual parts (see module docstring)."""
    return ref.startswith("#")


class _Netlist(NamedTuple):
    """The comparable content of a netlist (see the module docstring)."""

    refs: set[str]
    nets: set[frozenset[tuple[str, str]]]
    tstamps: dict[str, str]
    names: dict[frozenset[tuple[str, str]], str]


def _parse_netlist(text: str) -> _Netlist:
    """Refs, net member sets, ref -> tstamp path and net-members -> name."""
    root = sexpr.parse(text)
    comps = root.find("components")
    assert comps is not None, "netlist has no (components) section"
    refs: set[str] = set()
    tstamps: dict[str, str] = {}
    for comp in comps.find_all("comp"):
        ref = _sub(comp, "ref")
        assert ref is not None
        if _virtual(ref):
            continue
        refs.add(ref)
        tstamps[ref] = _tstamp_path(comp)

    nets_node = root.find("nets")
    assert nets_node is not None, "netlist has no (nets) section"
    nets: set[frozenset[tuple[str, str]]] = set()
    names: dict[frozenset[tuple[str, str]], str] = {}
    for net in nets_node.find_all("net"):
        members = frozenset(
            (_sub(node, "ref") or "", _sub(node, "pin") or "")
            for node in net.find_all("node")
            if not _virtual(_sub(node, "ref") or "")
        )
        if members:
            nets.add(members)
            names[members] = _sub(net, "name") or ""
    return _Netlist(refs, nets, tstamps, names)


def _akcli_netlist(board: Path) -> str:
    return exporters.to_kicad_netlist(kreader.read_sch(str(board)))


def _kicad_cli_netlist(board: Path) -> str | None:
    """Real ``kicad-cli sch export netlist`` text for ``board`` (``None`` on failure)."""
    wrapper = kicad_cli.netlist(str(board))
    if not isinstance(wrapper, dict) or wrapper.get("exit_code") != 0:
        return None
    text = wrapper.get("netlist")
    return text if isinstance(text, str) and text.strip() else None


def _assert_semantically_equal(kicad_text: str, board: Path) -> None:
    k = _parse_netlist(kicad_text)
    a = _parse_netlist(_akcli_netlist(board))

    assert a.refs == k.refs, f"component set differs: {a.refs ^ k.refs}"
    assert a.nets == k.nets, (
        "net partition differs\n"
        f"  only akcli: {sorted(sorted(n) for n in a.nets - k.nets)}\n"
        f"  only kicad: {sorted(sorted(n) for n in k.nets - a.nets)}"
    )
    assert a.tstamps == k.tstamps, "UUID tstamp path differs per ref: " + str(
        {r: (a.tstamps.get(r), k.tstamps.get(r)) for r in k.refs
         if a.tstamps.get(r) != k.tstamps.get(r)}
    )
    _assert_net_names_equal(a.names, k.names)


def _assert_net_names_equal(
    akcli: dict[frozenset[tuple[str, str]], str],
    kicad: dict[frozenset[tuple[str, str]], str],
) -> None:
    """Per-net names must match, auto-named nets compared as a class."""
    designed = {
        members: (akcli[members], name)
        for members, name in kicad.items()
        if not _KICAD_AUTO_NAME.match(name)
    }
    mismatched = {
        tuple(sorted(members)): got_want
        for members, got_want in designed.items() if got_want[0] != got_want[1]
    }
    assert not mismatched, (
        "net NAME differs (akcli, kicad) — pcbnew keys board nets by name:\n"
        + "\n".join(f"  {m}: {gw}" for m, gw in sorted(mismatched.items()))
    )
    auto = {
        tuple(sorted(members)): (akcli[members], name)
        for members, name in kicad.items()
        if _KICAD_AUTO_NAME.match(name)
        and not _AKCLI_AUTO_NAME.match(akcli[members])
    }
    assert not auto, (
        "KiCad auto-named these nets; akcli must fall back to a stable_id "
        f"(got): {auto}"
    )


# --------------------------------------------------------------------------- #
# committed ground truth (runs everywhere)
# --------------------------------------------------------------------------- #
def test_netlist_matches_committed_kicad_fixture():
    """akcli's netlist agrees with a committed real ``kicad-cli`` export."""
    _assert_semantically_equal(GROUND_TRUTH.read_text(encoding="utf-8"), GROUND_TRUTH_BOARD)


def test_netlist_matches_committed_hierarchical_fixture():
    """The hierarchical ground truth: sheet-qualified names, two sheet instances."""
    _assert_semantically_equal(
        HIER_GROUND_TRUTH.read_text(encoding="utf-8"), HIER_BOARD
    )


def test_hierarchical_fixture_pins_every_name_flavour():
    """Guard the fixture: it must still cover all four qualification cases."""
    names = set(_parse_netlist(HIER_GROUND_TRUTH.read_text(encoding="utf-8")).names.values())
    assert "/ROOT_LOCAL" in names                 # root-sheet local label
    assert "/Amp/AMP_LOCAL" in names              # child-sheet local label
    assert "/Amp Two/AMP_LOCAL" in names          # ...second instance of that sheet
    assert "/Amp/Deep/DEEP_LOCAL" in names        # grandchild local label
    assert "/Amp/Deep/DEEP_OUT" in names          # hier label, bare parent pin
    assert "AMP_GLOBAL" in names                  # global label: never qualified
    assert "GND" in names                         # power port: never qualified


@pytest.mark.parametrize(
    "fixture", [GROUND_TRUTH, HIER_GROUND_TRUTH], ids=lambda p: p.stem
)
def test_ground_truth_fixture_carries_uuid_identity(fixture: Path):
    """Guard the fixture itself: it must be the UUID-bearing KiCad grammar."""
    text = fixture.read_text(encoding="utf-8")
    tstamps = _parse_netlist(text).tstamps
    assert tstamps and all(len(p.split("/")[-1]) == 36 for p in tstamps.values())
    # sanitized on commit: no absolute dev path, no wall-clock date
    assert '(date "")' in text
    assert "/Users/" not in text and "C:\\" not in text


# --------------------------------------------------------------------------- #
# live kicad-cli arbitration (CI KiCad job / any box with KiCad installed)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("fixture", "board"),
    [(GROUND_TRUTH, GROUND_TRUTH_BOARD), (HIER_GROUND_TRUTH, HIER_BOARD)],
    ids=lambda p: Path(p).stem,
)
def test_committed_fixture_still_matches_live_kicad_cli(fixture: Path, board: Path):
    """The committed ground truth must still be what KiCad produces today."""
    if not kicad_cli.available():
        pytest.skip("kicad-cli not installed (runs in the CI KiCad job)")
    live = _kicad_cli_netlist(board)
    if live is None:
        pytest.skip("kicad-cli produced no netlist for the ground-truth board")
    assert _parse_netlist(live) == _parse_netlist(fixture.read_text(encoding="utf-8"))


@pytest.mark.parametrize("board", CORPUS_BOARDS, ids=lambda p: p.stem)
def test_kicad_cli_agrees_on_corpus_board(board: Path):
    """Every corpus board: KiCad's own netlist and akcli's must agree semantically."""
    if not kicad_cli.available():
        pytest.skip("kicad-cli not installed (runs in the CI KiCad job)")
    live = _kicad_cli_netlist(board)
    if live is None:
        pytest.skip(f"kicad-cli produced no netlist for {board.name}")
    _assert_semantically_equal(live, board)
