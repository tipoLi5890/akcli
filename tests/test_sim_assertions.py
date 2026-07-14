"""Tests for the sim.json spec + meas round-trip (sim/assertions.py).

Covers load() shape validation (BAD_CONFIG / PROTOCOL_MISMATCH), meas
statement emission (analysis inference), parse_meas_output() on VERBATIM
lines captured from a live libngspice 45.2 session, and evaluate()
pass/fail/approx/failed-measurement.
"""

from __future__ import annotations

import json

import pytest

from akcli.errors import AkcliError
from akcli.sim import assertions as sa


def _write(tmp_path, doc, name="sim.json") -> str:
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return str(p)


def _base_doc(**overrides) -> dict:
    doc = {
        "protocol_version": 1,
        "analyses": {"tran": "5u 100m", "ac": "dec 40 10 100k"},
        "assert": [
            {"name": "vpeak_max", "meas": "MAX v(peak) from=20m to=60m", "gt": "0.35"},
        ],
    }
    doc.update(overrides)
    return doc


# --------------------------------------------------------------------------- #
# load(): golden table
# --------------------------------------------------------------------------- #
def test_load_minimal_ok(tmp_path):
    spec = sa.load(_write(tmp_path, _base_doc()))
    assert spec.analyses == {"tran": "5u 100m", "ac": "dec 40 10 100k"}
    assert len(spec.asserts) == 1
    assert spec.asserts[0]["name"] == "vpeak_max"
    assert spec.asserts[0]["gt"] == pytest.approx(0.35)
    assert spec.stimuli == []
    assert spec.models == {}
    assert spec.options == {}


def test_load_full_shape_ok(tmp_path):
    doc = _base_doc(
        stimuli=[{"name": "Vin", "kind": "pwl", "node": "in"}],
        models={"DBAT": ".model DBAT D(IS=1e-8 N=1.05)"},
        options={"gnd": "GND"},
    )
    doc["assert"].append(
        {"name": "t_detect", "when": "v(peak)=0.297 RISE=1", "lt": "25m", "analysis": "tran"}
    )
    spec = sa.load(_write(tmp_path, doc))
    assert spec.stimuli == [{"name": "Vin", "kind": "pwl", "node": "in"}]
    assert spec.models == {"DBAT": ".model DBAT D(IS=1e-8 N=1.05)"}
    assert spec.options == {"gnd": "GND"}
    assert len(spec.asserts) == 2


def test_load_engineering_suffix_bounds(tmp_path):
    doc = _base_doc()
    doc["assert"][0]["gt"] = "350m"
    spec = sa.load(_write(tmp_path, doc))
    assert spec.asserts[0]["gt"] == pytest.approx(0.350)


def test_load_approx_with_tol(tmp_path):
    doc = _base_doc()
    doc["assert"] = [
        {"name": "vpeak_max", "meas": "MAX v(peak) from=20m to=60m",
         "approx": "0.3", "tol": "0.1"},
    ]
    spec = sa.load(_write(tmp_path, doc))
    assert spec.asserts[0]["approx"] == pytest.approx(0.3)
    assert spec.asserts[0]["tol"] == pytest.approx(0.1)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda d: d.pop("protocol_version"), "protocol_version"),
        (lambda d: d.__setitem__("protocol_version", "1"), "protocol_version"),
        (lambda d: d.__setitem__("bogus_key", 1), "unknown key"),
        (lambda d: d.__setitem__("analyses", ["tran"]), "analyses"),
        (lambda d: d.__setitem__("stimuli", {"a": 1}), "stimuli"),
        (lambda d: d.__setitem__("models", []), "models"),
        (lambda d: d.__setitem__("options", []), "options"),
        (lambda d: d.__setitem__("assert", {}), "'assert' must be an array"),
    ],
)
def test_load_bad_config_top_level(tmp_path, mutate, match):
    doc = _base_doc()
    mutate(doc)
    with pytest.raises(AkcliError) as ei:
        sa.load(_write(tmp_path, doc))
    assert ei.value.code == "BAD_CONFIG"
    assert match.lower() in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# two-sided asserts: a lower (gt|ge) AND an upper (lt|le) bound in one entry
# --------------------------------------------------------------------------- #
def test_load_two_sided_window_ok(tmp_path):
    doc = _base_doc()
    doc["assert"] = [
        {"name": "rail", "meas": "MAX v(rail)", "ge": "3.0", "le": "3.6"},
    ]
    spec = sa.load(_write(tmp_path, doc))
    a = spec.asserts[0]
    assert a["ge"] == pytest.approx(3.0)
    assert a["le"] == pytest.approx(3.6)


