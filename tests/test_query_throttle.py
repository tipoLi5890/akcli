"""Output-throttling contract: `read --summary`, `nets`/`component` --match/--limit.

The context-budget escape hatches: an agent must be able to see the *shape* of
a big board (counts) or a filtered slice (glob + cap) without ever receiving
the full object arrays — and a truncated listing must say so in-band
(total/matched/returned/truncated in JSON; a stderr note in text mode).
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli.cli import main
from akcli.errors import EXIT

FIXTURES = Path(__file__).parent / "fixtures"
SHARED = str(FIXTURES / "shared_name_label.SchDoc")


def test_read_summary_text(capsys):
    assert main(["read", SHARED, "--summary"]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "components: 4" in out
    assert "R12" not in out  # no component rows in summary mode


def test_read_summary_json_counts_only(capsys):
    assert main(["read", SHARED, "--summary", "--json"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema_version"]
    assert doc["counts"] == {"components": 4, "nets": 1, "pins": 4}
    assert "components" not in doc  # never the full arrays
    assert doc["metadata"]["detected_format"] == "altium_sch"


def test_nets_match_and_meta(capsys):
    assert main(["nets", SHARED, "--json", "--match", "S*"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["total"] == 1 and doc["matched"] == 1
    assert doc["truncated"] is False
    assert doc["nets"][0]["name"] == "STAT"


def test_nets_match_none(capsys):
    assert main(["nets", SHARED, "--json", "--match", "ZZZ*"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["matched"] == 0 and doc["nets"] == []
    assert doc["total"] == 1  # the pre-filter population is still reported


def test_component_list_json(capsys):
    assert main(["component", SHARED, "--json"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["total"] == 4 and doc["returned"] == 4
    designators = [c["designator"] for c in doc["components"]]
    assert designators == sorted(designators)


def test_component_list_limit_truncates(capsys):
    assert main(["component", SHARED, "--json", "--limit", "2"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["returned"] == 2 and doc["truncated"] is True
    assert doc["total"] == 4


def test_component_list_text_truncation_note(capsys):
    assert main(["component", SHARED, "--limit", "1"]) == EXIT["OK"]
    captured = capsys.readouterr()
    assert "showing 1 of 4 components" in captured.err
    assert len(captured.out.strip().splitlines()) == 1


def test_component_match_glob(capsys):
    assert main(["component", SHARED, "--json", "--match", "R*"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert {c["designator"] for c in doc["components"]} == {"R7", "R12"}
