"""A real-KiCad sch/board/library project for the PCB<->library tests.

``tests/fixtures/kicad/fp_attrs`` holds a board and a footprint library that
``kicad-cli 10.0.4`` wrote (see the fixtures README). :func:`make_project`
copies it into a temp dir and adds the (hand-authored) schematic that board
implements, so a test starts from a pair that is CLEAN on real KiCad output
and provokes one finding with one targeted edit.

Board (6 footprints): R1/R2 ``proj:R_0402``, H1 ``proj:MountingHole_3.2mm``
(board_only), J1 ``proj:PinHeader_1x02``, TP1 ``proj:TestPoint`` — all from the
project ``fp-lib-table`` — and R3 ``Resistor_SMD:R_0402_1005Metric``, which
only the (conftest-controlled) GLOBAL table registers.
"""

from __future__ import annotations

import shutil
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "kicad" / "fp_attrs"
BOARD = "attrs.kicad10.kicad_pcb"

_LIB = """  (lib_symbols
    (symbol "T:P2" (pin_numbers hide) (in_bom yes) (on_board yes)
      (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "P2" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "P2_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "T:P1" (pin_numbers hide) (in_bom yes) (on_board yes)
      (property "Reference" "TP" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "P1" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "P1_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
"""

# (ref, value, footprint, nets per pin, assembly flags) — the schematic the
# fixture board implements. R2 is DNP and out of the BOM on BOTH sides.
PARTS = [
    ("R1", "10k", "proj:R_0402", ["+3V3", "SIG"], ""),
    ("R2", "10k", "proj:R_0402", ["SIG", "GND"],
     "(in_bom no) (on_board yes) (dnp yes)"),
    ("J1", "Conn", "proj:PinHeader_1x02", ["+3V3", "GND"], ""),
    ("TP1", "TP", "proj:TestPoint", ["SIG"], ""),
    ("R3", "1k", "Resistor_SMD:R_0402_1005Metric", ["SIG", "GND"], ""),
]


def sch_text(parts=PARTS, nick: str = "T") -> str:
    """A schematic whose every pin carries a label naming its net.

    ``nick`` is the symbol-library nickname of its two generic symbols
    (``T`` is registered nowhere; ``Device`` is in the conftest global table).
    """
    out = ['(kicad_sch (version 20231120) (generator "eeschema") '
           '(generator_version "8.0")',
           "  (uuid 44444444-4444-4444-4444-444444444444)",
           '  (paper "A4")', _LIB.replace('"T:', f'"{nick}:')]
    for i, (ref, value, footprint, nets, flags) in enumerate(parts):
        x = 100 + 20 * i
        lib = f"{nick}:P2" if len(nets) == 2 else f"{nick}:P1"
        out.append(
            f'  (symbol (lib_id "{lib}") (at {x} 100 0) (unit 1) {flags}\n'
            f"    (uuid 55555555-5555-5555-5555-{i:012d})\n"
            f'    (property "Reference" "{ref}" (at {x + 2} 100 0))\n'
            f'    (property "Value" "{value}" (at {x + 4} 100 0))\n'
            f'    (property "Footprint" "{footprint}" (at {x} 100 0)))')
        for net, y in zip(nets, (96.19, 103.81)):
            out.append(f'  (label "{net}" (at {x} {y} 0))')
    out.append(")")
    return "\n".join(out) + "\n"


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def edit(path: Path, old: str, new: str, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) >= count, f"{old!r} not in {path.name}"
    write(path, text.replace(old, new, count))



def make_project(tmp_path: Path, nick: str = "T") -> Path:
    """A copy of the fixture project + the schematic it implements."""
    d = tmp_path / "proj"
    shutil.copytree(FIX, d)
    write(d / "board.kicad_sch", sch_text(nick=nick))
    return d


def waive(directory: Path, *entries: str) -> None:
    """Write an ``akcli.toml`` holding the given ``[[waiver]]`` bodies."""
    write(directory / "akcli.toml",
          "\n".join(f"[[waiver]]\n{e}\n" for e in entries))