def test_load_two_sided_gt_lt_ok(tmp_path):
    doc = _base_doc()
    doc["assert"] = [{"name": "w", "meas": "MAX v(x)", "gt": "1", "lt": "5"}]
    spec = sa.load(_write(tmp_path, doc))
    assert spec.asserts[0]["gt"] == pytest.approx(1.0)
    assert spec.asserts[0]["lt"] == pytest.approx(5.0)


def test_evaluate_two_sided_pass():
    spec = sa.SimSpec(asserts=[{"name": "rail", "meas": "m", "ge": 3.0, "le": 3.6}])
    findings, _ = sa.evaluate(spec, {"rail": 3.3})
    assert findings == []


def test_evaluate_two_sided_below_lower_names_lower_side():
    spec = sa.SimSpec(asserts=[{"name": "rail", "meas": "m", "ge": 3.0, "le": 3.6}])
    findings, _ = sa.evaluate(spec, {"rail": 2.5})
    assert len(findings) == 1
    assert findings[0].code == "SIM_ASSERT_FAIL"
    assert ">= 3" in findings[0].message
    assert "rail = 2.5" in findings[0].message


def test_evaluate_two_sided_above_upper_names_upper_side():
    spec = sa.SimSpec(asserts=[{"name": "rail", "meas": "m", "ge": 3.0, "le": 3.6}])
    findings, _ = sa.evaluate(spec, {"rail": 4.0})
    assert len(findings) == 1
    assert findings[0].code == "SIM_ASSERT_FAIL"
    assert "<= 3.6" in findings[0].message


# --------------------------------------------------------------------------- #
# options.rshunt validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rv", [False, True, "auto", "1e12", 1e12, 1000000000000])
def test_load_rshunt_accepts_bool_number_string(tmp_path, rv):
    doc = _base_doc(options={"rshunt": rv})
    spec = sa.load(_write(tmp_path, doc))
    assert spec.options["rshunt"] == rv


@pytest.mark.parametrize("rv", [[1], {"a": 1}, None])
def test_load_rshunt_rejects_other_types(tmp_path, rv):
    doc = _base_doc(options={"rshunt": rv})
    with pytest.raises(AkcliError) as ei:
        sa.load(_write(tmp_path, doc))
    assert ei.value.code == "BAD_CONFIG"
    assert "rshunt" in str(ei.value).lower()


def test_load_protocol_mismatch_future_version(tmp_path):
    doc = _base_doc(protocol_version=2)
    with pytest.raises(AkcliError) as ei:
        sa.load(_write(tmp_path, doc))
    assert ei.value.code == "PROTOCOL_MISMATCH"


@pytest.mark.parametrize(
    "assert_entry,match",
    [
        ({"meas": "MAX v(x)", "gt": 1}, "'name'"),
        ({"name": "a", "gt": 1}, "meas' or 'when'"),
        ({"name": "a", "meas": "MAX v(x)", "when": "v(x)=1", "gt": 1}, "meas' or 'when'"),
        ({"name": "a", "meas": "MAX v(x)"}, "bound key"),
        ({"name": "a", "meas": "MAX v(x)", "gt": 1, "ge": 2}, "lower bound"),
        ({"name": "a", "meas": "MAX v(x)", "lt": 1, "le": 2}, "upper bound"),
        ({"name": "a", "meas": "MAX v(x)", "approx": 1, "lt": 2}, "cannot be combined"),
        ({"name": "a", "meas": "MAX v(x)", "gt": 1, "tol": 0.1}, "'tol'"),
        ({"name": "a", "meas": "MAX v(x)", "gt": 1, "bogus": 1}, "unknown key"),
        ({"name": "a", "meas": "MAX v(x)", "gt": "not-a-number"}, "cannot parse"),
        ({"name": "a", "meas": "MAX v(x)", "gt": 1, "analysis": "dc"}, "not in configured"),
    ],
)
def test_load_bad_config_assert_shapes(tmp_path, assert_entry, match):
    doc = _base_doc()
    doc["assert"] = [assert_entry]
    with pytest.raises(AkcliError) as ei:
        sa.load(_write(tmp_path, doc))
    assert ei.value.code == "BAD_CONFIG"
    assert match.lower() in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# item 7: stimulus names required, identifier-shaped, unique
