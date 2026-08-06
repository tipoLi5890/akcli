"""Fixture-first grammar gate: a REAL ``kicad-cli`` must accept what we generate.

Repo rule (CLAUDE.md): a new S-expression shape is proved against a real KiCad
before it ships. Nothing in the pure round-trip tests can catch a grammar that
akcli's own reader is lenient about and KiCad is not — only KiCad can arbitrate
that, so these run in the CI KiCad job and skip elsewhere.

Ground truth committed alongside: ``tests/fixtures/kicad/symlib/grammar_probe.kicad_sym``
(see that directory's README for the claim-by-claim evidence table). The probe
is checked here too, so a KiCad release that stops accepting it fails loudly
instead of silently invalidating the writer's documented grammar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akcli import symbol_spec as ss
from akcli.drivers import kicad_cli
from akcli.model import KICAD_PINTYPE
from akcli.readers import kicad_lib
from akcli.writers import symbol_lib

FIX = Path(__file__).resolve().parent / "fixtures" / "kicad" / "symlib"
PROBE = FIX / "grammar_probe.kicad_sym"


def _need_cli() -> str:
    exe = kicad_cli.find()
    if exe is None:
        pytest.skip("kicad-cli not installed (runs in the CI KiCad job)")
    return exe


def _export_svg(exe: str, lib: Path, out_dir: Path, *, hidden: bool = False):
    from akcli.safety import run_subprocess

    cmd = [exe, "sym", "export", "svg", "--output", str(out_dir)]
    if hidden:
        cmd.append("--include-hidden-pins")
    cmd.append(str(lib.absolute()))
    return run_subprocess(cmd, timeout=120.0)


def _upgrade(exe: str, lib: Path, out: Path):
    from akcli.safety import run_subprocess

    return run_subprocess(
        [exe, "sym", "upgrade", "--force", "--output", str(out),
         str(lib.absolute())], timeout=120.0)


def _write_generated(tmp_path: Path, spec_file: str) -> Path:
    lay = ss.layout(ss.load(FIX / spec_file))
    path = tmp_path / f"{lay.name}.kicad_sym"
    path.write_text(symbol_lib.to_kicad_sym(lay), encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------------------- #
# the generated file
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec_file", ["generic_opamp_8.spec.json",
                                       "generic_logic_14.spec.json"])
def test_generated_symbol_is_accepted_by_kicad_cli(spec_file, tmp_path):
    exe = _need_cli()
    lib = _write_generated(tmp_path, spec_file)
    proc = _export_svg(exe, lib, tmp_path / "svg")
    assert proc.returncode == 0, (proc.stderr or b"").decode("utf-8", "replace")
    assert list((tmp_path / "svg").glob("*.svg")), "kicad-cli drew nothing"


def test_generated_symbol_survives_kicads_own_rewrite(tmp_path):
    """`sym upgrade` re-parses and re-serializes: every pin must come back."""
    exe = _need_cli()
    lib = _write_generated(tmp_path, "generic_logic_14.spec.json")
    out = tmp_path / "upgraded.kicad_sym"
    proc = _upgrade(exe, lib, out)
    assert proc.returncode == 0, (proc.stderr or b"").decode("utf-8", "replace")
    assert out.exists()

    def signature(path: Path):
        sym = kicad_lib.read(path).symbols[0]
        return sorted((p.number, p.name, round(p.x_mil), round(p.y_mil),
                       p.orientation, p.electrical_type.value) for p in sym.pins)

    assert signature(out) == signature(lib)


# --------------------------------------------------------------------------- #
# the committed ground truth
# --------------------------------------------------------------------------- #
def test_grammar_probe_fixture_is_still_accepted(tmp_path):
    exe = _need_cli()
    proc = _export_svg(exe, PROBE, tmp_path / "svg")
    assert proc.returncode == 0, (proc.stderr or b"").decode("utf-8", "replace")


def test_probe_hidden_pin_is_understood_as_hidden(tmp_path):
    """`(hide yes)` on a pin is verified SEMANTICALLY, not just syntactically."""
    exe = _need_cli()
    plain, with_hidden = tmp_path / "a", tmp_path / "b"
    assert _export_svg(exe, PROBE, plain).returncode == 0
    assert _export_svg(exe, PROBE, with_hidden, hidden=True).returncode == 0

    def vdd_hits(d: Path) -> int:
        (svg,) = list(d.glob("*.svg"))
        return svg.read_text(encoding="utf-8", errors="replace").count("VDD")

    # pins 8 and 9 are a stacked VDD pair; only 9 carries `(hide yes)`
    assert vdd_hits(with_hidden) > vdd_hits(plain)


def test_probe_keeps_every_electrical_type_through_a_kicad_rewrite(tmp_path):
    exe = _need_cli()
    out = tmp_path / "probe10.kicad_sym"
    assert _upgrade(exe, PROBE, out).returncode == 0
    before = {p.number: p for p in kicad_lib.read(PROBE).symbols[0].pins}
    after = {p.number: p for p in kicad_lib.read(out).symbols[0].pins}
    assert set(before) == set(after)
    for number, pin in before.items():
        assert after[number].electrical_type is pin.electrical_type
        assert (round(after[number].x_mil), round(after[number].y_mil)) == \
               (round(pin.x_mil), round(pin.y_mil))


def test_probe_covers_the_whole_electrical_type_vocabulary():
    """A grammar fixture that drifts away from the vocabulary proves less."""
    text = PROBE.read_text(encoding="utf-8")
    for token in KICAD_PINTYPE:
        assert f"(pin {token} line" in text, f"probe does not exercise {token!r}"
