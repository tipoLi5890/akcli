"""Tests for :mod:`altium_kicad_cli.relink` (embedded lib_symbols re-embed).

Covers plan statuses (``up-to-date`` / ``replace`` / ``missing-lib``) against a
tiny fake ``.kicad_sym`` library dir, the ``only`` filter, the default-lib-dir
fallback, the apply splice (backup, atomicity, idempotent re-plan) and — most
importantly — the non-negotiable net-membership equivalence gate: a fresh
library whose pins moved must be REFUSED with ``VERIFY_FAILED`` and leave the
schematic untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from altium_kicad_cli import relink
from altium_kicad_cli.errors import AkcliError
from altium_kicad_cli.readers import kicad, sexpr

# --------------------------------------------------------------------------- #
# fixture text
# --------------------------------------------------------------------------- #
# Library blocks are written at .kicad_sym depth (one tab). The schematic embeds
# renamed copies of the SAME text so token-level (in)equality is fully controlled.
_R2 = (
    '\t(symbol "R2"\n'
    "\t\t(exclude_from_sim no)\n"
    "\t\t(in_bom yes)\n"
    "\t\t(on_board yes)\n"
    '\t\t(property "Reference" "R"\n'
    "\t\t\t(at 2.032 0 90)\n"
    "\t\t\t(effects (font (size 1.27 1.27))))\n"
    '\t\t(property "Value" "R2"\n'
    "\t\t\t(at 0 0 90)\n"
    "\t\t\t(effects (font (size 1.27 1.27))))\n"
    '\t\t(symbol "R2_0_1"\n'
    "\t\t\t(rectangle\n"
    "\t\t\t\t(start -1.016 -2.54)\n"
    "\t\t\t\t(end 1.016 2.54)\n"
    "\t\t\t\t(stroke (width 0.254) (type default))\n"
    "\t\t\t\t(fill (type none))))\n"
    '\t\t(symbol "R2_1_1"\n'
    "\t\t\t(pin passive line\n"
    "\t\t\t\t(at 0 3.81 270)\n"
    "\t\t\t\t(length 1.27)\n"
    '\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))\n'
    '\t\t\t\t(number "1" (effects (font (size 1.27 1.27)))))\n'
    "\t\t\t(pin passive line\n"
    "\t\t\t\t(at 0 -3.81 90)\n"
    "\t\t\t\t(length 1.27)\n"
    '\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))\n'
    '\t\t\t\t(number "2" (effects (font (size 1.27 1.27)))))))\n'
)

_C2 = (
    '\t(symbol "C2"\n'
    "\t\t(exclude_from_sim no)\n"
    "\t\t(in_bom yes)\n"
    "\t\t(on_board yes)\n"
    '\t\t(property "Reference" "C"\n'
    "\t\t\t(at 0.635 2.54 0)\n"
    "\t\t\t(effects (font (size 1.27 1.27))))\n"
    '\t\t(property "Value" "C2"\n'
    "\t\t\t(at 0.635 -2.54 0)\n"
    "\t\t\t(effects (font (size 1.27 1.27))))\n"
    '\t\t(symbol "C2_1_1"\n'
    "\t\t\t(pin passive line\n"
    "\t\t\t\t(at 0 3.81 270)\n"
    "\t\t\t\t(length 2.54)\n"
    '\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))\n'
    '\t\t\t\t(number "1" (effects (font (size 1.27 1.27)))))\n'
    "\t\t\t(pin passive line\n"
    "\t\t\t\t(at 0 -3.81 90)\n"
    "\t\t\t\t(length 2.54)\n"
    '\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))\n'
    '\t\t\t\t(number "2" (effects (font (size 1.27 1.27)))))))\n'
)


def _lib_text() -> str:
    return (
        "(kicad_symbol_lib\n"
        "\t(version 20231120)\n"
        '\t(generator "test")\n' + _R2 + _C2 + ")\n"
    )


# Embedded copies: R2 is deliberately STALE (graphics-only: rectangle grew),
# C2 is token-identical (only the parent name is qualified), Ghost:X has no
# source library at all.
_EMB_R2_STALE = _R2.replace('(symbol "R2"', '(symbol "Fake:R2"', 1).replace(
    "1.016", "1.27"
)
_EMB_C2_FRESH = _C2.replace('(symbol "C2"', '(symbol "Fake:C2"', 1)
_EMB_GHOST = (
    '\t(symbol "Ghost:X"\n'
    "\t\t(exclude_from_sim no)\n"
    '\t\t(property "Reference" "U"\n'
    "\t\t\t(at 0 0 0))\n"
    '\t\t(symbol "X_1_1"\n'
    "\t\t\t(pin passive line\n"
    "\t\t\t\t(at 0 0 0)\n"
    "\t\t\t\t(length 2.54)\n"
    '\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))\n'
    '\t\t\t\t(number "1" (effects (font (size 1.27 1.27)))))))\n'
)


def _placed(ref: str, x: float, y: float, uid: str) -> str:
    return (
        "\t(symbol\n"
        '\t\t(lib_id "Fake:R2")\n'
        f"\t\t(at {x} {y} 0)\n"
        "\t\t(unit 1)\n"
        f'\t\t(uuid "{uid}")\n'
        f'\t\t(property "Reference" "{ref}"\n'
        "\t\t\t(at 0 0 0))\n"
        '\t\t(property "Value" "R2"\n'
        "\t\t\t(at 0 0 0)))\n"
    )


def _sch_text() -> str:
    # R1 at (100,100): pins land at y=96.19 (pin 1) / 103.81 (pin 2);
    # R2 at (100,110): pins at 106.19 / 113.81. The wire joins R1.2 <-> R2.1.
    return (
        "(kicad_sch\n"
        "\t(version 20231120)\n"
        '\t(generator "test")\n'
        '\t(uuid "00000000-0000-4000-8000-00000000abcd")\n'
        '\t(paper "A4")\n'
        "\t(lib_symbols\n" + _EMB_R2_STALE + _EMB_C2_FRESH + _EMB_GHOST + "\t)\n"
        "\t(wire (pts (xy 100 103.81) (xy 100 106.19))\n"
        "\t\t(stroke (width 0) (type default))\n"
        '\t\t(uuid "00000000-0000-4000-8000-000000000101"))\n'
        + _placed("R1", 100, 100, "00000000-0000-4000-8000-000000000001")
        + _placed("R2", 100, 110, "00000000-0000-4000-8000-000000000002")
        + ")\n"
    )


@pytest.fixture()
def proj(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(schematic, good lib dir, bad lib dir with MOVED pins)."""
    libdir = tmp_path / "libs"
    libdir.mkdir()
    (libdir / "Fake.kicad_sym").write_text(_lib_text(), encoding="utf-8")

    baddir = tmp_path / "libs_bad"
    baddir.mkdir()
    (baddir / "Fake.kicad_sym").write_text(
        _lib_text()
        .replace("(at 0 3.81 270)", "(at 0 2.54 270)")
        .replace("(at 0 -3.81 90)", "(at 0 -2.54 90)"),
        encoding="utf-8",
    )

    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_sch_text(), encoding="utf-8")
    return sch, libdir, baddir


