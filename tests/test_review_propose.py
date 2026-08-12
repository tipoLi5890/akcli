"""``review propose`` / ``diff`` / ``tree`` — the closed loop (M7).

The load-bearing guarantee: a proposal with open ``requires_confirmation``
items NEVER carries an op-list draft (enforced in code AND in the shipped
schema); value fixes are recomputed + E-series-snapped here, not copied from
the finding; contract drafts carry the datasheet sha256+page — the
sedimentation chain closes.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from akcli import cli, ops
from akcli.model import Component, Net, Pin, Schematic
from akcli.review import diff as diffmod
from akcli.review import engine, facts as fx, propose, topo, tree

_ROOT = Path(__file__).parent.parent
PROPOSALS_SCHEMA = json.loads(
    (_ROOT / "schemas" / "proposals.schema.json").read_text())


def _comp(ref, lib, value=None, pins=(), params=None):
    return Component(designator=ref, library_ref=lib, x_mil=0, y_mil=0,
                     value=value, parameters=dict(params or {}),
                     pins=[Pin(number=n, name=nm, x_mil=0, y_mil=0)
                           for n, nm in pins])


def _sch(comps, nets):
    return Schematic(source_path="<test>", source_format="kicad",
                     components=comps,
                     nets=[Net(name=n, members=m) for n, m in nets])


def _fb_sch():
    comps = [_comp("R1", "Device:R", "82k"),
             _comp("R2", "Device:R", "10k"),
             _comp("U1", "Regulator:BUCK", "BUCK",
                   pins=(("1", "VIN"), ("2", "FB"), ("3", "GND")),
                   params={"MPN": "BUCK1"})]
    nets = [("+5V", [("R1", "1"), ("U1", "1")]),
            ("FB_NODE", [("R1", "2"), ("R2", "1"), ("U1", "2")]),
            ("GND", [("R2", "2"), ("U1", "3")])]
    return _sch(comps, nets)


def _vref_store(vref=0.8):
    f = fx.Facts(mpn="BUCK1", sha256="ab" * 32, pdf="x.pdf")
    f.values["vref"] = fx.FactValue(key="vref", unit="V", page=5, value=vref,
                                    quote="VFB = 0.8 V", sha256=f.sha256)
    store = fx.FactsStore()
    store.by_mpn["BUCK1"] = f
    return store


def _findings_doc(findings):
    from akcli import report
    return json.loads(report.render(findings, "json"))


# --------------------------------------------------------------------------- #
# propose
# --------------------------------------------------------------------------- #
def test_fb_retune_proposal_recomputes_and_snaps_e96():
    findings, _ = engine.analyze(_fb_sch(), profile="fast",
                                 facts=_vref_store())
    doc = propose.build_proposals(_findings_doc(findings))
    jsonschema.validate(doc, PROPOSALS_SCHEMA)
    prs = doc["proposals"]
    assert len(prs) == 1
    pr = prs[0]
    assert pr["kind"] == "set_value" and pr["requires_confirmation"] == []
    # ideal R_top = 10k·(5/0.8 − 1) = 52.5k → E96 nearest 52.3k
    op = pr["oplist_draft"]["ops"][0]
    assert op == {"op": "set_component_parameters", "designator": "R1",
                  "value": "52.3k"}
    # contract draft carries the datasheet evidence (sedimentation chain)
    assert "sha256:abababababababab" in pr["contract_draft"]
    assert 'component = "R1"' in pr["contract_draft"]
    assert 'value = "52.3k"' in pr["contract_draft"]
    # sim draft asserts the target Vout with a tolerance
    assert pr["sim_draft"]["assertions"][0]["approx"] == 5.0
    # traceability
    assert pr["finding_fingerprint"] == findings[0].fingerprint


def test_confirm_proposals_carry_no_oplist():
    findings, _ = engine.analyze(_fb_sch(), profile="fast")   # no facts
    doc = propose.build_proposals(_findings_doc(findings))
    jsonschema.validate(doc, PROPOSALS_SCHEMA)
    # heuristic-band finding (implied vref 0.543 plausible → INFO no fix) —
    # build an implausible one instead:
    sch = _fb_sch()
    sch.components[0].value = "1k"       # R1=1k → vref 5·10/11 = 4.5 V
    findings, _ = engine.analyze(sch, profile="fast")
    doc = propose.build_proposals(_findings_doc(findings))
    prs = [p for p in doc["proposals"] if p["finding_code"] ==
           "REVIEW_FB_DIVIDER_VREF"]
    assert len(prs) == 1
    pr = prs[0]
    assert pr["kind"] == "confirm"
    assert pr["requires_confirmation"] and pr["oplist_draft"] is None
    jsonschema.validate(doc, PROPOSALS_SCHEMA)


def test_layout_proposals_never_carry_oplist():
    doc = propose.build_proposals({"findings": [{
        "code": "REVIEW_DECAP_DISTANCE", "severity": "warning",
        "message": "far", "refs": ["C1"], "fingerprint": "0" * 32,
        "fix_params": {"kind": "move_decap", "cap": "C1",
                       "target_pad": "U1.8", "distance_mm": 8.0},
    }]})
    jsonschema.validate(doc, PROPOSALS_SCHEMA)
    pr = doc["proposals"][0]
    assert pr["kind"] == "layout" and pr["oplist_draft"] is None
    assert any("akcli writes schematics only" in c
               for c in pr["requires_confirmation"])


def test_xtal_retune_snaps_e24():
    doc = propose.build_proposals({"findings": [{
        "code": "REVIEW_XTAL_LOAD_MISMATCH", "severity": "warning",
        "message": "m", "refs": ["Y1"], "fingerprint": "1" * 32,
        "evidence": {"source": "datasheet",
                     "datasheet": {"sha256": "cd" * 32, "page": 3}},
        "fix_params": {"kind": "xtal_load_retune", "c1": "C1", "c2": "C2",
                       "c_suggested_pf": 12.6},
    }]})
    pr = doc["proposals"][0]
    ops = pr["oplist_draft"]["ops"]
    assert [o["designator"] for o in ops] == ["C1", "C2"]
    assert all(o["value"] == "13p" for o in ops)     # E24 nearest of 12.6p
    assert "p3" in pr["contract_draft"]


def test_pullup_retune_uses_calc_suggested():
    doc = propose.build_proposals({"findings": [{
        "code": "REVIEW_I2C_PULLUP_STRONG", "severity": "warning",
        "message": "m", "refs": ["R1"], "fingerprint": "2" * 32,
        "evidence": {"source": "calc",
                     "calc": {"results": {"suggested": {"value": 1800.0}}}},
        "fix_params": {"kind": "retune_pullup", "ref": "R1", "r_min": 966.7},
    }]})
    op = doc["proposals"][0]["oplist_draft"]["ops"][0]
    assert op["designator"] == "R1" and op["value"] == "1.8k"


def test_findings_without_fix_params_are_skipped():
    doc = propose.build_proposals({"findings": [{
        "code": "REVIEW_RC_CUTOFF", "severity": "info", "message": "m",
        "refs": ["R5"], "fingerprint": "3" * 32}]})
    assert doc["proposals"] == []


def test_proposals_schema_mirror_is_byte_identical():
    repo = (_ROOT / "schemas" / "proposals.schema.json").read_bytes()
    pkg = (_ROOT / "src" / "akcli" / "schemas" /
           "proposals.schema.json").read_bytes()
    assert repo == pkg


def test_schema_rejects_unconfirmed_oplist():
    doc = propose.build_proposals({"findings": []})
    doc["proposals"] = [{
        "id": "P1", "finding_fingerprint": "0" * 32,
        "finding_code": "X", "kind": "confirm", "summary": "s",
        "requires_confirmation": ["something"],
        "oplist_draft": {"ops": []}, "contract_draft": None,
        "sim_draft": None, "status": "proposed"}]
    import pytest
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, PROPOSALS_SCHEMA)


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #
def _fdoc(*findings):
    return {"findings": list(findings)}


def _f(code, fp, severity="warning", confidence="heuristic", msg="m",
       refs=()):
    return {"code": code, "severity": severity, "confidence": confidence,
            "message": msg, "refs": list(refs), "fingerprint": fp}


def test_diff_added_resolved_changed_persisting():
    old = _fdoc(_f("A", "a" * 32), _f("B", "b" * 32),
                _f("C", "c" * 32, severity="warning"))
    new = _fdoc(_f("B", "b" * 32),                       # persisting
                _f("C", "c" * 32, severity="warning",
                   confidence="datasheet_backed"),       # changed
                _f("D", "d" * 32))                       # added
    d = diffmod.diff_findings(old, new)
    assert [x["code"] for x in d["added"]] == ["D"]
    assert [x["code"] for x in d["resolved"]] == ["A"]
    assert [x["code"] for x in d["severity_changed"]] == ["C"]
    assert d["severity_changed"][0]["now"]["confidence"] == "datasheet_backed"
    assert d["persisting"] == 1


def test_diff_falls_back_to_code_refs_without_fingerprint():
    old = _fdoc({"code": "X", "severity": "note", "message": "m",
                 "refs": ["R1"]})
    new = _fdoc({"code": "X", "severity": "note", "message": "reworded!",
                 "refs": ["R1"]})
    d = diffmod.diff_findings(old, new)
    assert d["persisting"] == 1 and not d["added"] and not d["resolved"]


# --------------------------------------------------------------------------- #
# tree
# --------------------------------------------------------------------------- #
def test_power_tree_finds_regulator_and_consumers():
    comps = [_comp("R1", "Device:R", "82k"), _comp("R2", "Device:R", "10k"),
             _comp("U1", "Regulator:BUCK", "BUCK",
                   pins=(("1", "VIN"), ("2", "FB"), ("3", "GND"))),
             _comp("U2", "MCU:MCU", "MCU",
                   pins=(("1", "VDD"), ("2", "IO"), ("3", "GND"))),
             _comp("C1", "Device:C", "100n")]
    nets = [("+12V", [("U1", "1")]),
            ("+5V", [("R1", "1"), ("U2", "1"), ("C1", "1")]),
            ("FB_NODE", [("R1", "2"), ("R2", "1"), ("U1", "2")]),
            ("SIG", [("U2", "2")]),
            ("GND", [("R2", "2"), ("U1", "3"), ("U2", "3"), ("C1", "2")])]
    doc = tree.power_tree(_sch(comps, nets))
    rails = {r["net"]: r for r in doc["rails"]}
    assert set(rails) == {"+12V", "+5V"}
    five = rails["+5V"]
    assert five["regulator"]["ref"] == "U1"
    assert five["regulator"]["divider"] == ["R1", "R2"]
    assert five["consumers"] == ["U2"]
    assert five["decoupling_caps"] == 1
    assert doc["rails"][0]["net"] == "+12V"          # sorted by voltage desc
    text = tree.render_text(doc)
    assert "regulated by U1" in text and "└─ U2" in text


# --------------------------------------------------------------------------- #
# CLI round trips
# --------------------------------------------------------------------------- #
def test_cli_propose_diff_tree(tmp_path, capsys, monkeypatch):
    # findings file with one auto-applicable fix
    findings, meta = engine.analyze(_fb_sch(), profile="fast",
                                    facts=_vref_store())
    from akcli import report
    f1 = tmp_path / "one.json"
    f1.write_text(report.render(findings, "json", meta=meta), encoding="utf-8")
    out = tmp_path / "proposals.json"
    assert cli.main(["review", "propose", str(f1), "--out", str(out)]) == 0
    capsys.readouterr()
    doc = json.loads(out.read_text(encoding="utf-8"))
    jsonschema.validate(doc, PROPOSALS_SCHEMA)
    assert (doc["proposals"][0]["oplist_draft"]["protocol_version"]
            == ops.PROTOCOL_VERSION)

    # diff: fixed board (facts satisfied) vs original
    fixed, meta2 = engine.analyze(_fb_sch(), profile="fast",
                                  facts=_vref_store(vref=0.55))
    f2 = tmp_path / "two.json"
    f2.write_text(report.render(fixed, "json", meta=meta2), encoding="utf-8")
    assert cli.main(["review", "diff", str(f1), str(f2), "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["added"] and d["resolved"]
    assert cli.main(["review", "diff", str(f1), str(f2),
                     "--fail-on-new"]) == 1
    capsys.readouterr()
