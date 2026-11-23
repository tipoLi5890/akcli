"""Fixture-first ground truth for ``akcli fab check``: a REAL KiCad measures the
boards akcli reads, live.

``tests/test_kicad_pcb_fab_facts.py`` holds the reader against committed
evidence (boards kicad-cli 10.0.4 wrote, and the statistics it exported for
them). This file is what keeps that evidence honest wherever KiCad is
installed:

* the committed ``*.stats.kicad10.json`` are re-derived with ``kicad-cli pcb
  export stats`` and must still say the same thing — and the reader must
  agree with the live numbers (outline area, cutouts, via types, drill
  table);
* the hand-authored LEGACY inputs (KiCad 5 centre/angle arcs + ``module``,
  KiCad 9 bare tenting tokens, the KiCad <= 8 ``viasonmask`` plot option) are
  re-saved with ``kicad-cli pcb upgrade`` and the reader must describe the
  re-save exactly as it describes the committed one — i.e. akcli reads those
  old shapes the way KiCad itself does;
* the template boards KiCad ships (written by KiCad 9) are measured by both.

KiCad tessellates arcs (5 µm error), so its areas sit slightly under the
exact value; comparisons are relative.

The skip reason string is load-bearing: ``.github/workflows/ci.yml`` greps for
``kicad-cli not installed``. ``pcb export stats`` exists from KiCad 10 on;
older installs skip with a different, capability-specific reason.
"""

from __future__ import annotations

import json
import math
import shutil
from collections import Counter
from pathlib import Path

import pytest

from akcli import safety
from akcli.drivers import kicad_cli
from akcli.readers import kicad, kicad_pro

FAB = Path(__file__).parent / "fixtures" / "kicad" / "fab"
_TIMEOUT = 180.0
KICAD_REL = 3e-4

_EXE = kicad_cli.find()
needs_cli = pytest.mark.skipif(_EXE is None, reason="kicad-cli not installed")
_VERSION = kicad_cli.version() if _EXE is not None else None
needs_stats = pytest.mark.skipif(
    _VERSION is not None and _VERSION < (10,),
    reason="this kicad-cli predates `pcb export stats` (KiCad 10)")
needs_pcb_upgrade = pytest.mark.skipif(
    _VERSION is not None and _VERSION < (10,),
    reason="this kicad-cli predates `pcb upgrade` (KiCad 10)")

KICAD10_BOARDS = ["outline_cutouts", "outline_circle", "outline_poly_arc",
                  "outline_rect_radius", "outline_open", "outline_footprint",
                  "vias_tenting", "slots"]


@pytest.fixture()
def english(tmp_path, monkeypatch):
    """Pin KiCad's UI language: the statistics JSON localizes its via/pad
    keys (the same hazard as the DRC report's descriptions), and a throwaway
    ``KICAD_CONFIG_HOME`` keeps the choice out of the developer's settings."""
    assert _VERSION is not None
    cfg = tmp_path / "kicad-config" / f"{_VERSION[0]}.{_VERSION[1] if len(_VERSION) > 1 else 0}"
    cfg.mkdir(parents=True)
    (cfg / "kicad_common.json").write_text(
        '{"system": {"language": "English"}}\n', encoding="utf-8", newline="\n")
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(cfg.parent))


def _run(*argv: str) -> None:
    assert _EXE is not None
    proc = safety.run_subprocess([_EXE, *argv], _TIMEOUT)
    assert proc.returncode == 0, (argv, proc.stdout, proc.stderr)


