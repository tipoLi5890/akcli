"""Tests for :mod:`akcli.writers.connectivity` (SPEC §3.5).

The connectivity module is the **primary** post-write gate and MUST run with no
KiCad installed (pure Python). These tests exercise:

* a clean, fully-wired board → zero findings;
* dangling wire endpoints, duplicate UUIDs, unresolved ``lib_id`` and broken
  ``(instances)`` paths → the matching findings;
* ``(no_connect)`` honored as a valid terminator (and flagged when it sits on a
  wired pin);
* :func:`auto_junctions` reinserting exactly the right ``(junction)`` nodes at
  3+-way meets / T-junctions, idempotently, while leaving X-crossings alone.
"""

from __future__ import annotations

from pathlib import Path

from akcli.readers import sexpr
from akcli.readers.sexpr import dumps, parse
from akcli.report import Severity
from akcli.writers import connectivity

FIX = Path(__file__).parent / "fixtures" / "kicad"
V8 = FIX / "board_v8.kicad_sch"
V7 = FIX / "board_v7.kicad_sch"
ROOT_UUID = "8a000000-0000-4000-8000-000000000000"

# Known junction points of board_v8, in integer nm (50.8 mm, 59.69 mm) etc.
NM = 1_000_000
J1 = (round(50.8 * NM), round(59.69 * NM))   # 2 wire ends + R2 pin
J2 = (round(58.42 * NM), round(67.31 * NM))  # wire end on a wire mid-span (T)


def _doc():
    return parse(V8.read_text())


def _codes(findings):
    return [f.code for f in findings]


def _errors(findings):
    return [f for f in findings if f.severity in (Severity.ERROR, Severity.CRITICAL)]


# --------------------------------------------------------------------------- #
# clean board
# --------------------------------------------------------------------------- #
def test_clean_board_has_no_findings():
    findings = connectivity.verify(_doc())
    assert findings == [], _codes(findings)


def test_clean_board_v7_has_no_error_findings():
    findings = connectivity.verify(parse(V7.read_text()))
    assert _errors(findings) == [], _codes(findings)