# --------------------------------------------------------------------------- #
def test_load_stimulus_requires_name(tmp_path):
    doc = _base_doc(stimuli=[{"kind": "vsource", "node": "in", "value": "5"}])
    with pytest.raises(AkcliError) as ei:
        sa.load(_write(tmp_path, doc))
    assert ei.value.code == "BAD_CONFIG"
    assert "name" in str(ei.value).lower()


def test_load_stimulus_name_must_be_identifier(tmp_path):
    # leading digit and embedded space are both rejected (would corrupt the
    # SPICE element line).
    for bad in ("3V3", "v in", "V-1"):
        doc = _base_doc(stimuli=[{"name": bad, "kind": "vsource", "node": "in"}])
        with pytest.raises(AkcliError) as ei:
            sa.load(_write(tmp_path, doc))
        assert ei.value.code == "BAD_CONFIG"


def test_load_stimulus_names_must_be_unique(tmp_path):
    doc = _base_doc(stimuli=[
        {"name": "V1", "kind": "vsource", "node": "a"},
        {"name": "V1", "kind": "vsource", "node": "b"},
    ])
    with pytest.raises(AkcliError) as ei:
        sa.load(_write(tmp_path, doc))
    assert ei.value.code == "BAD_CONFIG"
    assert "duplicate" in str(ei.value).lower()


def test_load_stimulus_valid_name_accepted(tmp_path):
    doc = _base_doc(stimuli=[{"name": "Vsup", "kind": "vsource", "node": "in"}])
    spec = sa.load(_write(tmp_path, doc))
    assert spec.stimuli[0]["name"] == "Vsup"


def test_load_duplicate_assert_names_rejected(tmp_path):
    doc = _base_doc()
    doc["assert"] = [
        {"name": "a", "meas": "MAX v(x)", "gt": 1},
        {"name": "a", "meas": "MAX v(y)", "lt": 2},
    ]
    with pytest.raises(AkcliError) as ei:
        sa.load(_write(tmp_path, doc))
    assert ei.value.code == "BAD_CONFIG"
    assert "duplicate" in str(ei.value).lower()


def test_load_not_json_object_root(tmp_path):
    p = tmp_path / "sim.json"
    p.write_text("[1, 2, 3]")
    with pytest.raises(AkcliError) as ei:
        sa.load(str(p))
    assert ei.value.code == "BAD_CONFIG"


def test_load_invalid_json_syntax(tmp_path):
    p = tmp_path / "sim.json"
    p.write_text("{not json")
    with pytest.raises(AkcliError) as ei:
        sa.load(str(p))
    assert ei.value.code == "BAD_CONFIG"


def test_load_missing_file_propagates():
    with pytest.raises(FileNotFoundError):
        sa.load("/no/such/sim.json")


# --------------------------------------------------------------------------- #
# meas_statements(): analysis inference
# --------------------------------------------------------------------------- #
def test_meas_statements_deterministic_order():
    doc = _base_doc()
    doc["assert"] = [
        {"name": "b", "meas": "MAX v(peak) from=1m to=2m", "gt": 0.1},
        {"name": "a", "when": "v(peak)=0.297 RISE=1", "lt": 0.1},
    ]
    spec = sa.SimSpec(analyses=doc["analyses"], asserts=doc["assert"])
    stmts = sa.meas_statements(spec)
    assert stmts == [
        "meas tran b MAX v(peak) from=1m to=2m",
        "meas tran a WHEN v(peak)=0.297 RISE=1",
    ]


def test_meas_statements_when_style_defaults_tran():
    spec = sa.SimSpec(
        analyses={"tran": "5u 100m"},
        asserts=[{"name": "t_detect", "when": "v(peak)=0.297 RISE=1", "lt": 0.1}],
    )
    assert sa.meas_statements(spec) == ["meas tran t_detect WHEN v(peak)=0.297 RISE=1"]


def test_meas_statements_from_to_defaults_tran():
    spec = sa.SimSpec(
        analyses={"tran": "5u 100m", "ac": "dec 40 10 100k"},
        asserts=[{"name": "ripple", "meas": "PP v(peak) from=40m to=50m", "lt": 0.2}],
    )
    assert sa.meas_statements(spec) == ["meas tran ripple PP v(peak) from=40m to=50m"]


def test_meas_statements_find_at_defaults_ac_when_configured():
    spec = sa.SimSpec(
        analyses={"tran": "5u 100m", "ac": "dec 40 10 100k"},
        asserts=[{"name": "g10", "meas": "FIND vm(hp) AT=10", "gt": 50}],
    )
    assert sa.meas_statements(spec) == ["meas ac g10 FIND vm(hp) AT=10"]


