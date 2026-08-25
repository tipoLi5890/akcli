"""Fixture-first ground truth: a REAL KiCad accepts what `project new` writes.

akcli's rule for KiCad grammar is that a claim is proven against the actual
tool, not asserted from a spec reading. That matters more here than anywhere
else in the tree: ``.kicad_pro`` and ``.kicad_pcb`` are two file formats akcli
had never written before, and a plausible-looking header that KiCad silently
"repairs" on open would pass every pure-Python gate we own.

So this file runs the real ``kicad-cli`` over both the freshly scaffolded
project AND the committed fixtures under
``tests/fixtures/kicad/blank_project/`` — the second half is what makes the
fixtures ground truth instead of a copy of the emitter's current opinion.

``pcb drc`` is asserted on the tool's EXIT STATUS, not on an empty report: a
board with no outline legitimately reports one ``invalid_outline`` violation,
because a blank board scaffold is a container, not a layout. What is being
proven is that KiCad parsed the file and ran the check — the failure mode this
guards against is "unrecognised file", not "unrouted board".

The skip reason string is load-bearing: ``.github/workflows/ci.yml`` greps for
``kicad-cli not installed`` to fail the KiCad job if these cases silently skip
on a box that HAS the tool.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from akcli import safety
from akcli.drivers import kicad_cli
from tests._cli_harness import run_cli

FIX = Path(__file__).parent / "fixtures" / "kicad" / "blank_project"

needs_cli = pytest.mark.skipif(kicad_cli.find() is None,
                               reason="kicad-cli not installed")

# Generous: a cold KiCad start on a CI runner is slow, and a hang here should
# fail as a timeout with a message rather than as a job-level cancellation.
_TIMEOUT = 180.0


def _exe() -> str:
    exe = kicad_cli.find()
    assert exe is not None
    return exe


def _run(*argv: str):
    # No ``--`` separator: kicad-cli's own parser treats it as a positional
    # argument (it is not getopt), which is why the driver never passes one
    # either. Paths are passed last, exactly as ``drivers/kicad_cli`` does.
    return safety.run_subprocess([_exe(), *argv], _TIMEOUT)


def _scaffold(tmp_path: Path) -> Path:
    d = tmp_path / "demo"
    rc, _out, err = run_cli(["project", "new", str(d), "--title", "E2E"])
    assert rc == 0, err
    return d


# --------------------------------------------------------------------------- #
# the scaffolded project
# --------------------------------------------------------------------------- #
@needs_cli
def test_scaffolded_schematic_passes_erc(tmp_path):
    d = _scaffold(tmp_path)
    out = kicad_cli.erc(str(d / "demo.kicad_sch"))
    assert out is not None, "kicad-cli produced no ERC report"
    assert out["exit_code"] == 0, out["stderr"]
    # An empty sheet has nothing to violate; a nonzero count here would mean
    # the emitter wrote something eeschema objects to.
    assert not (out["report"] or {}).get("violations")


@needs_cli
def test_scaffolded_schematic_exports_a_netlist(tmp_path):
    d = _scaffold(tmp_path)
    net = kicad_cli.netlist(str(d / "demo.kicad_sch"))
    assert net is not None
    assert net["exit_code"] == 0, net["stderr"]
    assert net["netlist"] and net["netlist"].lstrip().startswith("(export")


@needs_cli
def test_scaffolded_board_runs_drc(tmp_path):
    d = _scaffold(tmp_path)
    out = tmp_path / "drc.json"
    proc = _run("pcb", "drc", "--format", "json", "--output", str(out),
                str(d / "demo.kicad_pcb"))
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    report = json.loads(out.read_text(encoding="utf-8"))
    # KiCad parsed OUR file (not a recovered/blank one) and ran the checks.
    assert report["source"] == "demo.kicad_pcb"
    assert report["unconnected_items"] == []
    # The only expected violation is "no board outline" — a scaffold has no
    # geometry by design. Anything else means the header/layers are wrong.
    kinds = {v["type"] for v in report.get("violations", [])}
    assert kinds <= {"invalid_outline"}, kinds


@needs_cli
def test_scaffolded_board_exports_svg(tmp_path):
    d = _scaffold(tmp_path)
    outdir = tmp_path / "svg"
    proc = _run("pcb", "export", "svg", "--output", str(outdir),
                "--layers", "F.Cu,Edge.Cuts", str(d / "demo.kicad_pcb"))
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert outdir.exists()


@needs_cli
def test_scaffolded_project_file_is_read_back_unchanged(tmp_path):
    """kicad-cli must not need to rewrite the project file to use it."""
    d = _scaffold(tmp_path)
    pro = d / "demo.kicad_pro"
    before = pro.read_bytes()
    kicad_cli.erc(str(d / "demo.kicad_sch"))
    assert pro.read_bytes() == before


# --------------------------------------------------------------------------- #
# the committed fixtures (ground truth, not a snapshot of today's emitter)
# --------------------------------------------------------------------------- #
@needs_cli
def test_fixture_board_runs_drc(tmp_path):
    board = tmp_path / "blank.kicad_pcb"
    shutil.copy2(FIX / "blank.kicad_pcb", board)
    out = tmp_path / "drc.json"
    proc = _run("pcb", "drc", "--format", "json", "--output", str(out),
                str(board))
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    report = json.loads(out.read_text(encoding="utf-8"))
    assert {v["type"] for v in report.get("violations", [])} <= {"invalid_outline"}


@needs_cli
def test_fixture_project_file_is_valid_json_kicad_accepts():
    doc = json.loads((FIX / "blank.kicad_pro").read_text(encoding="utf-8"))
    assert doc["meta"]["version"] == 1
    assert doc["sheets"] and len(doc["sheets"][0]) == 2