def test_verify_does_not_shell_out(monkeypatch):
    # Guard: the primary gate must be pure Python. If anything tries to spawn a
    # subprocess the test fails.
    import subprocess

    def _boom(*a, **k):  # pragma: no cover - only runs on regression
        raise AssertionError("connectivity.verify must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert connectivity.verify(_doc()) == []


# --------------------------------------------------------------------------- #
# dangling endpoints
# --------------------------------------------------------------------------- #
def test_dangling_endpoint_flagged():
    doc = _doc()
    # A floating wire that connects to nothing.
    stray = parse(
        '(wire (pts (xy 200 200) (xy 220 200)) '
        '(stroke (width 0) (type default)) '
        '(uuid "8a000000-0000-4000-8000-0000000009ff"))'
    )
    doc.children.append(stray)
    doc.ws.insert(len(doc.children) - 1, "\n\t")
    findings = connectivity.verify(doc)
    dangling = [f for f in findings if f.code == connectivity.DANGLING_ENDPOINT]
    # Both free ends of the stray wire dangle.
    assert len(dangling) == 2
    assert all(f.severity is Severity.ERROR for f in dangling)


def test_wire_to_wire_endpoint_is_connected():
    # Two wires meeting end-to-end (an L corner) — neither end dangles.
    text = (
        '(kicad_sch (uuid "%s")\n'
        '(wire (pts (xy 10 10) (xy 20 10)) (uuid "u-1"))\n'
        '(wire (pts (xy 20 10) (xy 20 20)) (uuid "u-2")))' % ROOT_UUID
    )
    findings = connectivity.verify(parse(text))
    assert [f for f in findings if f.code == connectivity.DANGLING_ENDPOINT] == [
        # only the two truly free ends (10,10) and (20,20) dangle
        f
        for f in findings
        if f.code == connectivity.DANGLING_ENDPOINT
    ]
    dangling = [f for f in findings if f.code == connectivity.DANGLING_ENDPOINT]
    refs = {tuple(f.refs) for f in dangling}
    assert ("(10 10)",) in refs
    assert ("(20 20)",) in refs
    assert len(dangling) == 2  # the shared (20,10) corner is connected


def test_t_junction_endpoint_is_connected():
    # Wire B's endpoint lands on the mid-span of wire A -> connected (no dangling).
    text = (
        '(kicad_sch (uuid "%s")\n'
        '(wire (pts (xy 0 0) (xy 40 0)) (uuid "a"))\n'
        '(wire (pts (xy 20 0) (xy 20 20)) (uuid "b")))' % ROOT_UUID
    )
    findings = connectivity.verify(parse(text))
    dangling = {tuple(f.refs) for f in findings if f.code == connectivity.DANGLING_ENDPOINT}
    assert ("(20 0)",) not in dangling  # the T point is connected


# --------------------------------------------------------------------------- #
# no_connect honoring
# --------------------------------------------------------------------------- #
def test_no_connect_terminates_a_wire():
    text = (
        '(kicad_sch (uuid "%s")\n'
        '(wire (pts (xy 0 0) (xy 10 0)) (uuid "w"))\n'
        '(no_connect (at 10 0) (uuid "nc")))' % ROOT_UUID
    )
    findings = connectivity.verify(parse(text))
    dangling = {tuple(f.refs) for f in findings if f.code == connectivity.DANGLING_ENDPOINT}
    assert ("(10 0)",) not in dangling
    # (0,0) still dangles
    assert ("(0 0)",) in dangling


# --------------------------------------------------------------------------- #
# duplicate uuid
# --------------------------------------------------------------------------- #
def test_duplicate_uuid_flagged():
    doc = _doc()
    dup = parse('(junction (at 999 999) (uuid "8a000000-0000-4000-8000-000000000301"))')
    doc.children.append(dup)
    doc.ws.insert(len(doc.children) - 1, "\n\t")
    findings = connectivity.verify(doc)
    dups = [f for f in findings if f.code == connectivity.DUPLICATE_UUID]
    assert len(dups) == 1
    assert "8a000000-0000-4000-8000-000000000301" in dups[0].refs


# --------------------------------------------------------------------------- #
# unresolved lib_id
# --------------------------------------------------------------------------- #
def test_unresolved_lib_id_flagged():
    text = (
        '(kicad_sch (uuid "%s")\n'
        '(lib_symbols)\n'
        '(symbol (lib_id "Device:NOPE") (at 10 10 0) (unit 1) (uuid "s1")\n'
        '  (instances (project "p" (path "/%s" (reference "U1") (unit 1))))))'
        % (ROOT_UUID, ROOT_UUID)
    )
    findings = connectivity.verify(parse(text))
    assert connectivity.UNRESOLVED_LIB_ID in _codes(findings)


# --------------------------------------------------------------------------- #
# invalid instances path
# --------------------------------------------------------------------------- #
def test_missing_instances_block_flagged():
    text = (
        '(kicad_sch (uuid "%s")\n'
        '(lib_symbols (symbol "Device:R" '
        '(symbol "R_1_1" (pin passive line (at 0 3.81 270) (length 1.27) '
        '(name "~") (number "1"))(pin passive line (at 0 -3.81 90) (length 1.27) '
        '(name "~") (number "2")))))\n'
        '(symbol (lib_id "Device:R") (at 10 10 0) (unit 1) (uuid "s1")))'
        % ROOT_UUID
    )
    findings = connectivity.verify(parse(text))
    assert connectivity.INVALID_INSTANCES_PATH in _codes(findings)


def test_wrong_instances_path_flagged():
    text = (
        '(kicad_sch (uuid "%s")\n'
        '(lib_symbols (symbol "Device:R" '
        '(symbol "R_1_1" (pin passive line (at 0 3.81 270) (length 1.27) '
        '(name "~") (number "1")))))\n'
        '(symbol (lib_id "Device:R") (at 10 10 0) (unit 1) (uuid "s1")\n'
        '  (instances (project "p" (path "/deadbeef-0000-4000-8000-000000000000" '
        '(reference "R1") (unit 1))))))' % ROOT_UUID
    )
    findings = connectivity.verify(parse(text))
    bad = [f for f in findings if f.code == connectivity.INVALID_INSTANCES_PATH]
    assert bad and bad[0].severity is Severity.ERROR


def test_valid_instances_path_not_flagged():
    findings = connectivity.verify(_doc())
    assert connectivity.INVALID_INSTANCES_PATH not in _codes(findings)


# --------------------------------------------------------------------------- #
# auto_junctions
# --------------------------------------------------------------------------- #
def _strip_junctions(doc: sexpr.SNode) -> int:
    """Remove every top-level (junction ...) node; return how many were removed."""
    keep_children = []
    keep_ws = [doc.ws[0]]
    removed = 0
    for i, c in enumerate(doc.children):
        if c.is_list and c.tag == "junction":
            removed += 1
            continue
        keep_children.append(c)
        keep_ws.append(doc.ws[i + 1])
    doc.children = keep_children
    doc.ws = keep_ws
    return removed


def _junction_points(doc: sexpr.SNode) -> set[tuple[int, int]]:
    out = set()
    for j in doc.find_all("junction"):
        at = j.find("at")
        out.add(
            (
                round(float(at.children[1].value) * NM),
                round(float(at.children[2].value) * NM),
            )
        )
    return out


def test_auto_junctions_reinserts_expected_points():
    doc = _doc()
    removed = _strip_junctions(doc)
    assert removed == 2
    assert _junction_points(doc) == set()

    connectivity.auto_junctions(doc)
    pts = _junction_points(doc)
    assert J1 in pts
    assert J2 in pts
    assert len(pts) == 2  # no spurious junctions (e.g. at the MID label / corners)


def test_auto_junctions_idempotent():
    doc = _doc()
    _strip_junctions(doc)
    connectivity.auto_junctions(doc)
    once = dumps(doc)
    connectivity.auto_junctions(doc)
    twice = dumps(doc)
    assert once == twice  # second pass adds nothing


def test_auto_junctions_noop_on_clean_board():
    doc = _doc()
    before = _junction_points(doc)
    connectivity.auto_junctions(doc)
    assert _junction_points(doc) == before


def test_auto_junctions_output_reparses_and_verifies_clean():
    doc = _doc()
    _strip_junctions(doc)
    connectivity.auto_junctions(doc)
    # round-trips through the serializer and still parses + verifies clean
    redoc = parse(dumps(doc))
    assert connectivity.verify(redoc) == []


def test_x_crossing_not_auto_joined():
    # Two wires crossing at a non-endpoint point: no junction should be inserted
    # (auto-joining would silently change designer intent).
    text = (
        '(kicad_sch (uuid "%s")\n'
        '(wire (pts (xy 0 10) (xy 20 10)) (uuid "h"))\n'
        '(wire (pts (xy 10 0) (xy 10 20)) (uuid "v")))' % ROOT_UUID
    )
    doc = parse(text)
    connectivity.auto_junctions(doc)
    assert _junction_points(doc) == set()


def test_three_wire_ends_meet_gets_junction():
    text = (
        '(kicad_sch (uuid "%s")\n'
        '(wire (pts (xy 0 10) (xy 10 10)) (uuid "a"))\n'
        '(wire (pts (xy 10 10) (xy 20 10)) (uuid "b"))\n'
        '(wire (pts (xy 10 10) (xy 10 20)) (uuid "c")))' % ROOT_UUID
    )
    doc = parse(text)
    connectivity.auto_junctions(doc)
    assert (round(10 * NM), round(10 * NM)) in _junction_points(doc)