def _statuses(actions: list[dict]) -> dict[str, str]:
    return {a["lib_id"]: a["status"] for a in actions}


def _membership(path: Path) -> set[frozenset]:
    return {frozenset(n.members) for n in kicad.read_sch(path).nets}


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def test_plan_statuses(proj):
    sch, libdir, _ = proj
    actions = relink.plan(sch, [libdir])
    assert _statuses(actions) == {
        "Fake:R2": "replace",
        "Fake:C2": "up-to-date",
        "Ghost:X": "missing-lib",
    }
    rep = next(a for a in actions if a["lib_id"] == "Fake:R2")
    assert rep["new_sexpr"].startswith('(symbol "Fake:R2"')
    assert rep["source"].endswith("Fake.kicad_sym")
    # the fresh block carries the LIBRARY graphics, not the stale embed's
    assert "(start -1.016 -2.54)" in rep["new_sexpr"]


def test_plan_accepts_kicad_sym_file_as_source(proj):
    sch, libdir, _ = proj
    actions = relink.plan(sch, [libdir / "Fake.kicad_sym"])
    assert _statuses(actions)["Fake:R2"] == "replace"


def test_plan_only_filters_by_nick(proj):
    sch, libdir, _ = proj
    assert _statuses(relink.plan(sch, [libdir], only=["Fake"])) == {
        "Fake:R2": "replace",
        "Fake:C2": "up-to-date",
    }
    # comma-separated string form; full lib_id also accepted
    assert _statuses(relink.plan(sch, [libdir], only="Ghost,Fake:C2")) == {
        "Fake:C2": "up-to-date",
        "Ghost:X": "missing-lib",
    }


