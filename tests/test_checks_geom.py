"""Tests for :mod:`altium_kicad_cli.checks.geom` — wire/pin/label attachment lint.

The module's contract is that it mirrors ``netbuild`` exactly (it is built from
the same ``_q`` / ``_on_seg`` helpers), so alongside the finding assertions the
mid-span tests ALSO read the fixture through the real KiCad reader and assert
that the net engine agrees with the lint: touch-without-junction really is
disconnected, junction really connects.

Fixtures are handcrafted ``.kicad_sch`` text (the op-list writer refuses to
produce most of these defects — its connectivity gate is exactly what these
checks retrofit onto files drawn by other tools).
"""

from __future__ import annotations

from pathlib import Path

from altium_kicad_cli.checks import geom
from altium_kicad_cli.readers import kicad
from altium_kicad_cli.report import Finding, Severity

# Device:R with pin 1 at lib (0, 3.81) and pin 2 at (0, -3.81) — placed at
# (25.4, 25.4) mm the world pin tips are (25.4, 21.59) and (25.4, 29.21).
_R_LIB = """\
(symbol "Device:R" (pin_numbers (hide yes)) (pin_names (offset 0))
      (property "Reference" "R" (at 2.032 0 90) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 0 90) (effects (font (size 1.27 1.27))))
      (symbol "R_0_1"
        (rectangle (start -1.016 -2.54) (end 1.016 2.54)
          (stroke (width 0.254) (type default)) (fill (type none))))
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))))"""

_R1 = """\
(symbol (lib_id "Device:R") (at 25.4 25.4 0) (unit 1)
    (uuid "aaaaaaaa-0000-4000-8000-000000000001")
    (property "Reference" "R1" (at 27.4 25.4 0) (effects (font (size 1.27 1.27))))
    (property "Value" "1k" (at 27.4 26.7 0) (effects (font (size 1.27 1.27)))))"""

PIN1 = (25.4, 21.59)   # mm, R1 pin 1 world tip
PIN2 = (25.4, 29.21)   # mm, R1 pin 2 world tip


def _sch(tmp_path: Path, *body: str) -> Path:
    p = tmp_path / "t.kicad_sch"
    p.write_text(
        '(kicad_sch (version 20231120) (generator "test")\n'
        '  (uuid "11111111-2222-3333-4444-555555555555") (paper "A4")\n'
        f"  (lib_symbols {_R_LIB})\n  "
        + "\n  ".join(body)
        + "\n)\n"
    )
    return p


def _wire(a: tuple[float, float], b: tuple[float, float]) -> str:
    return (f"(wire (pts (xy {a[0]} {a[1]}) (xy {b[0]} {b[1]}))"
            " (stroke (width 0) (type default)))")


def _junction(at: tuple[float, float]) -> str:
    return f"(junction (at {at[0]} {at[1]}) (diameter 0) (color 0 0 0 0))"


def _label(text: str, at: tuple[float, float]) -> str:
    return (f'(label "{text}" (at {at[0]} {at[1]} 0)'
            " (effects (font (size 1.27 1.27))))")


def _codes(findings: list[Finding]) -> set[str]:
    return {f.code for f in findings}


def _by_code(findings: list[Finding], code: str) -> list[Finding]:
    return [f for f in findings if f.code == code]


def _net_of(path: Path, ref: tuple[str, str]):
    sch = kicad.read_sch(path)
    return next((n for n in sch.nets if ref in n.members), None)


# --------------------------------------------------------------------------- #
# clean fixture
# --------------------------------------------------------------------------- #
def test_clean_schematic_has_no_findings(tmp_path):
    tgt = _sch(
        tmp_path,
        _R1,
        _wire(PIN1, (25.4, 16.51)),        # wire ENDS on the pin tip: connected
        _label("SIG", (25.4, 16.51)),      # label on the wire's far end
        _label("BOT", PIN2),               # blessed label-on-pin pattern
    )
    assert geom.run(tgt) == []


# --------------------------------------------------------------------------- #
# NET_PIN_MIDSPAN_TOUCH — must always agree with the net engine
# --------------------------------------------------------------------------- #
def _midspan(tmp_path, *extra: str) -> Path:
    # horizontal wire crossing R1 pin 1's tip mid-span
    return _sch(
        tmp_path,
        _R1,
        _wire((20.32, 21.59), (30.48, 21.59)),
        _label("W", (20.32, 21.59)),
        *extra,
    )