def _staged(tmp_path: Path, source: Path, name: str | None = None) -> Path:
    """kicad-cli writes a ``.kicad_prl`` beside any board it loads — always
    run it on a copy, never in the fixture directory."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    dst = work / (name or source.name)
    shutil.copyfile(source, dst)
    return dst


def _live_stats(board: Path, *flags: str) -> dict:
    out = board.with_suffix(".stats.json")
    _run("pcb", "export", "stats", "--format", "json", *flags,
         "--output", str(out), str(board))
    return json.loads(out.read_text(encoding="utf-8"))


def _mm(text: str) -> float:
    return float(text.split()[0])


def _drill_table(stats: dict) -> Counter:
    return Counter(
        (h["shape"], _mm(h["x_size"]), _mm(h["y_size"]), h["plated"], h["source"])
        for h in stats["drill_holes"] for _ in range(h["count"]))


def _bodies(outline: dict) -> float:
    return sum(lp["area_mm2"] for lp in outline["loops"] if lp["depth"] == 0)


# --------------------------------------------------------------------------- #
# the committed statistics are what KiCad still says
# --------------------------------------------------------------------------- #
@needs_cli
@needs_stats
@pytest.mark.parametrize("name", KICAD10_BOARDS)
def test_committed_statistics_match_a_live_export(tmp_path, english, name):
    live = _live_stats(_staged(tmp_path, FAB / f"{name}.kicad_pcb"))
    kept = json.loads((FAB / f"{name}.stats.kicad10.json")
                      .read_text(encoding="utf-8"))
    assert live["board"]["has_outline"] == kept["board"]["has_outline"]
    if kept["board"]["area"] is None:
        assert live["board"]["area"] is None
    else:
        assert _mm(live["board"]["area"]) == pytest.approx(
            _mm(kept["board"]["area"]), rel=KICAD_REL)
    assert live["vias"] == kept["vias"]
    assert live["pads"] == kept["pads"]
    assert _drill_table(live) == _drill_table(kept)


# --------------------------------------------------------------------------- #
# the reader agrees with the live measurement
# --------------------------------------------------------------------------- #
@needs_cli
@needs_stats
@pytest.mark.parametrize("name", KICAD10_BOARDS)
def test_reader_agrees_with_live_kicad(tmp_path, english, name):
    board = _staged(tmp_path, FAB / f"{name}.kicad_pcb")
    pcb = kicad.read_pcb(str(board))
    outline = pcb.board["outline"]
    stats = _live_stats(board)

    # outline: closure, body area, and area net of cutouts and drills
    assert outline["closed"] is stats["board"]["has_outline"]
    if outline["closed"]:
        assert _bodies(outline) == pytest.approx(_mm(stats["board"]["area"]),
                                                 rel=KICAD_REL)
        drilled = sum(math.pi * v["drill"] ** 2 / 4 for v in pcb.vias)
        for pad in pcb.pads:
            if pad["drill_size"]:
                w, h = sorted(pad["drill_size"])
                drilled += math.pi * w * w / 4 + w * (h - w)   # a stadium
        net = _live_stats(board, "--subtract-holes-from-board")
        assert outline["area_mm2"] - drilled == pytest.approx(
            _mm(net["board"]["area"]), rel=KICAD_REL)
        # KiCad's extent is that of its tessellated polygon: up to 5 µm inside
        # a true arc on each side
        assert outline["bbox"][1][0] - outline["bbox"][0][0] == pytest.approx(
            _mm(stats["board"]["width"]), abs=0.02)
        assert outline["bbox"][1][1] - outline["bbox"][0][1] == pytest.approx(
            _mm(stats["board"]["height"]), abs=0.02)

    # via types — KiCad counts by token, so a `blind` token on an inner layer
    # pair is "blind" there and buried here; everything else is one-to-one
    ours = Counter(v["type"] for v in pcb.vias)
    theirs = stats["vias"]
    inner_blind = sum(1 for v in pcb.vias if v["type"] == "buried") - theirs["buried"]
    assert inner_blind >= 0
    assert ours["through"] == theirs["through"]
    assert ours["micro"] == theirs["micro"]
    assert ours["blind"] + inner_blind == theirs["blind"]

    # drill table: every pad hole, both dimensions, plated or not
    ours_drills = Counter(
        ("Slot" if p["drill_shape"] == "oval" else "Round",
         *p["drill_size"], p["pad_type"] != "np_thru_hole")
        for p in pcb.pads if p["drill"])
    theirs_drills = Counter(
        (shape, w, h, plated)
        for (shape, w, h, plated, source), n in _drill_table(stats).items()
        if source == "Pad" for _ in range(n))
    assert ours_drills == theirs_drills


# --------------------------------------------------------------------------- #
# legacy grammars: akcli reads them the way kicad-cli re-saves them
# --------------------------------------------------------------------------- #
def _facts(pcb) -> dict:
    outline = pcb.board["outline"]
    return {
        "closed": outline["closed"],
        "area": outline["area_mm2"],
        "bbox": outline["bbox"],
        "depths": [lp["depth"] for lp in outline["loops"]],
        "vias": sorted((v["at"], v["type"], tuple(v["layers"]))
                       for v in pcb.vias),
        "tenting": (pcb.board["tenting"]["front"], pcb.board["tenting"]["back"]),
    }


@needs_cli
@needs_pcb_upgrade
@pytest.mark.parametrize("name", ["legacy_v5", "tenting_v9"])
def test_legacy_input_resaves_to_what_the_reader_already_sees(tmp_path, name):
    source = FAB / f"{name}.kicad_pcb"
    resaved = _staged(tmp_path, source)
    _run("pcb", "upgrade", "--force", str(resaved))

    from_legacy = _facts(kicad.read_pcb(str(source)))
    from_live = _facts(kicad.read_pcb(str(resaved)))
    from_kept = _facts(kicad.read_pcb(str(FAB / f"{name}.kicad10.kicad_pcb")))
    for got in (from_live, from_kept):
        assert got["area"] == pytest.approx(from_legacy["area"], abs=1e-4)
        assert {k: v for k, v in got.items() if k != "area"} == {
            k: v for k, v in from_legacy.items() if k != "area"}


@needs_cli
@needs_pcb_upgrade
def test_kicad9_named_tenting_sides_survive_a_resave(tmp_path):
    """Per-via KiCad 9 tokens: a NAMED side is tented in both readings; an
    unnamed side is the one thing akcli declines to decide (see the reader)."""
    resaved = _staged(tmp_path, FAB / "tenting_v9.kicad_pcb")
    _run("pcb", "upgrade", "--force", str(resaved))
    old = [v["tenting"] for v in kicad.read_pcb(str(FAB / "tenting_v9.kicad_pcb")).vias]
    new = [v["tenting"] for v in kicad.read_pcb(str(resaved)).vias]
    for before, after in zip(old, new, strict=True):
        for side in ("front", "back"):
            assert before[side] in (after[side], None)
    assert old[1] == new[1] == {"front": True, "back": True}


@needs_cli
@needs_pcb_upgrade
@pytest.mark.parametrize("on_mask, tented", [("true", False), ("false", True)])
def test_viasonmask_migrates_to_board_tenting(tmp_path, on_mask, tented):
    """KiCad <= 8 kept "do not tent vias" as a plot option; kicad-cli moves it
    into the board setup exactly as the reader interprets it."""
    text = (FAB / "legacy_v5.kicad_pcb").read_text(encoding="utf-8")
    assert "(viasonmask true)" in text
    work = tmp_path / "work"
    work.mkdir()
    board = work / "legacy.kicad_pcb"
    board.write_text(text.replace("(viasonmask true)", f"(viasonmask {on_mask})"),
                     encoding="utf-8", newline="\n")
    before = kicad.read_pcb(str(board)).board["tenting"]
    assert before == {"front": tented, "back": tented,
                      "source": "pcbplotparams.viasonmask"}
    _run("pcb", "upgrade", "--force", str(board))
    after = kicad.read_pcb(str(board)).board["tenting"]
    assert after == {"front": tented, "back": tented, "source": "setup.tenting"}


# --------------------------------------------------------------------------- #
# boards and projects KiCad itself ships (written by KiCad 9)
# --------------------------------------------------------------------------- #
def _template_dir() -> Path | None:
    if _EXE is None:
        return None
    exe = Path(_EXE).resolve()
    for candidate in (
            exe.parent.parent / "SharedSupport" / "template",      # macOS app
            exe.parent.parent / "share" / "kicad" / "template",    # Linux, Windows
            Path("/usr/share/kicad/template")):
        if candidate.is_dir():
            return candidate
    return None


@needs_cli
@needs_stats
def test_shipped_template_boards_measure_the_same(tmp_path, english):
    """Whenever akcli reports a template's outline as closed, KiCad must agree
    that it is one and measure the same body area. (The converse is not
    asserted: declining to close an outline is the honest fallback.)"""
    root = _template_dir()
    boards = sorted(root.glob("*/*.kicad_pcb")) if root else []
    if not boards:
        pytest.skip("KiCad's template boards are not installed here")
    compared = 0
    for i, source in enumerate(boards):
        board = _staged(tmp_path, source, f"t{i}.kicad_pcb")
        outline = kicad.read_pcb(str(board)).board.get("outline")
        if not outline or not outline["closed"]:
            continue
        stats = _live_stats(board)["board"]
        assert stats["has_outline"] is True, source.name
        assert _bodies(outline) == pytest.approx(_mm(stats["area"]),
                                                 rel=KICAD_REL), source.name
        compared += 1
    assert compared, "no template outline closed — the reader regressed"


@needs_cli
def test_shipped_template_projects_carry_readable_design_rules():
    root = _template_dir()
    projects = sorted(root.glob("*/*.kicad_pro")) if root else []
    if not projects:
        pytest.skip("KiCad's template projects are not installed here")
    for path in projects:
        pro = kicad_pro.read(path)
        assert pro.ok, path.name
        rules = pro.design_rules
        for key in ("min_clearance", "min_track_width", "min_via_diameter",
                    "min_through_hole_diameter", "min_via_annular_width",
                    "min_hole_to_hole"):
            assert isinstance(rules.get(key), float), (path.name, key)
        assert all(math.isfinite(v) and v >= 0 for v in rules.values())
