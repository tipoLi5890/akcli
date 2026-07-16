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
]


def test_corpus_board_reproducible(tmp_path):
    """The corpus board re-derives from its committed op-list (provenance)."""
    import shutil

    target = tmp_path / "analog_frontend.kicad_sch"
    rc = main(["new", str(target)])
    assert rc == 0
    shutil.copy2(ROOT / "tests/fixtures/corpus/analog_frontend.ops.json",
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
                     str(ROOT / "tests/fixtures/corpus/analog_frontend.kicad_sch")).nets}
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
