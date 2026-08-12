"""``review validate`` gates + preflight review policy + domain.usb (M8).

The structural guarantees under test: an LLM candidate is accepted ONLY as an
anchored, evidence-checked ``llm_reviewed`` observation (never blocking,
never wearing a deterministic rule's identity); the preflight review gate
blocks on explicitly allowlisted codes and nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli import cli
from akcli.model import Component, Net, Pin, Schematic
from akcli.review import facts as fx, topo, validate as val
from akcli.review.detectors.domain import usb
from akcli.writers import kicad as kw

DEVICE = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym"


def _comp(ref, lib, value=None, pins=(), params=None):
    return Component(designator=ref, library_ref=lib, x_mil=0, y_mil=0,
                     value=value, parameters=dict(params or {}),
                     pins=[Pin(number=n, name=nm, x_mil=0, y_mil=0)
                           for n, nm in pins])


def _sch(comps, nets):
    return Schematic(source_path="<test>", source_format="kicad",
                     components=comps,
                     nets=[Net(name=n, members=m) for n, m in nets])


def _board():
    return _sch(
        [_comp("R1", "Device:R", "10k"), _comp("U1", "MCU:MCU", "MCU",
             pins=(("1", "VDD"), ("2", "IO"), ("3", "GND")))],
        [("+3V3", [("R1", "1"), ("U1", "1")]),
         ("SIG", [("R1", "2"), ("U1", "2")]),
         ("GND", [("U1", "3")])])


def _cand(**kw):
    base = {"code": "REVIEW_LLM_OBSERVATION", "severity": "note",
            "message": "the SIG series resistor may be redundant",
            "refs": ["R1"],
            "anchors": [{"kind": "component", "id": "R1"},
                        {"kind": "net", "id": "SIG"}]}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# validate gates
# --------------------------------------------------------------------------- #
def test_valid_candidate_accepted_as_llm_reviewed():
    accepted, quarantined = val.validate_candidates(
        {"candidates": [_cand()]}, _board())
    assert quarantined == []
    f = accepted[0]
    assert f.confidence == "llm_reviewed" and f.status == "reported"
    assert f.detector == "review.validate" and f.fingerprint


def test_g1_schema_rejections():
    cases = [
        (_cand(severity="fatal"), "illegal severity"),
        (_cand(code="NOT_REVIEW"), "must start with REVIEW_"),
        (_cand(confidence="deterministic"), "only the deterministic layer"),
        (_cand(status="waived"), "pre-sets"),
        (_cand(anchors=[]), "unanchored"),
    ]
    for cand, needle in cases:
        _a, q = val.validate_candidates({"candidates": [cand]}, _board())
        assert q and any(needle in r for r in q[0]["reasons"]), needle


def test_g2_anchor_existence():
    bad = _cand(anchors=[{"kind": "component", "id": "R99"}])
    _a, q = val.validate_candidates({"candidates": [bad]}, _board())
    assert any("R99" in r for r in q[0]["reasons"])
    bad_pin = _cand(anchors=[{"kind": "pin", "id": "U1.9"}])
    _a, q = val.validate_candidates({"candidates": [bad_pin]}, _board())
    assert any("U1.9" in r for r in q[0]["reasons"])


def test_g3_datasheet_evidence():
    ds_cand = _cand(evidence={"source": "datasheet",
                              "datasheet": {"sha256": "9" * 64, "page": 3}})
    # no facts store at all → unverifiable → quarantined
    _a, q = val.validate_candidates({"candidates": [ds_cand]}, _board())
    assert any("no facts store" in r for r in q[0]["reasons"])
    # store present but sha unknown → quarantined
    store = fx.FactsStore()
    f = fx.Facts(mpn="X1", sha256="ab" * 32, pdf="x.pdf")
    store.by_mpn["X1"] = f
    _a, q = val.validate_candidates({"candidates": [ds_cand]}, _board(),
                                    facts=store)
    assert any("matches no PDF" in r for r in q[0]["reasons"])
    # matching sha accepted
    ok = _cand(evidence={"source": "datasheet",
                         "datasheet": {"sha256": "ab" * 32, "page": 3}})
    a, q = val.validate_candidates({"candidates": [ok]}, _board(),
                                   facts=store)
    assert not q and a[0].evidence["datasheet"]["page"] == 3


def test_g4_masquerade_rejected():
    imp = _cand(code="REVIEW_XTAL_LOAD")     # a registered deterministic rule
    _a, q = val.validate_candidates({"candidates": [imp]}, _board())
    assert any("masquerade" in r for r in q[0]["reasons"])


def test_cli_validate_roundtrip(tmp_path, capsys):
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "77777777-8888-9999-aaaa-bbbbbbbbbbbb") (paper "A4"))\n')
    rs = kw.apply({"protocol_version": 2, "target_format": "kicad", "ops": [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 2000, "y_mil": 1000, "value": "10k"},
        {"op": "add_net_label", "name": "SIG", "at": "R1.2"},
        {"op": "add_net_label", "name": "GND", "at": "R1.1"},
    ]}, str(tgt), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs)
    cands = tmp_path / "cands.json"
    cands.write_text(json.dumps({"candidates": [
        _cand(anchors=[{"kind": "component", "id": "R1"},
                       {"kind": "net", "id": "SIG"}]),
        _cand(code="REVIEW_LLM_GHOST",
              anchors=[{"kind": "component", "id": "R77"}]),
    ]}))
    out = tmp_path / "validated.json"
    assert cli.main(["review", "validate", str(cands), str(tgt),
                     "--out", str(out), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["findings"]) == 1
    assert doc["findings"][0]["confidence"] == "llm_reviewed"
    assert doc["metadata"]["validate_quarantined"] == 1
    assert "R77" in doc["metadata"]["quarantined"][0]["reasons"][0]


# --------------------------------------------------------------------------- #
# domain.usb
# --------------------------------------------------------------------------- #
def _usb_sch(rd=None, with_controller=False):
    comps = [_comp("J1", "Connector:USB_C_Receptacle", "USB-C",
                   pins=(("A5", "CC1"), ("B5", "CC2"), ("A4", "VBUS")))]
    cc1 = [("J1", "A5")]
    cc2 = [("J1", "B5")]
    nets = [("VBUS", [("J1", "A4")])]
    if rd is not None:
        comps += [_comp("R1", "Device:R", rd), _comp("R2", "Device:R", rd)]
        cc1.append(("R1", "1"))
        cc2.append(("R2", "1"))
        nets.append(("GND", [("R1", "2"), ("R2", "2")]))
    if with_controller:
        comps.append(_comp("U5", "Interface:USBC_CTRL", "TUSB320",
                           pins=(("1", "CC1"), ("2", "CC2"), ("3", "VDD"))))
        cc1.append(("U5", "1"))
        cc2.append(("U5", "2"))
        nets.append(("+3V3", [("U5", "3")]))
    return _sch(comps, [("CC1", cc1), ("CC2", cc2)] + nets)


def test_usb_cc_missing_rd():
    fs = usb.run(topo.build_ctx(_usb_sch()))
    assert [f.code for f in fs] == ["REVIEW_USB_CC_MISSING"] * 2


def test_usb_cc_correct_rd_silent():
    assert usb.run(topo.build_ctx(_usb_sch(rd="5.1k"))) == []


def test_usb_cc_wrong_value():
    fs = usb.run(topo.build_ctx(_usb_sch(rd="10k")))
    assert [f.code for f in fs] == ["REVIEW_USB_CC_VALUE"] * 2
    assert fs[0].evidence["calc"]["rd_ohms"] == 5100.0


def test_usb_cc_controller_integrates_rd():
    assert usb.run(topo.build_ctx(_usb_sch(with_controller=True))) == []


# --------------------------------------------------------------------------- #
# release preflight --review-policy
# --------------------------------------------------------------------------- #
def _seed_divider_sheet(tmp_path):
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "88888888-9999-aaaa-bbbb-cccccccccccc") (paper "A4"))\n')
    rs = kw.apply({"protocol_version": 2, "target_format": "kicad", "ops": [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 2000, "y_mil": 1000, "value": "10k"},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
         "x_mil": 2000, "y_mil": 1400, "value": "30k"},
        {"op": "add_net_label", "name": "+5V", "at": "R1.1"},
        {"op": "add_net_label", "name": "2V5_REF", "at": "R1.2"},
        {"op": "add_net_label", "name": "2V5_REF", "at": "R2.1"},
        {"op": "add_net_label", "name": "GND", "at": "R2.2"},
    ]}, str(tgt), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs)
    return tgt


def test_preflight_review_gate_allowlist_blocks(tmp_path, capsys):
    tgt = _seed_divider_sheet(tmp_path)
    pol = tmp_path / "policy.toml"
    pol.write_text('[review]\nprofile = "standard"\n'
                   'allow = ["REVIEW_DIVIDER_TAP_MISMATCH"]\n')
    code = cli.main(["release", "preflight", "--sch", str(tgt),
                     "--review-policy", str(pol), "--allow-dirty", "--json"])
    doc = json.loads(capsys.readouterr().out)
    review_gate = next(g for g in doc["gates"] if g["gate"] == "review")
    assert review_gate["status"] == "fail"
    assert [f["code"] for f in review_gate["findings"]] == \
        ["REVIEW_DIVIDER_TAP_MISMATCH"]
    assert doc["inputs"]["review_policy"]["allow"] == \
        ["REVIEW_DIVIDER_TAP_MISMATCH"]
    assert code == 1


def test_preflight_review_gate_ignores_unlisted_codes(tmp_path, capsys):
    tgt = _seed_divider_sheet(tmp_path)
    pol = tmp_path / "policy.toml"
    pol.write_text('[review]\nallow = ["REVIEW_PCB_UNROUTED"]\n')
    cli.main(["release", "preflight", "--sch", str(tgt),
              "--review-policy", str(pol), "--allow-dirty", "--json"])
    doc = json.loads(capsys.readouterr().out)
    review_gate = next(g for g in doc["gates"] if g["gate"] == "review")
    # the tap mismatch exists but is NOT allowlisted → the gate passes
    assert review_gate["status"] == "pass"
    assert review_gate["findings"] == []


def test_preflight_review_skipped_without_policy(tmp_path, capsys):
    tgt = _seed_divider_sheet(tmp_path)
    cli.main(["release", "preflight", "--sch", str(tgt),
              "--allow-dirty", "--json"])
    doc = json.loads(capsys.readouterr().out)
    review_gate = next(g for g in doc["gates"] if g["gate"] == "review")
    assert review_gate["status"] == "skipped"
    assert "advisory" in review_gate["reason"]


def test_preflight_review_policy_requires_allowlist(tmp_path, capsys):
    tgt = _seed_divider_sheet(tmp_path)
    pol = tmp_path / "policy.toml"
    pol.write_text("[review]\nprofile = \"standard\"\n")   # no allow list
    assert cli.main(["release", "preflight", "--sch", str(tgt),
                     "--review-policy", str(pol), "--allow-dirty"]) == 2
    assert "allow" in capsys.readouterr().err