def test_meas_statements_explicit_analysis_overrides():
    spec = sa.SimSpec(
        analyses={"tran": "5u 100m", "ac": "dec 40 10 100k"},
        asserts=[{"name": "x", "meas": "MAX v(a)", "gt": 1, "analysis": "ac"}],
    )
    assert sa.meas_statements(spec) == ["meas ac x MAX v(a)"]


# --------------------------------------------------------------------------- #
# run_commands(): per-analysis interactive dispatch (multi-analysis fix)
# --------------------------------------------------------------------------- #
def test_run_commands_single_analysis_runs_then_measures():
    spec = sa.SimSpec(
        analyses={"tran": "10u 2m"},
        asserts=[{"name": "vpk", "meas": "MAX v(peak) from=0 to=2m", "gt": 0.1}],
    )
    assert sa.run_commands(spec) == [
        "tran 10u 2m",
        "meas tran vpk MAX v(peak) from=0 to=2m",
    ]


def test_run_commands_multi_analysis_dispatches_each_with_its_meas():
    # tran + ac, one assert each: each analysis is issued explicitly and
    # immediately followed by *its own* meas so it reads its own plot — never a
    # single 'run' that leaves the ac meas with no plot (the misdiagnosed fail).
    spec = sa.SimSpec(
        analyses={"tran": "10u 2m", "ac": "dec 5 10 100k"},
        asserts=[
            {"name": "vpk", "meas": "MAX v(peak) from=0 to=2m", "gt": 0.1},
            {"name": "gmax", "meas": "FIND vm(out) AT=1k", "analysis": "ac",
             "gt": 0.0},
        ],
    )
    assert sa.run_commands(spec) == [
        "tran 10u 2m",
        "meas tran vpk MAX v(peak) from=0 to=2m",
        "ac dec 5 10 100k",
        "meas ac gmax FIND vm(out) AT=1k",
    ]


def test_run_commands_analysis_with_no_asserts_still_runs():
    # A waveform-only analysis (no asserts) is still issued so wrdata can capture.
    spec = sa.SimSpec(analyses={"tran": "1u 1m"}, asserts=[])
    assert sa.run_commands(spec) == ["tran 1u 1m"]


def test_run_commands_op_and_prefixed_params_normalize():
    spec = sa.SimSpec(analyses={"op": "", "ac": ".ac dec 5 10 100k"}, asserts=[])
    assert sa.run_commands(spec) == ["op", "ac dec 5 10 100k"]


def test_run_commands_no_analyses_falls_back_to_run():
    assert sa.run_commands(sa.SimSpec(analyses={}, asserts=[])) == ["run"]


# --------------------------------------------------------------------------- #
# parse_meas_output(): VERBATIM lines from a live libngspice 45.2 session
# (captured via demod.py at
#  /private/tmp/.../scratchpad/sim/demod.py -- see task spec).
# --------------------------------------------------------------------------- #
_LIVE_TRAN_LINES = [
    "stdout Doing analysis at TEMP = 27.000000 and TNOM = 27.000000",
    "stdout vpeak_max           =  4.912189e-01 at=  5.600927e-02",
    "stdout vpeak_idle          =  3.872626e-02 at=  1.688021e-02",
    "stdout t_detect            =  2.233761e-02",
    "stdout t_release           =  6.235740e-02",
    "stdout ripple              =  1.572552e-01 from=  4.000000e-02 to=  5.000000e-02",
    "stdout hp_max              =  6.432281e-01 at=  3.636469e-02",
]

_LIVE_AC_LINES = [
    "stdout Doing analysis at TEMP = 27.000000 and TNOM = 27.000000",
    "stdout g10                 =  9.716496e+01",
    "stdout g100                =  8.684300e+02",
    "stdout g1994               =  1.925713e+03",
]

_LIVE_FAILED_LINE = "stdout meas tran nope WHEN v(a)=99 RISE=1 failed!"


def test_parse_meas_output_live_tran_lines():
    result = sa.parse_meas_output(_LIVE_TRAN_LINES)
    assert result["vpeak_max"] == pytest.approx(4.912189e-01)
    assert result["vpeak_idle"] == pytest.approx(3.872626e-02)
    assert result["t_detect"] == pytest.approx(2.233761e-02)
    assert result["t_release"] == pytest.approx(6.235740e-02)
    assert result["ripple"] == pytest.approx(1.572552e-01)
    assert result["hp_max"] == pytest.approx(6.432281e-01)
    assert "Doing" not in result


