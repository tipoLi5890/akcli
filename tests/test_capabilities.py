"""`akcli capabilities` — the self-describing surface manifest.

The manifest is introspected from the live parser, so these tests assert it
stays in lockstep with every other single source of truth: the subparser
registry, the frozen EXIT/ERROR_CODES tables, the op vocabulary, and the calc
registry. A command added without appearing here is a discoverability bug.
"""

from __future__ import annotations

import argparse
import json

from akcli import cli, ops
from akcli.calc import CALCS
from akcli.commands.capabilities import build_manifest
from akcli.errors import ERROR_CODES, EXIT


def _parser_choices() -> dict:
    parser = cli.build_parser()
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("no subparsers found")


def test_manifest_covers_every_subcommand():
    manifest = build_manifest()
    listed = {c["name"] for c in manifest["commands"]}
    assert listed == set(_parser_choices())


def test_manifest_versions_and_tables():
    manifest = build_manifest()
    assert manifest["schema_version"]
    assert manifest["protocol_version"] == ops.PROTOCOL_VERSION
    assert {e["name"]: e["code"] for e in manifest["exit_codes"]} == EXIT
    assert {e["code"] for e in manifest["error_codes"]} == set(ERROR_CODES)


def test_manifest_ops_and_calcs_match_registries():
    manifest = build_manifest()
    m_ops = manifest["ops"]
    assert set(m_ops["core"]) | set(m_ops["sugar"]) == set(ops.OP_NAMES)
    assert set(m_ops["macros"]) == set(ops.MACRO_OPS)
    assert manifest["calculators"]["count"] == len(CALCS)
    assert set(manifest["calculators"]["names"]) == set(CALCS)
    # every op in the manifest carries its per-executor support entry
    assert set(m_ops["capabilities"]) >= set(m_ops["core"]) - set(m_ops["sugar"])


def test_manifest_nested_subcommands_present():
    manifest = build_manifest()
    by_name = {c["name"]: c for c in manifest["commands"]}
    review_subs = {s["name"] for s in by_name["review"]["subcommands"]}
    assert {"analyze", "facts", "report", "explain",
            "propose", "diff", "tree", "validate"} <= review_subs
    ops_subs = {s["name"] for s in by_name["ops"]["subcommands"]}
    assert {"list", "template"} <= ops_subs


def test_manifest_global_flags_reported_once():
    manifest = build_manifest()
    global_opts = {f for entry in manifest["global_flags"] for f in entry["flags"]}
    assert {"--json", "--config", "--debug", "--version"} <= global_opts
    for cmd in manifest["commands"]:
        for flag in cmd.get("flags", []):
            assert not (set(flag["flags"]) & global_opts), (
                f"{cmd['name']} repeats a global flag: {flag['flags']}")


def test_cli_json_and_text(capsys):
    assert cli.main(["capabilities", "--json"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema_version"] and doc["commands"]

    assert cli.main(["capabilities"]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "commands (" in out and "exit codes:" in out


def test_flag_shapes_are_json_safe():
    manifest = build_manifest()
    json.dumps(manifest)  # raises on anything non-serializable
    draw = next(c for c in manifest["commands"] if c["name"] == "draw")
    apply_flag = next(f for f in draw["flags"] if "--apply" in f["flags"])
    assert apply_flag["takes_value"] is False
