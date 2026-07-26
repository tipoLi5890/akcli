"""Tests for the ``akcli plan`` / ``akcli draw`` CLI (SPEC §3.5 / §3.7).

Drives the real CLI ``main()`` over a copy of the synthetic ``board_v8.kicad_sch``
fixture with a small op-list:

* ``plan`` validates + dry-runs (per-op preview + connectivity) and NEVER writes;
* ``draw`` without ``--apply`` is dry-run (no write);
* ``draw --apply`` writes atomically and the result re-parses;
* exit codes honour ``errors.EXIT`` — ``OPLIST=6`` on an op/verify failure,
  ``USAGE=2`` for a missing ``--ops``, ``0`` on success.
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli.cli import main
from akcli.errors import EXIT
from akcli.readers import sexpr

FIX = Path(__file__).parent / "fixtures" / "kicad"
V8 = FIX / "board_v8.kicad_sch"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _copy(tmp_path: Path) -> Path:
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_bytes(V8.read_bytes())
    return tgt


def _ops_file(tmp_path: Path, ops: list, *, protocol=1, target="kicad", name="ops.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(
        {"protocol_version": protocol, "target_format": target, "ops": ops}
    ))
    return p


_GOOD_OPS = [
    {"op": "place_component", "lib_id": "Device:R", "designator": "R9",
     "x_mil": 7000, "y_mil": 4000, "value": "1k"},
    {"op": "add_wire", "vertices": ["R9.1", "R9.2"]},
]


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def test_plan_exit_zero_and_no_write(tmp_path, capsys):
    tgt = _copy(tmp_path)
    ops = _ops_file(tmp_path, _GOOD_OPS)
    before = tgt.read_bytes()
    rc = main(["plan", str(tgt), "--ops", str(ops)])
    out = capsys.readouterr().out
    assert rc == EXIT["OK"]
    assert tgt.read_bytes() == before          # plan never writes
    assert "place_component" in out            # per-op preview
    assert "connectivity" in out


def test_plan_json(tmp_path, capsys):
    tgt = _copy(tmp_path)
    ops = _ops_file(tmp_path, _GOOD_OPS)
    rc = main(["plan", str(tgt), "--ops", str(ops), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == EXIT["OK"]
    assert payload["applied"] is False
    assert [o["status"] for o in payload["ops"]] == ["ok", "ok"]


# --------------------------------------------------------------------------- #
# draw
# --------------------------------------------------------------------------- #
def test_draw_dry_run_no_write(tmp_path):
    tgt = _copy(tmp_path)
    ops = _ops_file(tmp_path, _GOOD_OPS)
    before = tgt.read_bytes()
    rc = main(["draw", str(tgt), "--ops", str(ops)])
    assert rc == EXIT["OK"]
    assert tgt.read_bytes() == before          # dry-run is the default


def test_draw_apply_writes(tmp_path):
    tgt = _copy(tmp_path)
    ops = _ops_file(tmp_path, _GOOD_OPS)
    before = tgt.read_bytes()
    rc = main(["draw", str(tgt), "--ops", str(ops), "--apply"])
    assert rc == EXIT["OK"]
    assert tgt.read_bytes() != before
    doc = sexpr.parse(tgt.read_text())         # re-parses cleanly
    refs = []
    for s in doc.find_all("symbol"):
        inst = s.find("instances")
        if inst:
            for proj in inst.find_all("project"):
                for path in proj.find_all("path"):
                    r = path.find("reference")
                    if r:
                        refs.append(r.children[1].value)
    assert "R9" in refs


def test_draw_apply_idempotent(tmp_path):
    tgt = _copy(tmp_path)
    ops = _ops_file(tmp_path, _GOOD_OPS)
    main(["draw", str(tgt), "--ops", str(ops), "--apply"])
    once = tgt.read_bytes()
    main(["draw", str(tgt), "--ops", str(ops), "--apply"])
    assert tgt.read_bytes() == once


def test_draw_apply_idempotent_with_power_port(tmp_path):
    """A power port (auto-allocated ``#PWR0<n>`` ref) must re-apply byte-identically.

    Regression: ``alloc_pwr_ref`` is doc-state-dependent, so a naive second apply
    re-numbered the port (#PWR0n -> #PWR0n+1), changing its deterministic uuid and
    appending a duplicate.  The op must reuse the ref it allocated on the first run.
    """
    tgt = _copy(tmp_path)
    ops = _ops_file(tmp_path, [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R9",
         "x_mil": 7000, "y_mil": 4000, "value": "1k"},
        {"op": "place_component", "lib_id": "Device:C", "designator": "C9",
         "x_mil": 7000, "y_mil": 5000, "value": "100n"},
        {"op": "place_power_port", "lib_id": "power:GND", "net_name": "GND",
         "at": [7000, 6000]},
        {"op": "add_wire", "vertices": ["R9.2", "C9.1"]},
    ])
    main(["draw", str(tgt), "--ops", str(ops), "--apply"])
    once = tgt.read_bytes()
    main(["draw", str(tgt), "--ops", str(ops), "--apply"])
    assert tgt.read_bytes() == once
    # re-parses cleanly and the power port was placed exactly once
    doc = sexpr.parse(tgt.read_text())
    refs = [r for r in _all_refs(doc) if r.startswith("#PWR")]
    assert refs.count("#PWR03") == 1


def _all_refs(doc) -> list[str]:
    out: list[str] = []
    for sym in doc.find_all("symbol"):
        if sym.find("lib_id") is None:
            continue
        for prop in sym.find_all("property"):
            kids = prop.children or []
            if len(kids) >= 3 and kids[1].value == "Reference" and kids[2].value:
                out.append(kids[2].value)
    return out


def test_draw_apply_json(tmp_path, capsys):
    tgt = _copy(tmp_path)
    ops = _ops_file(tmp_path, _GOOD_OPS)
    rc = main(["draw", str(tgt), "--ops", str(ops), "--apply", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == EXIT["OK"]
    assert payload["applied"] is True


# --------------------------------------------------------------------------- #
# failure exit codes
# --------------------------------------------------------------------------- #
def test_draw_bad_symbol_exit_oplist(tmp_path):
    tgt = _copy(tmp_path)
    before = tgt.read_bytes()
    ops = _ops_file(tmp_path, [
        {"op": "place_component", "lib_id": "Device:NOPE", "designator": "Z1",
         "x_mil": 1000, "y_mil": 1000},
    ])
    rc = main(["draw", str(tgt), "--ops", str(ops), "--apply"])
    assert rc == EXIT["OPLIST"]
    assert tgt.read_bytes() == before


def test_plan_structurally_invalid_oplist_exit_oplist(tmp_path):
    tgt = _copy(tmp_path)
    # protocol_version 2 fails structural validation -> exit 6, no traceback
    ops = _ops_file(tmp_path, _GOOD_OPS, protocol=2)
    rc = main(["plan", str(tgt), "--ops", str(ops)])
    assert rc == EXIT["OPLIST"]


def test_draw_missing_ops_is_usage_error(tmp_path):
    tgt = _copy(tmp_path)
    rc = main(["draw", str(tgt)])
    assert rc == EXIT["USAGE"]


def test_draw_missing_target_is_usage_error(tmp_path):
    ops = _ops_file(tmp_path, _GOOD_OPS)
    rc = main(["draw", "--ops", str(ops)])
    assert rc == EXIT["USAGE"]


def test_draw_apply_plus_dry_run_never_writes(tmp_path, capsys):
    # a defensively added --dry-run must win over --apply (safety flag first)
    tgt = _copy(tmp_path)
    ops = _ops_file(tmp_path, _GOOD_OPS)
    before = tgt.read_bytes()
    rc = main(["draw", str(tgt), "--ops", str(ops), "--apply", "--dry-run"])
    err = capsys.readouterr().err
    assert rc == EXIT["OK"]
    assert tgt.read_bytes() == before
    assert "honoring --dry-run" in err


def test_unknown_pin_error_lists_valid_pins(tmp_path, capsys):
    tgt = _copy(tmp_path)
    ops = _ops_file(tmp_path, [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R9",
         "x_mil": 7000, "y_mil": 4000},
        {"op": "add_wire", "vertices": ["R9.3", [7000, 4300]]},
    ])
    rc = main(["plan", str(tgt), "--ops", str(ops)])
    out = capsys.readouterr().out
    assert rc == EXIT["OPLIST"]
    assert "valid pins: 1, 2" in out


# --------------------------------------------------------------------------- #
# op-list EDITING must not duplicate untouched parts (designator-seeded uuids)
# --------------------------------------------------------------------------- #
def test_insert_op_then_reapply_does_not_duplicate(tmp_path, capsys):
    tgt = _copy(tmp_path)
    base = [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R21",
         "x_mil": 7000, "y_mil": 4000, "value": "1k"},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R22",
         "x_mil": 7000, "y_mil": 4600, "value": "2k"},
    ]
    ops1 = _ops_file(tmp_path, base, name="ops1.json")
    assert main(["draw", str(tgt), "--ops", str(ops1), "--apply"]) == EXIT["OK"]
    capsys.readouterr()

    # Insert a NEW op at the FRONT — downstream ops all shift index.
    edited = [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R20",
         "x_mil": 7000, "y_mil": 3400, "value": "3k"},
        *base,
    ]
    ops2 = _ops_file(tmp_path, edited, name="ops2.json")
    assert main(["draw", str(tgt), "--ops", str(ops2), "--apply"]) == EXIT["OK"]
    capsys.readouterr()

    text = tgt.read_text(encoding="utf-8")
    for ref in ("R20", "R21", "R22"):
        assert text.count(f'(reference "{ref}"') == 1, ref


def test_duplicate_designator_gate_flags_literal_dup(tmp_path, capsys):
    """A doc carrying the same ref+unit on two symbol nodes must not verify."""
    import re

    from akcli.readers import sexpr as sx
    from akcli.writers import connectivity

    tgt = _copy(tmp_path)
    ops = _ops_file(tmp_path, [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R31",
         "x_mil": 7000, "y_mil": 4000, "value": "1k"},
    ])
    assert main(["draw", str(tgt), "--ops", str(ops), "--apply"]) == EXIT["OK"]
    capsys.readouterr()

    text = tgt.read_text(encoding="utf-8")
    assert not [f for f in connectivity.verify(sx.parse(text))
                if f.code == "DUPLICATE_DESIGNATOR"]

    # Clone R31's placed-symbol block (balanced-paren scan) under fresh uuids.
    start = text.rindex("(symbol (lib_id", 0, text.index('"R31"'))
    depth, i = 0, start
    in_str = False
    while True:
        c = text[i]
        if c == '"' and text[i - 1] != "\\":
            in_str = not in_str
        elif not in_str and c == "(":
            depth += 1
        elif not in_str and c == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    block = text[start:i + 1]
    clone = re.sub(r'"([0-9a-f]{8})(-[0-9a-f-]{27})"',
                   lambda g: '"deadbeef' + g.group(2) + '"', block)
    assert clone != block
    dup = text[:text.rindex(")")] + "\n" + clone + "\n)"
    findings = connectivity.verify(sx.parse(dup))
    assert [f for f in findings if f.code == "DUPLICATE_DESIGNATOR"]


# --------------------------------------------------------------------------- #
# keyless nodes are op-index-free too (audit): editing an op-list must not
# duplicate the wires / labels / annotations downstream of the edit
# --------------------------------------------------------------------------- #
_KEYLESS_OPS = [
    {"op": "place_component", "lib_id": "Device:R", "designator": "R41",
     "x_mil": 7000, "y_mil": 4000, "value": "1k"},
    {"op": "add_text", "text": "reviewed", "at": [7500, 3000]},
    {"op": "add_wire", "vertices": ["R41.1", "R41.2"]},
    {"op": "add_net_label", "name": "SENSE", "at": "R41.1"},
]


def _count_tags(tgt: Path) -> dict[str, int]:
    doc = sexpr.parse(tgt.read_text(encoding="utf-8"))
    out: dict[str, int] = {}
    for c in doc.children or []:
        if c.is_list and (c.children or []):
            tag = c.children[0].value or ""
            out[tag] = out.get(tag, 0) + 1
    return out


def test_keyless_nodes_survive_an_op_list_edit(tmp_path, capsys):
    """Insert an op at the FRONT, re-apply: text/wire/label must not double.

    Regression (0.17 fixed only symbols/pins): wire, junction, no_connect,
    label, bus_entry, annotation and sheet uuids were still seeded with the op
    INDEX, so one inserted op re-identified every node after it and the next
    apply stacked a second copy of each (two `add_text` notes on one point).
    """
    tgt = _copy(tmp_path)
    ops1 = _ops_file(tmp_path, _KEYLESS_OPS, name="ops1.json")
    assert main(["draw", str(tgt), "--ops", str(ops1), "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    before = _count_tags(tgt)

    edited = [{"op": "place_component", "lib_id": "Device:R",
               "designator": "R40", "x_mil": 7000, "y_mil": 3400,
               "value": "3k"}, *_KEYLESS_OPS]
    ops2 = _ops_file(tmp_path, edited, name="ops2.json")
    assert main(["draw", str(tgt), "--ops", str(ops2), "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    after = _count_tags(tgt)

    for tag in ("text", "wire", "label"):
        assert after[tag] == before[tag], (tag, before, after)
    assert tgt.read_text(encoding="utf-8").count('"reviewed"') == 1


def test_keyless_ops_replay_byte_identically(tmp_path, capsys):
    tgt = _copy(tmp_path)
    ops = _ops_file(tmp_path, _KEYLESS_OPS)
    assert main(["draw", str(tgt), "--ops", str(ops), "--apply"]) == EXIT["OK"]
    once = tgt.read_bytes()
    assert main(["draw", str(tgt), "--ops", str(ops), "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    assert tgt.read_bytes() == once


def test_two_identical_keyless_ops_collide_loudly(tmp_path, capsys):
    """Content-seeded uuids mean a literal duplicate op would OVERWRITE the
    first node while both report ok — refuse instead, naming both ops."""
    tgt = _copy(tmp_path)
    before = tgt.read_bytes()
    ops = _ops_file(tmp_path, [
        {"op": "add_text", "text": "A", "at": [7500, 3000]},
        {"op": "add_text", "text": "B", "at": [7500, 3000]},
    ])
    rc = main(["draw", str(tgt), "--ops", str(ops), "--apply"])
    out = capsys.readouterr().out
    assert rc == EXIT["OPLIST"]
    assert "collides with op [0]" in out
    assert tgt.read_bytes() == before


# --------------------------------------------------------------------------- #
# power-port identity is the COORDINATE (audit P0)
# --------------------------------------------------------------------------- #
def _power_values_at(tgt: Path, x_mil: float, y_mil: float) -> list[str]:
    """``Value`` of every ``#``-referenced symbol anchored exactly at x/y mil."""
    want = (round(x_mil * 0.0254, 4), round(y_mil * 0.0254, 4))
    doc = sexpr.parse(tgt.read_text(encoding="utf-8"))
    out = []
    for sym in doc.find_all("symbol"):
        at = sym.find("at")
        if sym.find("lib_id") is None or at is None:
            continue
        pos = (round(float(at.children[1].value), 4),
               round(float(at.children[2].value), 4))
        props = {}
        for prop in sym.find_all("property"):
            kids = prop.children or []
            if len(kids) >= 3:
                props[kids[1].value] = kids[2].value
        if pos == want and props.get("Reference", "").startswith("#"):
            out.append(props.get("Value", ""))
    return sorted(out)


def test_power_port_net_rename_replaces_in_place(tmp_path, capsys):
    """GND -> +3V3 at the same coordinate must REPLACE the port, not stack one.

    0.17 keyed a port's identity on position AND net name, so the renamed op
    matched nothing, allocated a fresh #PWR ref and appended a second symbol on
    top of the first: the stale GND stayed, invisible, shorting GND into +3V3.
    """
    tgt = _copy(tmp_path)
    gnd = _ops_file(tmp_path, [
        {"op": "place_power_port", "lib_id": "power:GND", "net_name": "GND",
         "at": [7000, 6000]},
    ], name="gnd.json")
    assert main(["draw", str(tgt), "--ops", str(gnd), "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    assert _power_values_at(tgt, 7000, 6000) == ["GND"]

    v33 = _ops_file(tmp_path, [
        {"op": "place_power_port", "lib_id": "power:+3V3", "net_name": "+3V3",
         "at": [7000, 6000]},
    ], name="v33.json")
    assert main(["draw", str(tgt), "--ops", str(v33), "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    # exactly one power symbol there, now naming +3V3 — no orphaned GND under it
    assert _power_values_at(tgt, 7000, 6000) == ["+3V3"]


def test_two_power_ports_on_one_point_are_refused(tmp_path, capsys):
    tgt = _copy(tmp_path)
    before = tgt.read_bytes()
    ops = _ops_file(tmp_path, [
        {"op": "place_power_port", "lib_id": "power:GND", "net_name": "GND",
         "at": [7000, 6000]},
        {"op": "place_power_port", "lib_id": "power:+3V3", "net_name": "+3V3",
         "at": [7000, 6000]},
    ])
    rc = main(["draw", str(tgt), "--ops", str(ops), "--apply"])
    cap = capsys.readouterr()
    assert rc == EXIT["OPLIST"]
    # the op-list validator catches this before the writer ever runs; the
    # writer keeps its own guard for library callers that skip validation, and
    # both report the same code
    assert "POWER_PORT_STACKED" in cap.out + cap.err
    assert tgt.read_bytes() == before


def test_two_identical_power_ports_are_refused(tmp_path, capsys):
    """Same coordinate AND same net: 0.17 wrote one over the other, both ok."""
    tgt = _copy(tmp_path)
    ops = _ops_file(tmp_path, [
        {"op": "place_gnd", "at": [7000, 6000]},
        {"op": "place_gnd", "at": [7000, 6000]},
    ])
    rc = main(["draw", str(tgt), "--ops", str(ops), "--apply"])
    cap = capsys.readouterr()
    assert rc == EXIT["OPLIST"]
    assert "POWER_PORT_STACKED" in cap.out + cap.err
