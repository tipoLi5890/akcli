"""Golden-file regression corpus (ROADMAP v0.9).

Frozen `nets`/`check`/`diff`/`review analyze` JSON snapshots over the
committed fixture boards. Any behavior drift — a net that splits, a finding
that appears/disappears, a schema field that changes shape — fails here with
a readable JSON diff. Regenerate deliberately with
`python3 tools/golden_regen.py` and review the diff like source code.

Snapshots are normalized (indent-2, sorted keys OFF — key order is part of
the contract) and invoked with repo-relative paths so they are stable across
machines.
"""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from akcli.cli import main

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"


@dataclass(frozen=True)
class Case:
    name: str                 # snapshot file stem
    argv: tuple[str, ...]     # repo-relative argv (deterministic commands only)
    exit_codes: tuple[int, ...] = (0, 1)


_KI = "tests/fixtures/kicad/board_v8.kicad_sch"
_KI7 = "tests/fixtures/kicad/board_v7.kicad_sch"
_ALT = "tests/fixtures/shared_name_label.SchDoc"
_ALT2 = "tests/fixtures/two_gnd_ports.SchDoc"
_TJ = "tests/fixtures/t_junction.SchDoc"
_AFE = "tests/fixtures/corpus/analog_frontend.kicad_sch"
_PWE = "tests/fixtures/corpus/power_entry.kicad_sch"
_GRP = "tests/fixtures/corpus/groups_board.kicad_sch"

CASES: list[Case] = [
    Case("nets-board_v8", ("nets", _KI, "--json")),
    Case("nets-board_v7", ("nets", _KI7, "--json")),
    Case("nets-shared_name_label", ("nets", _ALT, "--json")),
    Case("nets-t_junction", ("nets", _TJ, "--json")),
    Case("check-board_v8", ("check", _KI, "--json", "--fail-on", "never")),
    Case("check-shared_name_label", ("check", _ALT, "--json", "--fail-on", "never")),
    Case("check-two_gnd_ports", ("check", _ALT2, "--json", "--fail-on", "never")),
    Case("diff-v7-v8", ("diff", _KI7, _KI, "--json", "--fail-on", "never")),
    Case("diff-alt-self", ("diff", _ALT, _ALT, "--json", "--fail-on", "never")),
    # a cut wire moves R1.1 off +3V3: pins the "REF.PIN" member ENCODING in
    # added_members/removed_members (every other diff golden has them empty)
    Case("diff-cut-wire",
         ("diff", _KI, "tests/fixtures/kicad/board_v8_cut_wire.kicad_sch",
          "--json", "--fail-on", "never")),
    Case("review-board_v8",
         ("review", "analyze", _KI, "--json", "--profile", "standard")),
    Case("review-shared_name_label",
         ("review", "analyze", _ALT, "--json", "--profile", "standard")),
    Case("render-board_v8", ("render", _KI, "-o", "-")),
    # real-board corpus (authored by akcli itself; see tests/fixtures/corpus/)
    Case("nets-analog_frontend", ("nets", _AFE, "--json")),
    Case("check-analog_frontend", ("check", _AFE, "--json", "--fail-on", "never")),
    Case("review-analog_frontend",
         ("review", "analyze", _AFE, "--json", "--profile", "standard")),
    Case("render-analog_frontend", ("render", _AFE, "-o", "-")),
    Case("nets-power_entry", ("nets", _PWE, "--json")),
    Case("check-power_entry", ("check", _PWE, "--json", "--fail-on", "never")),
    Case("review-power_entry",
         ("review", "analyze", _PWE, "--json", "--profile", "standard")),
    Case("render-power_entry", ("render", _PWE, "-o", "-")),
    # groups_board has no committed op-list, but its netlist snapshot is what
    # tests/test_engine_parity.py's two-engine agreement check consumes — that
    # test skipped itself into permanent silence while this snapshot was absent.
    Case("nets-groups_board", ("nets", _GRP, "--json")),
]


