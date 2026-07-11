"""Tests for :mod:`altium_kicad_cli.checks.libsync`.

With sources: LIB_EMBED_STALE fires on pin-signature drift (moved pin, renamed
pin number) but NOT on graphics-only drift; unreadable/missing sources are
skipped. Without sources: LIB_EMBED_OLD_FORMAT fires on the version-token
telltales (pre-v8 document version, symbols missing ``exclude_from_sim``) and
stays silent on a current-format cache. Non-.kicad_sch inputs are skipped with
an INFO finding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from altium_kicad_cli.checks import libsync
from altium_kicad_cli.report import Severity

# --------------------------------------------------------------------------- #
# fixture text (library block reused verbatim as the fresh embed)
# --------------------------------------------------------------------------- #
_R2 = (
    '\t(symbol "R2"\n'
    "\t\t(exclude_from_sim no)\n"
    "\t\t(in_bom yes)\n"
    "\t\t(on_board yes)\n"
    '\t\t(property "Reference" "R"\n'
    "\t\t\t(at 2.032 0 90)\n"
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


def _lib_text(body: str = _R2) -> str:
    return (
        "(kicad_symbol_lib\n"
        "\t(version 20231120)\n"
        '\t(generator "test")\n' + body + ")\n"
    )


def _sch_text(embed: str, version: int = 20231120) -> str:
    return (
        "(kicad_sch\n"
        f"\t(version {version})\n"
        '\t(generator "test")\n'
        '\t(uuid "00000000-0000-4000-8000-00000000abcd")\n'
        '\t(paper "A4")\n'
        "\t(lib_symbols\n" + embed + "\t)\n"
        ")\n"
    )


_EMBED = _R2.replace('(symbol "R2"', '(symbol "Fake:R2"', 1)


@pytest.fixture()
def sch(tmp_path: Path) -> Path:
    p = tmp_path / "board.kicad_sch"
    p.write_text(_sch_text(_EMBED), encoding="utf-8")
    return p


def _libdir(tmp_path: Path, name: str, text: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "Fake.kicad_sym").write_text(text, encoding="utf-8")
    return d


# --------------------------------------------------------------------------- #
# LIB_EMBED_STALE (sources given)
# --------------------------------------------------------------------------- #
def test_stale_on_moved_pin(tmp_path: Path, sch: Path):
    moved = _libdir(tmp_path, "libs", _lib_text().replace(
        "(at 0 3.81 270)", "(at 0 2.54 270)"))
    findings = libsync.run(sch, [moved])
    assert [f.code for f in findings] == [libsync.LIB_EMBED_STALE]
    f = findings[0]
    assert f.severity is Severity.WARNING
    assert "Fake:R2" in f.refs
    assert "relink-symbols" in f.message
    assert "pins 1" in f.message           # names the drifted pin


def test_stale_on_renamed_pin_number(tmp_path: Path, sch: Path):
    renamed = _libdir(tmp_path, "libs", _lib_text().replace(
        '(number "2"', '(number "3"'))
    findings = libsync.run(sch, [renamed])
    assert [f.code for f in findings] == [libsync.LIB_EMBED_STALE]


def test_graphics_only_drift_is_not_stale(tmp_path: Path, sch: Path):
    """Graphics can't change connectivity; relink lists it, this check doesn't."""
    graphics = _libdir(tmp_path, "libs", _lib_text().replace("1.016", "1.27"))
    assert libsync.run(sch, [graphics]) == []


def test_identical_source_is_clean(tmp_path: Path, sch: Path):
    same = _libdir(tmp_path, "libs", _lib_text())
    assert libsync.run(sch, [same]) == []


def test_missing_source_lib_is_skipped(tmp_path: Path, sch: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert libsync.run(sch, [empty]) == []


def test_source_may_be_a_kicad_sym_file(tmp_path: Path, sch: Path):
    moved = _libdir(tmp_path, "libs", _lib_text().replace(
        "(at 0 -3.81 90)", "(at 0 -5.08 90)"))
    findings = libsync.run(sch, [moved / "Fake.kicad_sym"])
    assert [f.code for f in findings] == [libsync.LIB_EMBED_STALE]


# --------------------------------------------------------------------------- #
# LIB_EMBED_OLD_FORMAT (no sources given)
# --------------------------------------------------------------------------- #
def test_old_format_note_on_missing_exclude_from_sim(tmp_path: Path):
    old_embed = _EMBED.replace("\t\t(exclude_from_sim no)\n", "")
    p = tmp_path / "old.kicad_sch"
    p.write_text(_sch_text(old_embed), encoding="utf-8")
    findings = libsync.run(p)
    assert [f.code for f in findings] == [libsync.LIB_EMBED_OLD_FORMAT]
    f = findings[0]
    assert f.severity is Severity.NOTE
    assert "relink-symbols" in f.message
    assert "Fake:R2" in f.refs


def test_old_format_note_on_pre_v8_version(tmp_path: Path):
    p = tmp_path / "old.kicad_sch"
    p.write_text(_sch_text(_EMBED, version=20211123), encoding="utf-8")
    findings = libsync.run(p)
    assert [f.code for f in findings] == [libsync.LIB_EMBED_OLD_FORMAT]
    assert "20211123" in findings[0].message


def test_current_format_is_clean_without_sources(sch: Path):
    assert libsync.run(sch) == []


def test_empty_cache_is_clean(tmp_path: Path):
    p = tmp_path / "empty.kicad_sch"
    p.write_text(_sch_text(""), encoding="utf-8")
    assert libsync.run(p) == []
    assert libsync.run(p, [tmp_path]) == []


def test_non_kicad_sch_is_skipped_with_info(tmp_path: Path):
    p = tmp_path / "board.SchDoc"
    p.write_text("not a schematic", encoding="utf-8")
    findings = libsync.run(p)
    assert len(findings) == 1
    assert findings[0].severity is Severity.INFO
    assert "skipped" in findings[0].message
