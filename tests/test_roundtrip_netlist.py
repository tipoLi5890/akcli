"""Netlist-preservation round-trips through the writer (zero extra dependencies).

Two layers, both with fixed seeds:

* **Identity**: applying an EMPTY op-list with ``apply=True`` forces the full
  read -> serialize -> atomic-write path over every ``tests/fixtures/kicad``
  schematic; the re-read net membership sets (and their names) must be
  identical to the original's. This pins "serialization never changes
  connectivity" independently of the byte-identity gate.

* **Seeded prediction**: small generated op-lists (grid-aligned placements,
  pin-to-pin wires, labels-on-pins — the documented robust connectivity
  patterns) applied to a blank sheet. The netlist is predicted from op
  semantics alone (union-find over pins: wires join endpoints, same-name
  labels join their pins); a dry-run must succeed without touching the file,
  and the netlist read back from the ``apply=True`` file must equal the
  prediction exactly — every pin accounted for, named nets carrying the
  predicted name.

Layout invariants the generator maintains (so predictions are exact): columns
600 mil apart (no accidental coincident pins), wires only between the
vertically aligned facing pins of one column (straight, endpoints on pin tips
-> no dangles, no mid-span taps), at most one label name per connected
cluster (no multi-name canonical-name ambiguity).
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import pytest

from altium_kicad_cli.readers import kicad as kreader
from altium_kicad_cli.writers import kicad as kw

FIX = Path(__file__).parent / "fixtures" / "kicad"
DEVICE = FIX / "symbols" / "Device.kicad_sym"
FIXTURES = sorted(FIX.glob("*.kicad_sch"))

BLANK_SHEET = (
    '(kicad_sch (version 20231120) (generator "akcli") '
    '(uuid "11111111-2222-3333-4444-555555555555") (paper "A4"))\n'
)

IDENTITY = {"protocol_version": 1, "target_format": "kicad", "ops": []}


def _netlist(path: Path) -> set[tuple[str | None, frozenset]]:
    """(name, membership-set) pairs — the connectivity-relevant net identity."""
    sch = kreader.read_sch(str(path))
    return {
        (n.name, frozenset(tuple(m) for m in n.members))
        for n in sch.nets
    }


# --------------------------------------------------------------------------- #
# identity round-trip over every fixture
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_identity_apply_preserves_netlist(fixture, tmp_path):
    before = _netlist(fixture)
    assert before, f"{fixture.name}: fixture has no nets to compare"

    tgt = tmp_path / fixture.name
    shutil.copyfile(fixture, tgt)
    results = kw.apply(IDENTITY, str(tgt), apply=True)
    assert results == []                       # no ops -> no per-op results

    assert _netlist(tgt) == before


# --------------------------------------------------------------------------- #
# seeded layer: generated op-lists vs. the predicted netlist
# --------------------------------------------------------------------------- #
_NET_POOL = ("NA", "NB", "NC", "ND")


class _DSU:
    def __init__(self) -> None:
        self._p: dict = {}

    def add(self, x) -> None:
        self._p.setdefault(x, x)

    def find(self, x):
        self.add(x)
        while self._p[x] != x:
            x = self._p[x]
        return x

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._p[rb] = ra

    def clusters(self) -> dict:
        out: dict = {}
        for x in self._p:
            out.setdefault(self.find(x), set()).add(x)
        return out


def _generate(rng: random.Random):
    """A small valid op-list plus its predicted netlist.

    Returns ``(ops, partition, names)`` where ``partition`` is the full
    expected set of net membership frozensets (single-pin nets included:
    ``read_sch`` emits them) and ``names`` maps each labeled membership set to
    its one predicted net name.
    """
    ops_list: list[dict] = []
    dsu = _DSU()
    by_name: dict[str, list[tuple[str, str]]] = {}

    def _label(name: str, ref: str, pin: str, scope: str) -> None:
        op = {"op": "add_net_label", "name": name, "at": f"{ref}.{pin}"}
        if scope != "local":
            op["scope"] = scope
        ops_list.append(op)
        by_name.setdefault(name, []).append((ref, pin))

    for i in range(rng.randrange(2, 5)):
        x = 1000 + 600 * i                     # columns clear of each other
        top, bot = f"R{10 + i}", f"C{10 + i}"
        # Fixture Device:R / Device:C pins are 150 mil above/below the anchor:
        # top pin tips (x, 850)/(x, 1150), bottom pin tips (x, 1450)/(x, 1750).
        ops_list.append({"op": "place_component", "lib_id": "Device:R",
                         "designator": top, "x_mil": x, "y_mil": 1000,
                         "value": "1k"})
        ops_list.append({"op": "place_component", "lib_id": "Device:C",
                         "designator": bot, "x_mil": x, "y_mil": 1600,
                         "value": "100n"})
        for pin in ("1", "2"):
            dsu.add((top, pin))
            dsu.add((bot, pin))
        if rng.random() < 0.7:
            # straight vertical pin-to-pin wire: both endpoints terminate on
            # pin tips, nothing else shares this x column -> no dangles/shorts
            ops_list.append({"op": "add_wire",
                             "vertices": [f"{top}.2", f"{bot}.1"]})
            dsu.union((top, "2"), (bot, "1"))
            if rng.random() < 0.6:             # <= ONE name per wired cluster
                name = rng.choice(_NET_POOL)
                _label(name, top, "2", "local")
                if rng.random() < 0.5:
                    _label(name, bot, "1", "local")
        for ref, pin in ((top, "1"), (bot, "2")):
            if rng.random() < 0.6:
                _label(rng.choice(_NET_POOL), ref, pin,
                       rng.choice(("local", "global")))

    for name, pins in by_name.items():         # same-name labels join clusters
        for p in pins[1:]:
            dsu.union(pins[0], p)

    clusters = dsu.clusters()
    partition = {frozenset(c) for c in clusters.values()}
    names: dict[frozenset, str] = {}
    for name, pins in by_name.items():
        key = frozenset(clusters[dsu.find(pins[0])])
        assert names.get(key, name) == name, "generator broke 1-name-per-cluster"
        names[key] = name
    return ops_list, partition, names


@pytest.mark.parametrize("seed", range(6))
def test_seeded_oplist_netlist_matches_prediction(seed, tmp_path):
    rng = random.Random(seed)
    ops_list, partition, names = _generate(rng)
    doc = {"protocol_version": 1, "target_format": "kicad", "ops": ops_list}

    dry = tmp_path / "dry.kicad_sch"
    wet = tmp_path / "wet.kicad_sch"
    dry.write_text(BLANK_SHEET)
    wet.write_text(BLANK_SHEET)

    # dry-run: clean verify, every op ok, file untouched
    verify: list = []
    dry_results = kw.apply(doc, str(dry), apply=False,
                           sources=[str(DEVICE)], verify_out=verify)
    assert [(r.status, r.error_code) for r in dry_results] == \
        [("ok", None)] * len(ops_list)
    assert verify == [], [f.code for f in verify]
    assert dry.read_text() == BLANK_SHEET

    # apply: identical per-op outcome, then the written netlist == prediction
    wet_results = kw.apply(doc, str(wet), apply=True, sources=[str(DEVICE)])
    assert [r.to_dict() for r in wet_results] == [r.to_dict() for r in dry_results]

    sch = kreader.read_sch(str(wet))
    actual = {frozenset(tuple(m) for m in n.members) for n in sch.nets}
    assert actual == partition, (
        f"seed={seed}: missing={partition - actual} extra={actual - partition}"
    )
    for net in sch.nets:
        key = frozenset(tuple(m) for m in net.members)
        if key in names:
            assert net.name == names[key], f"seed={seed}: {key}"
        else:
            assert not net.is_named, f"seed={seed}: unexpected name {net.name!r}"
