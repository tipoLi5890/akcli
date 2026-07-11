"""Bus authoring vs the connectivity gate (stage 1).

``add_bus`` / ``add_bus_entry`` used to be a dead end: the DANGLING_ENDPOINT
hard gate rejected the very files those ops produce, because a wire ripped off
a bus terminates on a ``(bus_entry)`` end — a point the verifier did not know
was an anchor. These tests pin the stage-1 contract:

* bus_entry ends (``at`` and ``at + size``) anchor wire endpoints, so the
  canonical bus + entry + wire rip authored via ops passes the draw gate;
* reciprocally, each bus_entry end must itself land on a ``(bus)`` segment or a
  wire — a floating entry fails with DANGLING_BUS_ENTRY;
* a wire ending on a ``(bus)`` mid-span is still dangling (KiCad joins wires to
  buses only through entries).

Bus *semantics* (netbuild/reader expansion of bus members) are a later stage
and deliberately untested here.
"""

from __future__ import annotations

from pathlib import Path

from altium_kicad_cli.readers.sexpr import parse
from altium_kicad_cli.report import Severity
from altium_kicad_cli.writers import connectivity
from altium_kicad_cli.writers import kicad as kw

V8 = Path(__file__).parent / "fixtures" / "kicad" / "board_v8.kicad_sch"
ROOT_UUID = "8a000000-0000-4000-8000-000000000000"


def _oplist(*ops):
    return {"protocol_version": 1, "target_format": "kicad", "ops": list(ops)}


def _seed(tmp_path: Path) -> Path:
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_bytes(V8.read_bytes())
    return tgt


def _errors(findings):
    return [f for f in findings if f.severity in (Severity.ERROR, Severity.CRITICAL)]


# Canonical rip: vertical bus, entry at (4000,4000) mil with the default
# 100x100 mil size (far end (4100,4100)), a wire to a labelled point.
_RIP_OPS = (
    {"op": "add_bus", "vertices": [[4000, 3000], [4000, 5000]]},
    {"op": "add_bus_entry", "at": [4000, 4000]},
    {"op": "add_wire", "vertices": [[4100, 4100], [4700, 4100]]},
    {"op": "add_net_label", "name": "D0", "at": [4700, 4100]},
)


# --------------------------------------------------------------------------- #
# ops -> draw gate (end to end)
# --------------------------------------------------------------------------- #
def test_bus_rip_ops_pass_gate_dry_run(tmp_path):
    tgt = _seed(tmp_path)
    verify: list = []
    results = kw.apply(_oplist(*_RIP_OPS), str(tgt), apply=False, verify_out=verify)
    assert all(r.status == "ok" for r in results)
    assert _errors(verify) == [], [f"{f.code}: {f.message}" for f in verify]


def test_bus_rip_ops_apply_writes(tmp_path):
    tgt = _seed(tmp_path)
    results = kw.apply(_oplist(*_RIP_OPS), str(tgt), apply=True)
    assert all(r.status == "ok" for r in results)
    text = tgt.read_text(encoding="utf-8")
    assert "(bus_entry" in text and "(bus " in text


def test_floating_bus_entry_fails_gate(tmp_path):
    tgt = _seed(tmp_path)
    before = tgt.read_bytes()
    verify: list = []
    results = kw.apply(
        _oplist({"op": "add_bus_entry", "at": [6000, 6000]}),
        str(tgt), apply=True, verify_out=verify,
    )
    assert all(r.status == "ok" for r in results)  # the op itself succeeds...
    bad = [f for f in verify if f.code == connectivity.DANGLING_BUS_ENTRY]
    assert bad and all(f.severity is Severity.ERROR for f in bad)
    assert len(bad) == 2  # both free ends dangle
    assert tgt.read_bytes() == before  # ...but the gate refused the write