def test_parse_meas_output_live_ac_lines():
    result = sa.parse_meas_output(_LIVE_AC_LINES)
    assert result["g10"] == pytest.approx(9.716496e01)
    assert result["g100"] == pytest.approx(8.684300e02)
    assert result["g1994"] == pytest.approx(1.925713e03)


def test_parse_meas_output_live_failed_line():
    result = sa.parse_meas_output(_LIVE_TRAN_LINES + [_LIVE_FAILED_LINE])
    assert result["nope"] is None
    assert result["vpeak_max"] == pytest.approx(4.912189e-01)


def test_parse_meas_output_ignores_unrelated_lines():
    lines = ["Note: ...", "", "stdout Circuit: * some title"]
    assert sa.parse_meas_output(lines) == {}


# --------------------------------------------------------------------------- #
# evaluate(): pass / fail / approx / failed measurement
# --------------------------------------------------------------------------- #
def test_evaluate_pass_no_findings():
    spec = sa.SimSpec(asserts=[{"name": "vpeak_max", "meas": "MAX v(peak)", "gt": 0.35}])
    findings, measured = sa.evaluate(spec, {"vpeak_max": 0.49})
    assert findings == []
    assert measured == {"vpeak_max": 0.49}


def test_evaluate_gt_violation():
    spec = sa.SimSpec(asserts=[{"name": "vpeak", "meas": "MAX v(peak)", "gt": 0.35}])
    findings, measured = sa.evaluate(spec, {"vpeak": 0.212})
    assert len(findings) == 1
    f = findings[0]
    assert f.code == "SIM_ASSERT_FAIL"
    assert f.severity.value == "error"
    assert "vpeak = 0.212" in f.message
    assert "> 0.35" in f.message
    assert measured == {"vpeak": 0.212}


@pytest.mark.parametrize(
    "bound_key,bound,value,ok",
    [
        ("gt", 1.0, 1.5, True),
        ("gt", 1.0, 1.0, False),
        ("lt", 1.0, 0.5, True),
        ("lt", 1.0, 1.0, False),
        ("ge", 1.0, 1.0, True),
        ("le", 1.0, 1.0, True),
    ],
)
def test_evaluate_bound_kinds(bound_key, bound, value, ok):
    spec = sa.SimSpec(asserts=[{"name": "x", "meas": "m", bound_key: bound}])
    findings, _ = sa.evaluate(spec, {"x": value})
    assert (findings == []) is ok


def test_evaluate_approx_within_default_tol_passes():
    spec = sa.SimSpec(asserts=[{"name": "x", "meas": "m", "approx": 0.30}])
    findings, _ = sa.evaluate(spec, {"x": 0.31})  # within 5%
    assert findings == []


def test_evaluate_approx_outside_default_tol_fails():
    spec = sa.SimSpec(asserts=[{"name": "x", "meas": "m", "approx": 0.30}])
    findings, _ = sa.evaluate(spec, {"x": 0.40})
    assert len(findings) == 1
    assert findings[0].code == "SIM_ASSERT_FAIL"


def test_evaluate_approx_custom_tol():
    spec = sa.SimSpec(asserts=[{"name": "x", "meas": "m", "approx": 0.30, "tol": 0.5}])
    findings, _ = sa.evaluate(spec, {"x": 0.40})  # within 50%
    assert findings == []


def test_evaluate_failed_measurement():
    spec = sa.SimSpec(asserts=[{"name": "nope", "meas": "m", "gt": 1}])
    findings, measured = sa.evaluate(spec, {"nope": None})
    assert len(findings) == 1
    assert findings[0].code == "SIM_MEAS_FAILED"
    assert findings[0].severity.value == "error"
    assert measured == {"nope": None}


def test_evaluate_missing_measurement_treated_as_failed():
    spec = sa.SimSpec(asserts=[{"name": "absent", "meas": "m", "gt": 1}])
    findings, measured = sa.evaluate(spec, {})
    assert findings[0].code == "SIM_MEAS_FAILED"
    assert measured == {"absent": None}


def test_evaluate_multiple_asserts_report_order():
    spec = sa.SimSpec(asserts=[
        {"name": "a", "meas": "m", "gt": 1},
        {"name": "b", "meas": "m", "lt": 1},
    ])
    findings, measured = sa.evaluate(spec, {"a": 2, "b": 0.5})
    assert findings == []
    assert list(measured.keys()) == ["a", "b"]


