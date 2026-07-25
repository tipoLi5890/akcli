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
``(tool)``, sheet-name localization and net *names* (KiCad prefixes local names
with the sheet path) are all out of scope. What must agree:

1. the set of ``(comp (ref ...))`` designators,
2. the net partition as sets of ``(ref, pin)`` members,
3. the full UUID tstamp path per ref (``sheetpath`` tstamps + comp tstamps).

One documented normalization: ``#``-prefixed refs (power ports, ``PWR_FLAG``,
every virtual symbol) are dropped from BOTH sides. KiCad omits them from the
netlist entirely — components *and* nodes — while akcli's model keeps them,
because its net engine derives net NAMES from those power ports; a net left with
no non-virtual member (a bare ``PWR_FLAG`` stub) therefore drops out on both
sides.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akcli import exporters
from akcli.drivers import kicad_cli
from akcli.readers import kicad as kreader
from akcli.readers import sexpr

CORPUS = Path(__file__).parent / "fixtures" / "corpus"
GROUND_TRUTH = Path(__file__).parent / "fixtures" / "kicad" / "netlist_power_entry.kicad10.net"
GROUND_TRUTH_BOARD = CORPUS / "power_entry.kicad_sch"

CORPUS_BOARDS = sorted(CORPUS.glob("*.kicad_sch"))


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


def _parse_netlist(text: str) -> tuple[set[str], set[frozenset[tuple[str, str]]], dict[str, str]]:
    """``(refs, net member sets, ref -> tstamp path)`` from a netlist string."""
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
    for net in nets_node.find_all("net"):
        members = frozenset(
            (_sub(node, "ref") or "", _sub(node, "pin") or "")
            for node in net.find_all("node")
            if not _virtual(_sub(node, "ref") or "")
        )
        if members:
            nets.add(members)
    return refs, nets, tstamps


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
    k_refs, k_nets, k_tstamps = _parse_netlist(kicad_text)
    a_refs, a_nets, a_tstamps = _parse_netlist(_akcli_netlist(board))

    assert a_refs == k_refs, f"component set differs: {a_refs ^ k_refs}"
    assert a_nets == k_nets, (
        "net partition differs\n"
        f"  only akcli: {sorted(sorted(n) for n in a_nets - k_nets)}\n"
        f"  only kicad: {sorted(sorted(n) for n in k_nets - a_nets)}"
    )
    assert a_tstamps == k_tstamps, "UUID tstamp path differs per ref: " + str(
        {r: (a_tstamps.get(r), k_tstamps.get(r)) for r in k_refs
         if a_tstamps.get(r) != k_tstamps.get(r)}
    )


# --------------------------------------------------------------------------- #
# committed ground truth (runs everywhere)
# --------------------------------------------------------------------------- #
def test_netlist_matches_committed_kicad_fixture():
    """akcli's netlist agrees with a committed real ``kicad-cli`` export."""
    _assert_semantically_equal(GROUND_TRUTH.read_text(encoding="utf-8"), GROUND_TRUTH_BOARD)


def test_ground_truth_fixture_carries_uuid_identity():
    """Guard the fixture itself: it must be the UUID-bearing KiCad grammar."""
    text = GROUND_TRUTH.read_text(encoding="utf-8")
    _, _, tstamps = _parse_netlist(text)
    assert tstamps and all(len(p.split("/")[-1]) == 36 for p in tstamps.values())
    # sanitized on commit: no absolute dev path, no wall-clock date
    assert '(date "")' in text
    assert "/Users/" not in text and "C:\\" not in text


# --------------------------------------------------------------------------- #
# live kicad-cli arbitration (CI KiCad job / any box with KiCad installed)
# --------------------------------------------------------------------------- #
def test_committed_fixture_still_matches_live_kicad_cli():
    """The committed ground truth must still be what KiCad produces today."""
    if not kicad_cli.available():
        pytest.skip("kicad-cli not installed (runs in the CI KiCad job)")
    live = _kicad_cli_netlist(GROUND_TRUTH_BOARD)
    if live is None:
        pytest.skip("kicad-cli produced no netlist for the ground-truth board")
    fixture = GROUND_TRUTH.read_text(encoding="utf-8")
    assert _parse_netlist(live) == _parse_netlist(fixture)


@pytest.mark.parametrize("board", CORPUS_BOARDS, ids=lambda p: p.stem)
def test_kicad_cli_agrees_on_corpus_board(board: Path):
    """Every corpus board: KiCad's own netlist and akcli's must agree semantically."""
    if not kicad_cli.available():
        pytest.skip("kicad-cli not installed (runs in the CI KiCad job)")
    live = _kicad_cli_netlist(board)
    if live is None:
        pytest.skip(f"kicad-cli produced no netlist for {board.name}")
    _assert_semantically_equal(live, board)