@pytest.mark.parametrize("board", ["analog_frontend", "power_entry"])
def test_corpus_board_reproducible(tmp_path, board):
    """A corpus board re-derives from its committed op-list (provenance)."""
    import shutil

    target = tmp_path / f"{board}.kicad_sch"
    rc = main(["new", str(target)])
    assert rc == 0
    shutil.copy2(ROOT / f"tests/fixtures/corpus/{board}.ops.json",
                 tmp_path / "ops.json")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = main(["draw", str(target), "--ops", str(tmp_path / "ops.json"),
                   "--symbols", str(ROOT / "tests/fixtures/kicad/symbols/Device.kicad_sym"),
                   "--symbols", str(ROOT / "tests/fixtures/kicad/symbols/power.kicad_sym"),
                   "--apply", "--strict-nets"])
    assert rc == 0
    # netlists must be identical (membership-level, not byte-level: uuids differ
    # only if the op-list changed — deterministic UUIDv5 makes even bytes match)
    from akcli.readers import kicad as kreader
    fresh = {(n.name, tuple(n.members))
             for n in kreader.read_sch(str(target)).nets}
    committed = {(n.name, tuple(n.members))
                 for n in kreader.read_sch(
                     str(ROOT / f"tests/fixtures/corpus/{board}.kicad_sch")).nets}
    assert fresh == committed


def snapshot_path(case: Case) -> Path:
    suffix = ".svg" if case.argv[0] == "render" else ".json"
    return GOLDEN / f"{case.name}{suffix}"


def capture(case: Case) -> str:
    """Run the case in-process from the repo root; return canonical output."""
    import os

    old_cwd = os.getcwd()
    buf = io.StringIO()
    err = io.StringIO()
    try:
        os.chdir(ROOT)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = main(list(case.argv))
    finally:
        os.chdir(old_cwd)
    assert rc in case.exit_codes, (
        f"{case.name}: exit {rc} not in {case.exit_codes}\n{err.getvalue()}")
    text = buf.getvalue()
    if case.argv[0] == "render":
        return text
    # re-serialize so snapshot formatting is canonical regardless of emitter
    return json.dumps(json.loads(text), ensure_ascii=False, indent=2) + "\n"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_golden(case: Case):
    snap = snapshot_path(case)
    assert snap.exists(), (
        f"missing snapshot {snap.name} — run `python3 tools/golden_regen.py` "
        "and commit the result")
    got = capture(case)
    want = snap.read_text(encoding="utf-8")
    assert got == want, (
        f"{case.name}: output drifted from tests/golden/{snap.name}.\n"
        "If the change is INTENTIONAL, regenerate with "
        "`python3 tools/golden_regen.py` and review the diff; if not, this "
        "is a regression.")


# --------------------------------------------------------------------------- #
# tools/golden_regen.py blast-radius guard
# --------------------------------------------------------------------------- #
def _load_golden_regen():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "golden_regen", ROOT / "tools" / "golden_regen.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_corpus(gr, tmp_path: Path, n: int) -> list[Case]:
    """Point golden_regen at *n* synthetic cases rooted in *tmp_path*."""
    cases = [Case(f"fake{i}", ("nets", f"fake{i}")) for i in range(n)]
    gr.ROOT = tmp_path
    gr.CASES = cases
    gr.snapshot_path = lambda case: tmp_path / "golden" / f"{case.name}.json"
    gr.capture = lambda case: f"{{}}\n{case.name}\n"
    return cases


def test_golden_regen_refuses_a_wide_bless_without_the_flag(tmp_path: Path, capsys):
    gr = _load_golden_regen()
    _fake_corpus(gr, tmp_path, gr.BLESS_LIMIT + 1)

    assert gr.main([]) == 1
    out = capsys.readouterr().out
    assert "REFUSING to bless" in out
    assert "--bless-all" in out
    # nothing written: the refusal must be all-or-nothing
    assert not (tmp_path / "golden").exists()

    assert gr.main(["--bless-all"]) == 0
    assert len(list((tmp_path / "golden").glob("*.json"))) == gr.BLESS_LIMIT + 1


def test_golden_regen_blesses_a_small_change_without_the_flag(tmp_path: Path):
    gr = _load_golden_regen()
    _fake_corpus(gr, tmp_path, gr.BLESS_LIMIT)
    assert gr.main([]) == 0
    assert len(list((tmp_path / "golden").glob("*.json"))) == gr.BLESS_LIMIT
    # idempotent second run: no changes, still 0
    assert gr.main([]) == 0