def test_plan_default_lib_dir(proj, monkeypatch):
    sch, libdir, _ = proj
    monkeypatch.setattr(relink, "DEFAULT_LIB_DIR", libdir)
    assert _statuses(relink.plan(sch))["Fake:R2"] == "replace"
    monkeypatch.setattr(relink, "DEFAULT_LIB_DIR", libdir / "nope")
    assert set(_statuses(relink.plan(sch)).values()) == {"missing-lib"}


def test_plan_rejects_non_schematic(tmp_path: Path):
    p = tmp_path / "board.kicad_sch"
    p.write_text("(kicad_pcb)\n", encoding="utf-8")
    with pytest.raises(AkcliError) as ei:
        relink.plan(p, [])
    assert ei.value.code == "ALTIUM_MALFORMED"


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def test_apply_splices_replacement_and_backs_up(proj):
    sch, libdir, _ = proj
    original = sch.read_text(encoding="utf-8")
    before = _membership(sch)

    res = relink.apply(sch, relink.plan(sch, [libdir]))
    assert res["written"] is True
    assert res["replaced"] == ["Fake:R2"]

    bak = Path(res["backup"])
    assert bak == sch.with_name(sch.name + ".bak")
    assert bak.read_text(encoding="utf-8") == original

    new_text = sch.read_text(encoding="utf-8")
    assert "(start -1.016 -2.54)" in new_text     # fresh graphics spliced in
    sexpr.parse(new_text)                          # still a valid document
    assert _membership(sch) == before              # connectivity preserved
    # idempotent: a re-plan now reports the replaced entry as up-to-date
    assert _statuses(relink.plan(sch, [libdir]))["Fake:R2"] == "up-to-date"


def test_apply_without_backup(proj):
    sch, libdir, _ = proj
    res = relink.apply(sch, relink.plan(sch, [libdir]), backup=False)
    assert res["written"] is True and res["backup"] is None
    assert not sch.with_name(sch.name + ".bak").exists()


def test_apply_noop_without_replace_actions(proj):
    sch, libdir, _ = proj
    original = sch.read_text(encoding="utf-8")
    actions = [a for a in relink.plan(sch, [libdir]) if a["status"] != "replace"]
    res = relink.apply(sch, actions)
    assert res == {"path": str(sch), "replaced": [], "backup": None, "written": False}
    assert sch.read_text(encoding="utf-8") == original


def test_apply_refuses_connectivity_change(proj):
    """The safety gate: moved pins would rewire the board -> VERIFY_FAILED."""
    sch, _, baddir = proj
    original = sch.read_text(encoding="utf-8")
    actions = relink.plan(sch, [baddir])
    assert _statuses(actions)["Fake:R2"] == "replace"

    with pytest.raises(AkcliError) as ei:
        relink.apply(sch, actions)
    assert ei.value.code == "VERIFY_FAILED"
    assert "connectivity" in ei.value.message

    assert sch.read_text(encoding="utf-8") == original   # untouched
    assert not sch.with_name(sch.name + ".bak").exists()  # no backup either
    assert not list(sch.parent.glob("*.tmp"))              # no temp leftovers


def test_apply_unknown_cache_entry_fails(proj):
    sch, _, _ = proj
    action = {"lib_id": "Nope:Z", "status": "replace",
              "new_sexpr": '(symbol "Nope:Z")'}
    with pytest.raises(AkcliError) as ei:
        relink.apply(sch, [action])
    assert ei.value.code == "SYMBOL_NOT_FOUND"


def test_apply_malformed_action_fails(proj):
    sch, _, _ = proj
    with pytest.raises(AkcliError) as ei:
        relink.apply(sch, [{"lib_id": "Fake:R2", "status": "replace"}])
    assert ei.value.code == "BAD_CONFIG"
