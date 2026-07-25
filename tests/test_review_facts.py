"""Datasheet facts store (review M4): schema, CLI, and detector upgrades.

The store's whole promise: a ``datasheet_backed`` finding always traces to a
sha256-pinned PDF page — and without a fact, detectors fall back to their
heuristics, never to a guess.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from akcli import cli, report
from akcli.model import Component, Net, Pin, Schematic
from akcli.review import engine, facts as fx, topo
from akcli.review.detectors.signal import crystal, divider
from akcli.review.detectors.validation import vdomain

_ROOT = Path(__file__).parent.parent
FACTS_SCHEMA = json.loads(
    (_ROOT / "schemas" / "datasheet-facts.schema.json").read_text())
FINDINGS_SCHEMA = json.loads(
    (_ROOT / "schemas" / "findings.schema.json").read_text())


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _comp(ref, lib, value=None, pins=(), params=None):
    return Component(designator=ref, library_ref=lib, x_mil=0, y_mil=0,
                     value=value, parameters=dict(params or {}),
                     pins=[Pin(number=n, name=nm, x_mil=0, y_mil=0)
                           for n, nm in pins])


def _sch(comps, nets):
    return Schematic(source_path="<test>", source_format="kicad",
                     components=comps,
                     nets=[Net(name=n, members=m) for n, m in nets])


def _store(mpn: str, **kv) -> fx.FactsStore:
    """In-memory store: ``kv`` maps fact key -> (value, unit, page)."""
    f = fx.Facts(mpn=mpn, sha256="ab" * 32, pdf="x.pdf")
    for key, (value, unit, page) in kv.items():
        f.values[key] = fx.FactValue(key=key, unit=unit, page=page,
                                     value=value, sha256=f.sha256, pdf=f.pdf)
    store = fx.FactsStore()
    store.by_mpn[mpn.upper()] = f
    return store


def _run(det, sch, store=None):
    return det.run(topo.build_ctx(sch, facts=store))


# --------------------------------------------------------------------------- #
# schema + serialization
# --------------------------------------------------------------------------- #
def test_facts_schema_mirror_is_byte_identical():
    repo = (_ROOT / "schemas" / "datasheet-facts.schema.json").read_bytes()
    pkg = (_ROOT / "src" / "akcli" / "schemas" /
           "datasheet-facts.schema.json").read_bytes()
    assert repo == pkg


def test_facts_roundtrip_validates_and_reloads():
    store = _store("TPS61023", vref=(0.6, "V", 5),
                   abs_max_io=(6.0, "V", 2))
    doc = fx.facts_to_doc(store.lookup("tps61023"))
    jsonschema.validate(doc, FACTS_SCHEMA)
    back = fx._facts_from_doc(doc, None)
    assert back.mpn == "TPS61023"
    assert back.get("vref").best() == 0.6
    assert back.get("vref").evidence() == {"sha256": "ab" * 32, "page": 5}


def test_fact_best_prefers_value_then_typ_then_mid():
    v = fx.FactValue(key="k", unit="V", page=1, min=1.0, max=3.0)
    assert v.best() == 2.0
    assert fx.FactValue(key="k", unit="V", page=1, typ=1.2, min=1.0).best() == 1.2
    assert fx.FactValue(key="k", unit="V", page=1, value=5, typ=1).best() == 5


def test_parse_set_syntax():
    assert fx.parse_set("vref=0.6V@5") == ("vref", 0.6, "V", 5)
    assert fx.parse_set("load_capacitance=12pF@3") == \
        ("load_capacitance", 12e-12, "F", 3)
    assert fx.parse_set("abs_max_io=6V@p2") == ("abs_max_io", 6.0, "V", 2)
    assert fx.parse_set("i_limit=3mA@7") == ("i_limit", 0.003, "A", 7)
    for bad in ("vref=0.6V", "Vref=1V@2", "vref=@3", "vref=1V@0x2"):
        with pytest.raises(ValueError):
            fx.parse_set(bad)


def test_component_mpn_sources():
    assert fx.component_mpn(_comp("U1", "X", params={"MPN": "TPS61023"})) \
        == "TPS61023"
    assert fx.component_mpn(_comp("U1", "X", value="LM2731XMF")) == "LM2731XMF"
    assert fx.component_mpn(_comp("R1", "Device:R", value="10k")) is None


# --------------------------------------------------------------------------- #
# CLI: add / lookup / verify
# --------------------------------------------------------------------------- #
def _seed_pdf(tmp_path: Path) -> Path:
    root = tmp_path / "datasheets"
    root.mkdir()
    (root / "BUCK1.pdf").write_bytes(b"%PDF-1.4 Vref = 0.60 V typical\n")
    return root


def test_cli_facts_add_lookup_verify_roundtrip(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = _seed_pdf(tmp_path)
    assert cli.main(["review", "facts", "add", "BUCK1",
                     "--pdf", str(root / "BUCK1.pdf"),
                     "--dir", str(root),
                     "--set", "vref=0.6V@5", "--set", "en_vih=1.2V@6"]) == 0
    capsys.readouterr()
    # written file validates against the shipped schema
    doc = json.loads((root / "extracted" / "BUCK1.json").read_text(encoding="utf-8"))
    jsonschema.validate(doc, FACTS_SCHEMA)
    assert doc["source"]["pdf"] == "BUCK1.pdf"          # relative to the dir

    assert cli.main(["review", "facts", "lookup", "BUCK1",
                     "--dir", str(root)]) == 0
    out = capsys.readouterr().out
    assert "vref" in out and "@p5" in out

    assert cli.main(["review", "facts", "verify", "--dir", str(root)]) == 0
    capsys.readouterr()


def test_cli_facts_verify_flags_stale_pdf(tmp_path, capsys):
    root = _seed_pdf(tmp_path)
    assert cli.main(["review", "facts", "add", "BUCK1",
                     "--pdf", str(root / "BUCK1.pdf"), "--dir", str(root),
                     "--set", "vref=0.6V@5"]) == 0
    capsys.readouterr()
    (root / "BUCK1.pdf").write_bytes(b"%PDF-1.4 a different document\n")
    assert cli.main(["review", "facts", "verify", "--dir", str(root)]) == 1
    assert "FACTS_STALE" in capsys.readouterr().out


def test_cli_facts_add_requires_pdf(tmp_path, capsys):
    assert cli.main(["review", "facts", "add", "BUCK1",
                     "--dir", str(tmp_path)]) == 2
    assert "required" in capsys.readouterr().err


def test_verify_quote_note_without_pdftotext(tmp_path, monkeypatch):
    from akcli.drivers import pdftotext
    monkeypatch.setattr(pdftotext, "available", lambda: False)
    root = _seed_pdf(tmp_path)
    f = fx.Facts(mpn="BUCK1", sha256=fx.sha256_file(root / "BUCK1.pdf"),
                 pdf="BUCK1.pdf")
    f.values["vref"] = fx.FactValue(key="vref", unit="V", page=1, value=0.6,
                                    quote="Vref = 0.60 V typical",
                                    sha256=f.sha256, pdf=f.pdf)
    findings = fx.verify_facts(f, root)
    assert [x.code for x in findings] == ["FACTS_QUOTE_UNVERIFIED"]
    assert findings[0].severity is report.Severity.NOTE


# --------------------------------------------------------------------------- #
# detector upgrades: datasheet_backed judgements
# --------------------------------------------------------------------------- #
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


def test_fb_divider_vref_mismatch_is_datasheet_backed():
    # implied Vref = 5 × 10/92 = 0.543 V; datasheet says 0.80 V → mismatch
    fs = _run(divider, _fb_sch(), _store("BUCK1", vref=(0.8, "V", 5)))
    assert [f.code for f in fs] == ["REVIEW_FB_DIVIDER_VREF_MISMATCH"]
    f = fs[0]
    assert f.confidence == "datasheet_backed"
    assert f.evidence["datasheet"]["page"] == 5
    assert f.fix_params["vref_spec"] == 0.8


def test_fb_divider_vref_match_confirms():
    # implied Vref = 5 × 10/92 = 0.5435 V; datasheet 0.55 V → within 5 %
    fs = _run(divider, _fb_sch(), _store("BUCK1", vref=(0.55, "V", 5)))
    assert [f.code for f in fs] == ["REVIEW_FB_DIVIDER"]
    assert fs[0].confidence == "datasheet_backed"
    assert "matches" in fs[0].message


def test_fb_divider_without_facts_keeps_heuristic():
    fs = _run(divider, _fb_sch(), None)
    assert fs and all(f.confidence != "datasheet_backed" for f in fs)


def test_fb_divider_facts_findings_validate_against_findings_schema():
    fs = _run(divider, _fb_sch(), _store("BUCK1", vref=(0.8, "V", 5)))
    payload = report.render(fs, "json")
    jsonschema.validate(json.loads(payload), FINDINGS_SCHEMA)


def _xtal_sch():
    comps = [_comp("Y1", "Device:Crystal", "ABM8-8MHz",
                   params={"MPN": "ABM8-8.000MHZ"}),
             _comp("C1", "Device:C", "22p"), _comp("C2", "Device:C", "22p")]
    nets = [("OSC1", [("Y1", "1"), ("C1", "1")]),
            ("OSC2", [("Y1", "2"), ("C2", "1")]),
            ("GND", [("C1", "2"), ("C2", "2")])]
    return _sch(comps, nets)


def test_crystal_cl_mismatch_suggests_cap_value():
    # computed CL = 11 + 4 = 15 pF; datasheet CL = 10 pF → mismatch
    fs = _run(crystal, _xtal_sch(),
              _store("ABM8-8.000MHZ", load_capacitance=(10e-12, "F", 3)))
    assert [f.code for f in fs] == ["REVIEW_XTAL_LOAD_MISMATCH"]
    f = fs[0]
    assert f.confidence == "datasheet_backed"
    # C = 2·(CL − Cstray) = 2·(10 − 4) = 12 pF
    assert abs(f.fix_params["c_suggested_pf"] - 12.0) < 0.01


def test_crystal_cl_match_confirms():
    # computed 15 pF vs spec 15 pF → confirmation, datasheet_backed INFO
    fs = _run(crystal, _xtal_sch(),
              _store("ABM8-8.000MHZ", load_capacitance=(15e-12, "F", 3)))
    assert [f.code for f in fs] == ["REVIEW_XTAL_LOAD"]
    assert fs[0].confidence == "datasheet_backed"


def _vdomain_sch():
    comps = [
        _comp("U1", "MCU:BIG", "TX",
              pins=(("1", "TX"), ("2", "VDD"), ("3", "GND"))),
        _comp("U2", "MCU:SMALL", "RX",
              pins=(("1", "RX"), ("2", "VDD"), ("3", "GND")),
              params={"MPN": "RX3V3"}),
    ]
    nets = [("UART_TX", [("U1", "1"), ("U2", "1")]),
            ("+5V", [("U1", "2")]), ("+3V3", [("U2", "2")]),
            ("GND", [("U1", "3"), ("U2", "3")])]
    return _sch(comps, nets)


def test_vdomain_tolerant_pin_downgrades_to_info():
    fs = _run(vdomain, _vdomain_sch(),
              _store("RX3V3", abs_max_io=(5.5, "V", 2)))
    assert [f.code for f in fs] == ["REVIEW_VDOMAIN_CROSS"]
    f = fs[0]
    assert f.severity is report.Severity.INFO
    assert f.confidence == "datasheet_backed" and "tolerant" in f.message


def test_vdomain_violation_is_datasheet_backed_warning():
    fs = _run(vdomain, _vdomain_sch(),
              _store("RX3V3", abs_max_io=(3.6, "V", 2)))
    f = fs[0]
    assert f.severity is report.Severity.WARNING
    assert f.confidence == "datasheet_backed"
    assert "3.6" in f.message and f.evidence["datasheet"]["page"] == 2


def test_vdomain_without_facts_stays_heuristic():
    fs = _run(vdomain, _vdomain_sch(), None)
    assert fs[0].confidence == "heuristic"


# --------------------------------------------------------------------------- #
# engine + store loading
# --------------------------------------------------------------------------- #
def test_load_store_indexes_and_reports_errors(tmp_path):
    root = tmp_path / "datasheets"
    ex = root / "extracted"
    ex.mkdir(parents=True)
    good = fx.facts_to_doc(_store("BUCK1", vref=(0.6, "V", 5)).lookup("BUCK1"))
    (ex / "BUCK1.json").write_text(json.dumps(good))
    (ex / "broken.json").write_text("{not json")
    store = fx.load_store(root)
    assert store.lookup("buck1").get("vref").best() == 0.6
    assert store.errors and "broken.json" in store.errors[0]


def test_engine_metadata_lists_facts_mpns():
    sch = _fb_sch()
    fs, meta = engine.analyze(sch, profile="fast",
                              facts=_store("BUCK1", vref=(0.8, "V", 5)))
    assert meta["facts_mpns"] == ["BUCK1"]
    assert any(f.confidence == "datasheet_backed" for f in fs)


# --------------------------------------------------------------------------- #
# facts linkage audit (engine-level REVIEW_FACTS_UNMATCHED)
# --------------------------------------------------------------------------- #
def test_unmatched_facts_emit_engine_advisory():
    # a facts file whose MPN matches no component must not fail silently
    sch = _sch([_comp("R1", "Device:R", value="10k")], [])
    store = _store("TPS61023", vref=(0.6, "V", 5))
    findings, meta = engine.analyze(sch, facts=store)
    (f,) = [f for f in findings if f.code == "REVIEW_FACTS_UNMATCHED"]
    assert f.severity is report.Severity.NOTE
    assert "TPS61023" in f.message and "MPN" in f.message
    assert meta["facts_unmatched"] == ["TPS61023"]


def test_matched_facts_emit_no_advisory():
    sch = _sch([_comp("U1", "X", params={"MPN": "TPS61023"})], [])
    store = _store("TPS61023", vref=(0.6, "V", 5))
    findings, meta = engine.analyze(sch, facts=store)
    assert not [f for f in findings if f.code == "REVIEW_FACTS_UNMATCHED"]
    assert meta["facts_unmatched"] == []
