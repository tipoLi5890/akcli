"""examples/circuits/ — copy-paste-ready op-lists that CANNOT drift.

Each example directory holds ``ops.json`` + ``expected_nets.json`` + a README.
CI applies the exact committed op-list to a blank sheet and asserts (a) the
resulting net membership equals the pinned expectation (semantic lock — a
doc-motivated re-ordering of ops never falsely fails) and (b) apply-twice
byte-equality (the idempotency invariant every op-list must honor).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli.cli import main
from akcli.errors import EXIT

REPO = Path(__file__).parent.parent
EXAMPLES = sorted((REPO / "examples" / "circuits").iterdir())
SYMBOLS = [str(REPO / "tests/fixtures/kicad/symbols/Device.kicad_sym"),
           str(REPO / "tests/fixtures/kicad/symbols/power.kicad_sym")]


def _sym_flags() -> list[str]:
    return [a for s in SYMBOLS for a in ("--symbols", s)]


@pytest.mark.parametrize("exdir", EXAMPLES, ids=lambda p: p.name)
def test_example_layout(exdir):
    assert (exdir / "ops.json").is_file()
    assert (exdir / "expected_nets.json").is_file()
    assert (exdir / "README.md").is_file()


@pytest.mark.parametrize("exdir", EXAMPLES, ids=lambda p: p.name)
def test_example_applies_and_matches_pinned_nets(exdir, tmp_path, capsys):
    board = tmp_path / "board.kicad_sch"
    assert main(["new", str(board)]) == EXIT["OK"]
    capsys.readouterr()
    assert main(["draw", str(board), "--ops", str(exdir / "ops.json"),
                 "--apply", "--strict-nets", "--no-erc",
                 *_sym_flags()]) == EXIT["OK"]
    capsys.readouterr()

    assert main(["nets", str(board), "--json"]) == EXIT["OK"]
    got = json.loads(capsys.readouterr().out)
    got_nets = {n["name"]: frozenset(n["members"]) for n in got["nets"]
                if not n["name"].startswith("<")}
    expected = json.loads((exdir / "expected_nets.json").read_text(
        encoding="utf-8"))
    exp_nets = {n["name"]: frozenset(n["members"]) for n in expected["nets"]}
    assert got_nets == exp_nets

    # apply-twice byte-equality: the invariant every op-list must honor
    first = board.read_bytes()
    assert main(["draw", str(board), "--ops", str(exdir / "ops.json"),
                 "--apply", "--no-erc", *_sym_flags()]) == EXIT["OK"]
    capsys.readouterr()
    assert board.read_bytes() == first


@pytest.mark.parametrize("exdir", EXAMPLES, ids=lambda p: p.name)
def test_example_readme_shows_the_apply_command(exdir):
    text = (exdir / "README.md").read_text(encoding="utf-8")
    assert "akcli draw" in text and "--apply" in text