# --------------------------------------------------------------------------- #
# verify unit coverage
# --------------------------------------------------------------------------- #
def test_wire_end_on_bus_entry_end_is_connected():
    # entry at (10,10) on the bus mid-span; far end (12.54,12.54) starts a
    # labelled wire — nothing dangles.
    text = (
        '(kicad_sch (uuid "%s")\n'
        '(bus (pts (xy 10 0) (xy 10 30)) (uuid "b"))\n'
        '(bus_entry (at 10 10) (size 2.54 2.54) (uuid "e"))\n'
        '(wire (pts (xy 12.54 12.54) (xy 25 12.54)) (uuid "w"))\n'
        '(label "D0" (at 25 12.54 0) (uuid "l")))' % ROOT_UUID
    )
    findings = connectivity.verify(parse(text))
    assert _errors(findings) == [], [f.code for f in findings]


def test_wire_end_on_bus_midspan_still_dangles():
    # No entry: the wire's end sits ON the bus but is NOT attached to it.
    text = (
        '(kicad_sch (uuid "%s")\n'
        '(bus (pts (xy 0 0) (xy 40 0)) (uuid "b"))\n'
        '(wire (pts (xy 20 0) (xy 20 20)) (uuid "w"))\n'
        '(label "D0" (at 20 20 0) (uuid "l")))' % ROOT_UUID
    )
    findings = connectivity.verify(parse(text))
    dangling = [f for f in findings if f.code == connectivity.DANGLING_ENDPOINT]
    assert [tuple(f.refs) for f in dangling] == [("(20 0)",)]


def test_floating_bus_entry_both_ends_flagged():
    text = (
        '(kicad_sch (uuid "%s")\n'
        '(bus_entry (at 10 10) (size 2.54 2.54) (uuid "e")))' % ROOT_UUID
    )
    findings = connectivity.verify(parse(text))
    bad = [f for f in findings if f.code == connectivity.DANGLING_BUS_ENTRY]
    assert len(bad) == 2
    refs = {tuple(f.refs) for f in bad}
    assert ("(10 10)",) in refs and ("(12.54 12.54)",) in refs
    assert all(f.severity is Severity.ERROR for f in bad)


def test_bus_entry_with_free_wire_side_flags_only_that_end():
    text = (
        '(kicad_sch (uuid "%s")\n'
        '(bus (pts (xy 10 0) (xy 10 30)) (uuid "b"))\n'
        '(bus_entry (at 10 10) (size 2.54 2.54) (uuid "e")))' % ROOT_UUID
    )
    findings = connectivity.verify(parse(text))
    bad = [f for f in findings if f.code == connectivity.DANGLING_BUS_ENTRY]
    assert [tuple(f.refs) for f in bad] == [("(12.54 12.54)",)]


def test_bus_entry_end_on_bus_endpoint_counts():
    # The entry's bus-side end on the bus's own terminal vertex (not mid-span).
    text = (
        '(kicad_sch (uuid "%s")\n'
        '(bus (pts (xy 10 10) (xy 10 30)) (uuid "b"))\n'
        '(bus_entry (at 10 10) (size 2.54 2.54) (uuid "e"))\n'
        '(wire (pts (xy 12.54 12.54) (xy 25 12.54)) (uuid "w"))\n'
        '(label "D0" (at 25 12.54 0) (uuid "l")))' % ROOT_UUID
    )
    findings = connectivity.verify(parse(text))
    assert connectivity.DANGLING_BUS_ENTRY not in [f.code for f in findings]


def test_bus_entry_missing_size_checked_as_degenerate():
    # No (size): both ends coincide at (at); on a bus it is anchored, floating
    # it is flagged once.
    on_bus = (
        '(kicad_sch (uuid "%s")\n'
        '(bus (pts (xy 10 0) (xy 10 30)) (uuid "b"))\n'
        '(bus_entry (at 10 10) (uuid "e")))' % ROOT_UUID
    )
    findings = connectivity.verify(parse(on_bus))
    assert connectivity.DANGLING_BUS_ENTRY not in [f.code for f in findings]

    floating = (
        '(kicad_sch (uuid "%s")\n'
        '(bus_entry (at 10 10) (uuid "e")))' % ROOT_UUID
    )
    findings = connectivity.verify(parse(floating))
    bad = [f for f in findings if f.code == connectivity.DANGLING_BUS_ENTRY]
    assert len(bad) == 1
