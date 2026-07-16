"""Every JSON object payload must carry a version stamp (agent contract).

``akcli capabilities`` promises: "every JSON object payload carries
schema_version or a family version field". Two gates keep it true:

* **Behavioral** — offline commands whose payloads are hand-built (not routed
  through ``report.render`` or ``model.to_json``) are executed and their
  stamps asserted.
* **Mechanical** — an AST scan over every command module: any
  ``_dumps({...literal dict...})`` emit must contain a ``*_version`` key or be
  wrapped in ``_stamp(...)``, so a NEW payload cannot ship unstamped and
  silently falsify the manifest (the drift class this contract fixed).
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests._cli_harness import blank_sheet, json_of

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

_VERSION_FIELDS = ("schema_version", "protocol_version", "journal_version")


def _assert_stamped(doc: dict, argv: list[str]) -> None:
    assert any(k in doc for k in _VERSION_FIELDS), (
        f"{' '.join(argv)}: payload has no version stamp "
        f"(want one of {_VERSION_FIELDS}); keys: {sorted(doc)[:8]}"
    )


def test_new_and_undo_list_are_stamped(tmp_path):
    target = tmp_path / "board.kicad_sch"
    argv = ["new", str(target), "--json"]
    _assert_stamped(json_of(argv), argv)
    argv = ["undo", str(target), "--list", "--json"]
    _assert_stamped(json_of(argv), argv)


def test_doctor_is_stamped():
    argv = ["doctor", "--json"]
    _assert_stamped(json_of(argv), argv)


def test_log_is_stamped(tmp_path):
    argv = ["log", str(tmp_path), "--json"]
    _assert_stamped(json_of(argv), argv)


def test_render_summary_is_stamped(tmp_path):
    sch = FIXTURES / "kicad" / "board_v7.kicad_sch"
    out = tmp_path / "out.svg"
    argv = ["render", str(sch), "-o", str(out), "--json"]
    _assert_stamped(json_of(argv), argv)


def test_calc_result_is_stamped():
    argv = ["calc", "vdivider", "vin=5", "r_top=6.8k", "r_bottom=3.3k",
            "--json"]
    _assert_stamped(json_of(argv), argv)


def test_verify_both_modes_are_stamped(tmp_path):
    sch = FIXTURES / "kicad" / "board_v7.kicad_sch"
    argv = ["verify", str(sch), str(sch), "--json", "--exit-zero"]
    _assert_stamped(json_of(argv), argv)
    pcb = FIXTURES / "kicad" / "board.kicad_pcb"
    if pcb.is_file():
        argv = ["verify", str(sch), str(pcb), "--json", "--exit-zero"]
        _assert_stamped(json_of(argv), argv)


def test_relink_dry_run_is_stamped(tmp_path):
    target = blank_sheet(tmp_path)
    argv = ["relink-symbols", str(target), "--json"]
    _assert_stamped(json_of(argv), argv)


def test_library_check_lock_is_stamped(tmp_path):
    blank_sheet(tmp_path)
    argv = ["library", "check-lock", str(tmp_path), "--json"]
    _assert_stamped(json_of(argv), argv)


def test_arrange_dry_run_is_stamped(tmp_path):
    target = blank_sheet(tmp_path)
    argv = ["arrange", str(target), "--json"]
    _assert_stamped(json_of(argv), argv)


def test_nets_pins_and_query_miss_are_stamped(tmp_path):
    sch = FIXTURES / "kicad" / "board_v7.kicad_sch"
    argv = ["nets", str(sch), "--json"]
    _assert_stamped(json_of(argv), argv)
    argv = ["net", str(sch), "NO_SUCH_NET", "--json"]
    _assert_stamped(json_of(argv, expect=8), argv)


def test_review_explain_and_tree_are_stamped():
    argv = ["review", "explain", "REVIEW_RC_CUTOFF", "--json"]
    _assert_stamped(json_of(argv), argv)
    sch = FIXTURES / "corpus" / "analog_frontend.kicad_sch"
    argv = ["review", "tree", str(sch), "--json"]
    _assert_stamped(json_of(argv), argv)


def test_sim_deck_only_is_stamped():
    sch = FIXTURES / "corpus" / "analog_frontend.kicad_sch"
    argv = ["sim", str(sch), "--deck-only", "--json"]
    _assert_stamped(json_of(argv), argv)


def test_fab_check_is_stamped():
    pcb = FIXTURES / "kicad" / "board.kicad_pcb"
    profile = ROOT / "examples" / "fab" / "jlc-4l-1oz.toml"
    if not (pcb.is_file() and profile.is_file()):
        import pytest
        pytest.skip("fab fixtures unavailable")
    argv = ["fab", "check", str(pcb), "--profile", str(profile),
            "--json", "--fail-on", "never"]
    _assert_stamped(json_of(argv), argv)


def test_error_envelope_is_stamped():
    argv = ["read", "/nonexistent/x.kicad_sch", "--json"]
    _assert_stamped(json_of(argv, expect=4), argv)


def test_read_throttle_components(tmp_path):
    """`read --match/--limit`: graduated context-budget throttling."""
    sch = FIXTURES / "kicad" / "board_v7.kicad_sch"
    doc = json_of(["read", str(sch), "--json", "--limit", "1"])
    assert len(doc["components"]) == 1
    listing = doc["listing"]
    assert listing["filtered"] == "components"
    assert listing["truncated"] is True
    assert listing["returned"] == 1
    assert listing["total"] > 1
    # nets stay complete — only the filtered array is cut
    assert doc["nets"]

    full = json_of(["read", str(sch), "--json"])
    assert "listing" not in full   # no flags -> byte-identical full export


def test_read_throttle_match_filters_text_mode_too(tmp_path):
    from tests._cli_harness import run_cli

    sch = FIXTURES / "kicad" / "board_v7.kicad_sch"
    doc = json_of(["read", str(sch), "--json", "--match", "R*"])
    assert doc["components"]
    assert all(c["designator"].startswith("R") for c in doc["components"])
    assert doc["listing"]["matched"] == len(doc["components"])
    # text mode honors the same filter (was silently ignored before): the
    # components section empties (net membership strings still name pins)
    rc, out, err = run_cli(["read", str(sch), "--match", "ZZZ_NOTHING"])
    assert rc == 0
    assert "components: 0" in out
    assert "note: showing 0 of" in err


def test_read_throttle_pcb_footprints(tmp_path):
    pcb = FIXTURES / "kicad" / "board.kicad_pcb"
    doc = json_of(["read", str(pcb), "--json", "--limit", "1"])
    assert len(doc["footprints"]) <= 1
    assert doc["listing"]["filtered"] == "footprints"


def test_capabilities_constraints_and_altium_honesty():
    """The manifest publishes hard limits + the unwired-Altium flag —
    derived from ops.py / ops.capabilities.json, not hardcoded copies."""
    argv = ["capabilities", "--json"]
    doc = json_of(argv)
    ops = doc["ops"]
    assert ops["altium_live_wired"] is False
    cons = ops["constraints"]
    assert cons["rotation_enum"] == [0, 90, 180, 270]
    assert cons["wire_orthogonal_only"] is True
    assert cons["grid_mil"] == 50
    assert cons["hierarchy"] == "flat_v1_only"


def test_dumps_object_literals_carry_version_stamp():
    """Mechanical anti-drift guard: a NEW `_dumps({...})` object payload in
    any command module must carry a `*_version` key or route through
    `_stamp(...)`. (Non-literal args — variables, comprehensions — can't be
    checked statically; the behavioral tests above cover the known ones.)"""
    offenders: list[str] = []
    for path in sorted((ROOT / "src" / "akcli" / "commands").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_dumps" and node.args):
                continue
            arg = node.args[0]
            if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                    and arg.func.id == "_stamp"):
                continue
            if not isinstance(arg, ast.Dict):
                continue
            keys = {k.value for k in arg.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if not any(k == "schema_version" or k.endswith("_version")
                       for k in keys):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        "unstamped _dumps({...}) object payload(s) — wrap in _stamp() or "
        f"add a version field: {offenders}"
    )
