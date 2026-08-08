"""`akcli schema` + `capabilities --section/--command` (the 0.20 contract
accessibility pair): packaged schemas become CLI-reachable byte-for-byte, and
the ~96 KB manifest becomes sliceable so an agent pays for ONE section, not
the whole document.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli.cli import main
from akcli.commands.capabilities import _SECTIONS, build_manifest
from akcli.errors import EXIT

SCHEMAS = sorted(p.name for p in
                 (Path(__file__).parent.parent / "schemas").glob("*.json"))


# --------------------------------------------------------------------------- #
# akcli schema
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("filename", SCHEMAS)
def test_schema_output_is_the_packaged_file_verbatim(filename, capsys):
    assert main(["schema", filename]) == EXIT["OK"]
    out = capsys.readouterr().out
    canonical = (Path(__file__).parent.parent / "schemas" / filename
                 ).read_text(encoding="utf-8")
    assert out == canonical.rstrip("\n") + "\n"


def test_schema_short_name_resolves(capsys):
    assert main(["schema", "ops"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["$id"].endswith("ops.schema.json")


def test_schema_capabilities_short_name(capsys):
    assert main(["schema", "ops.capabilities"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert "ops" in doc


def test_schema_unknown_name_is_usage(capsys):
    # argparse-native choices fail at parse time (SystemExit 2) with the list
    with pytest.raises(SystemExit) as exc:
        main(["schema", "no-such-schema"])
    assert exc.value.code == EXIT["USAGE"]
    err = capsys.readouterr().err
    assert "invalid choice: 'no-such-schema'" in err
    assert "findings" in err                # the list rides along


def test_schema_unknown_name_json_envelope(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["schema", "no-such-schema", "--json"])
    assert exc.value.code == EXIT["USAGE"]
    env = json.loads(capsys.readouterr().out)
    assert env["error"]["code"] == "USAGE"


# --------------------------------------------------------------------------- #
# capabilities --section / --command
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("section", _SECTIONS)
def test_capabilities_section_slices_match_the_manifest(section, capsys):
    assert main(["capabilities", "--section", section]) == EXIT["OK"]
    payload = json.loads(capsys.readouterr().out)
    manifest = build_manifest()
    assert payload["section"] == section
    assert payload[section] == manifest[section]
    assert payload["schema_version"] == manifest["schema_version"]
    assert payload["protocol_version"] == manifest["protocol_version"]


def test_capabilities_command_filter_returns_one_entry(capsys):
    assert main(["capabilities", "--command", "draw"]) == EXIT["OK"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["found"] is True
    (entry,) = payload["commands"]
    assert entry["name"] == "draw"
    flags = {f["flags"][0] for f in entry["flags"] if f.get("flags")}
    assert "--strict-nets" in flags


def test_capabilities_command_miss_exits_8(capsys):
    assert main(["capabilities", "--command", "no-such-verb"]) == EXIT["QUERY_MISS"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["found"] is False
    assert payload["command"] == "no-such-verb"


def test_capabilities_unknown_section_is_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["capabilities", "--section", "nope"])
    assert exc.value.code == EXIT["USAGE"]


def test_full_manifest_unchanged_by_slicing_flags(capsys):
    """The no-flag document keeps its exact historical shape (no new keys)."""
    assert main(["capabilities", "--json"]) == EXIT["OK"]
    payload = json.loads(capsys.readouterr().out)
    assert "section" not in payload
    assert set(_SECTIONS) <= set(payload)