def test_pin_midspan_touch_flagged_and_disconnected(tmp_path):
    tgt = _midspan(tmp_path)
    hits = _by_code(geom.run(tgt), geom.NET_PIN_MIDSPAN_TOUCH)
    assert len(hits) == 1
    assert hits[0].severity is Severity.WARNING
    assert hits[0].refs == ["R1.1"]
    assert "junction" in hits[0].message
    # the engine agrees: R1.1 did NOT join the wire's net
    net = _net_of(tgt, ("R1", "1"))
    assert net is not None and net.name != "W"


def test_junction_connects_and_silences_midspan(tmp_path):
    tgt = _midspan(tmp_path, _junction(PIN1))
    assert geom.NET_PIN_MIDSPAN_TOUCH not in _codes(geom.run(tgt))
    # the engine agrees: with the junction R1.1 IS on the wire's net
    net = _net_of(tgt, ("R1", "1"))
    assert net is not None and net.name == "W"


def test_wire_endpoint_on_pin_is_not_midspan(tmp_path):
    tgt = _sch(tmp_path, _R1, _wire(PIN1, (25.4, 16.51)),
               _label("W", (25.4, 16.51)))
    assert geom.NET_PIN_MIDSPAN_TOUCH not in _codes(geom.run(tgt))
    net = _net_of(tgt, ("R1", "1"))
    assert net is not None and net.name == "W"


# --------------------------------------------------------------------------- #
# NET_LABEL_UNATTACHED
# --------------------------------------------------------------------------- #
def test_floating_label_flagged(tmp_path):
    tgt = _sch(tmp_path, _R1, _label("FLOAT", (50.8, 50.8)))
    hits = _by_code(geom.run(tgt), geom.NET_LABEL_UNATTACHED)
    assert len(hits) == 1
    assert hits[0].severity is Severity.WARNING
    assert hits[0].refs == ["FLOAT"]


def test_label_on_pin_tip_is_attached(tmp_path):
    tgt = _sch(tmp_path, _R1, _label("ONPIN", PIN1))
    assert geom.NET_LABEL_UNATTACHED not in _codes(geom.run(tgt))


def test_label_mid_wire_is_attached(tmp_path):
    tgt = _sch(
        tmp_path,
        _R1,
        _wire(PIN1, (25.4, 16.51)),
        _label("MID", (25.4, 19.05)),      # anywhere along the wire counts
    )
    assert geom.NET_LABEL_UNATTACHED not in _codes(geom.run(tgt))


# --------------------------------------------------------------------------- #
# NET_WIRE_CORNER_ON_PIN
# --------------------------------------------------------------------------- #
def test_wire_corner_on_pin_is_a_note(tmp_path):
    # two perpendicular segments both ending exactly on R1 pin 1's tip
    tgt = _sch(
        tmp_path,
        _R1,
        _wire(PIN1, (25.4, 16.51)),
        _wire(PIN1, (30.48, 21.59)),
        _label("A", (25.4, 16.51)),
        _label("B", (30.48, 21.59)),
    )
    out = geom.run(tgt)
    hits = _by_code(out, geom.NET_WIRE_CORNER_ON_PIN)
    assert len(hits) == 1
    assert hits[0].severity is Severity.NOTE
    assert hits[0].refs == ["R1.1"]
    # an endpoint connection is not a mid-span touch
    assert geom.NET_PIN_MIDSPAN_TOUCH not in _codes(out)


def test_collinear_wire_split_on_pin_is_not_a_corner(tmp_path):
    # two collinear segments meeting on the tip: a plain split, no corner
    tgt = _sch(
        tmp_path,
        _R1,
        _wire((25.4, 16.51), PIN1),
        _wire(PIN1, (25.4, 11.43)),
        _label("A", (25.4, 16.51)),
        _label("A", (25.4, 11.43)),
    )
    assert geom.NET_WIRE_CORNER_ON_PIN not in _codes(geom.run(tgt))


# --------------------------------------------------------------------------- #
# non-KiCad input
# --------------------------------------------------------------------------- #
def test_skips_non_kicad(tmp_path):
    f = tmp_path / "x.SchDoc"
    f.write_bytes(b"\xd0\xcf\x11\xe0")
    findings = geom.run(f)
    assert len(findings) == 1 and findings[0].severity is Severity.INFO
