"""Auto-generated subcircuit testbenches (review/testbench.py + CLI).

Offline half: generation re-derives topology from the live schematic,
recomputes predictions (never trusts the finding text), and skips loudly
when it cannot. Engine half (skipped without libngspice): ngspice confirms
the RC corner and the divider ratio on the corpus board, and a wrong bound
actually FAILS — the verdict machinery is validated, not assumed.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from akcli import report
from akcli.cli import main
from akcli.errors import EXIT
from akcli.readers import kicad
from akcli.review import engine as review_engine
from akcli.review import testbench
from akcli.sim import engine as sim_engine

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "tests" / "fixtures" / "corpus" / "analog_frontend.kicad_sch"

_HAVE_NGSPICE = sim_engine.available() is not None
_needs_engine = pytest.mark.skipif(
    not _HAVE_NGSPICE, reason="libngspice not installed on this machine")


def _board_findings(sch) -> list[dict]:
    findings, _meta = review_engine.analyze(sch, profile="standard")
    return [report._finding_json(f) for f in findings]


def _divider_finding(refs: list[str]) -> dict:
    return {"code": "REVIEW_FB_DIVIDER", "severity": "info",
            "message": "synthetic", "refs": refs,
            "fingerprint": "cafe" * 8}


# --------------------------------------------------------------------------- #
# offline: generation
# --------------------------------------------------------------------------- #
def test_rc_bench_generated_from_corpus_finding():
    sch = kicad.read_sch(str(BOARD))
    benches, skipped = testbench.generate(sch, _board_findings(sch))
    assert skipped == []
    assert [b.kind for b in benches] == ["rc_lowpass"]
    b = benches[0]
    assert b.refs == ["R5", "C5"] and b.gnd == "GND"
    # prediction recomputed from the schematic: 1/(2π·1k·10n)
    assert abs(b.expect["fcut"]["value"] - 15915.49) < 1.0
    # the cone contains ONLY the finding's components
    assert {c.designator for c in b.schematic.components} == {"R5", "C5"}
    assert {n.name for n in b.schematic.nets} == {"SENSE", "ADC_IN", "GND"}


def test_divider_bench_generated_from_refs():
    sch = kicad.read_sch(str(BOARD))
    benches, skipped = testbench.generate(sch, [_divider_finding(["R1", "R2"])])
    assert skipped == []
    (b,) = benches
    assert b.kind == "divider_dc" and set(b.refs) == {"R1", "R2"}
    # 3V3 implies 3.3 V; 10k/10k -> 1.65 V, recomputed from live values
    assert abs(b.expect["vmid"]["value"] - 1.65) < 1e-9
    assert b.gnd == "GND"


def test_ungeneratable_topology_is_skipped_loudly():
    sch = kicad.read_sch(str(BOARD))
    stale = {"code": "REVIEW_RC_CUTOFF", "refs": ["R99", "C99"],
             "anchors": [{"kind": "net", "id": "NOPE"}],
             "fingerprint": "dead" * 8}
    benches, skipped = testbench.generate(sch, [stale])
    assert benches == []
    assert len(skipped) == 1
    assert "do not resolve" in skipped[0]["reason"]


def test_non_simulable_codes_silently_out_of_scope():
    sch = kicad.read_sch(str(BOARD))
    benches, skipped = testbench.generate(
        sch, [{"code": "ERC_DANGLING_NET", "refs": ["R3.2"]}])
    assert benches == [] and skipped == []


def test_cli_deck_only_writes_decks(tmp_path, capsys):
    rc = main(["review", "testbench", str(BOARD), "--deck-only",
               "--out", str(tmp_path), "--json"])
    assert rc == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["mode"] == "deck-only" and len(doc["benches"]) == 1
    deck_path = Path(doc["benches"][0]["deck_path"])
    assert deck_path.exists()
    text = deck_path.read_text(encoding="utf-8")
    assert "AC 1" in text                  # the synthesized AC stimulus
    assert ".ac dec 20" in text            # sweep centered on the corner
    assert text.rstrip().endswith(".end")  # a complete, runnable deck


# --------------------------------------------------------------------------- #
# engine: ngspice delivers the verdicts
# --------------------------------------------------------------------------- #
@_needs_engine
def test_rc_bench_passes_on_engine():
    sch = kicad.read_sch(str(BOARD))
    benches, _ = testbench.generate(sch, _board_findings(sch))
    verdict = testbench.run_bench(benches[0])
    assert verdict["ok"], verdict
    measured = verdict["measured"]["fcut"]
    assert abs(measured - 15915.49) / 15915.49 < 0.05


@_needs_engine
def test_divider_bench_passes_on_engine():
    sch = kicad.read_sch(str(BOARD))
    benches, _ = testbench.generate(sch, [_divider_finding(["R1", "R2"])])
    verdict = testbench.run_bench(benches[0])
    assert verdict["ok"], verdict
    assert abs(verdict["measured"]["vmid"] - 1.65) < 0.02


@_needs_engine
def test_wrong_bound_actually_fails():
    sch = kicad.read_sch(str(BOARD))
    benches, _ = testbench.generate(sch, _board_findings(sch))
    b = benches[0]
    b.spec.asserts[0]["approx"] = b.spec.asserts[0]["approx"] * 3  # sabotage
    verdict = testbench.run_bench(b)
    assert verdict["ok"] is False
    assert any(f["code"] == "SIM_ASSERT_FAIL" for f in verdict["findings"])


@_needs_engine
def test_cli_run_mode_end_to_end(capsys):
    rc = main(["review", "testbench", str(BOARD), "--json"])
    assert rc == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True
    assert doc["summary"] == {"total": 1, "passed": 1, "failed": 0}
    assert doc["testbench_version"] == testbench.TESTBENCH_VERSION


@_needs_engine
def test_cli_reuses_saved_findings(tmp_path, capsys):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["review", "analyze", str(BOARD), "--json",
                     "--out", str(tmp_path / "f.json")]) == EXIT["OK"]
    capsys.readouterr()
    rc = main(["review", "testbench", str(BOARD),
               "--findings", str(tmp_path / "f.json"), "--json"])
    assert rc == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["summary"]["passed"] == 1