# --------------------------------------------------------------------------- #
# end-to-end: live demod.py transient measurements against a sim spec
# --------------------------------------------------------------------------- #
def test_end_to_end_load_meas_parse_evaluate(tmp_path):
    doc = _base_doc()
    doc["assert"] = [
        {"name": "vpeak_max", "meas": "MAX v(peak) from=20m to=60m", "gt": "0.35"},
        {"name": "vpeak_idle", "meas": "MAX v(peak) from=0 to=19m", "lt": "0.1"},
        {"name": "t_detect", "when": "v(peak)=0.297 RISE=1", "lt": "25m"},
    ]
    spec = sa.load(_write(tmp_path, doc))
    stmts = sa.meas_statements(spec)
    assert stmts == [
        "meas tran vpeak_max MAX v(peak) from=20m to=60m",
        "meas tran vpeak_idle MAX v(peak) from=0 to=19m",
        "meas tran t_detect WHEN v(peak)=0.297 RISE=1",
    ]
    results = sa.parse_meas_output(_LIVE_TRAN_LINES)
    findings, measured = sa.evaluate(spec, results)
    assert findings == []
    assert measured["vpeak_max"] == pytest.approx(4.912189e-01)
    assert measured["t_detect"] == pytest.approx(2.233761e-02)


# --------------------------------------------------------------------------- #
# schema mirror identity (root schemas/ is canonical; src/.../schemas/ ships
# in wheels -- see tests/test_schema_exports.py for the pattern).
# --------------------------------------------------------------------------- #
def test_sim_schema_packaged_mirror_identical():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    a = (root / "schemas" / "sim.schema.json").read_text()
    b = (root / "src" / "akcli" / "schemas" / "sim.schema.json").read_text()
    assert a == b


def test_sim_schema_is_valid_draft202012():
    jsonschema = pytest.importorskip("jsonschema")
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas" / "sim.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)


def test_sim_schema_validates_load_golden_docs(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas" / "sim.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    for doc in (_base_doc(),
                _base_doc(stimuli=[{"name": "Vin", "a": 1}], models={"M": ".model"})):
        errors = list(validator.iter_errors(doc))
        assert errors == [], f"{doc} failed schema: {errors}"


def test_sim_schema_two_sided_and_rshunt(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas" / "sim.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)

    def ok(assert_entry, **opts):
        doc = _base_doc(**opts)
        doc["assert"] = [assert_entry]
        return not list(validator.iter_errors(doc))

    # two-sided window is accepted; approx alone is accepted
    assert ok({"name": "w", "meas": "MAX v(x)", "ge": "3", "le": "3.6"})
    assert ok({"name": "w", "meas": "MAX v(x)", "gt": 1, "lt": 5})
    assert ok({"name": "w", "meas": "MAX v(x)", "approx": 0.3, "tol": 0.1})
    # no bound / two lowers / two uppers / approx+bound / tol-without-approx fail
    assert not ok({"name": "w", "meas": "MAX v(x)"})
    assert not ok({"name": "w", "meas": "MAX v(x)", "gt": 1, "ge": 2})
    assert not ok({"name": "w", "meas": "MAX v(x)", "lt": 1, "le": 2})
    assert not ok({"name": "w", "meas": "MAX v(x)", "approx": 1, "lt": 2})
    assert not ok({"name": "w", "meas": "MAX v(x)", "gt": 1, "tol": 0.1})
    # options.rshunt: bool / number / string accepted, array rejected
    assert ok({"name": "w", "meas": "MAX v(x)", "gt": 1}, options={"rshunt": False})
    assert ok({"name": "w", "meas": "MAX v(x)", "gt": 1}, options={"rshunt": "auto"})
    assert ok({"name": "w", "meas": "MAX v(x)", "gt": 1}, options={"rshunt": 1e12})
    assert not ok({"name": "w", "meas": "MAX v(x)", "gt": 1}, options={"rshunt": [1]})


def test_sim_schema_rejects_nameless_and_bad_stimulus_names(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas" / "sim.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    # missing 'name' and a leading-digit name both fail schema validation
    assert list(validator.iter_errors(_base_doc(stimuli=[{"kind": "vsource"}])))
    assert list(validator.iter_errors(_base_doc(stimuli=[{"name": "3V3"}])))
    assert not list(validator.iter_errors(_base_doc(stimuli=[{"name": "Vsup"}])))
