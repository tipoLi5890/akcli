"""Design-contract engine (`akcli check --contract contract.toml`).

Exercises every contract rule kind against a synthetic board: a regulator
feedback node (require/forbid pin-net), a connector flip pair (require/forbid
same-net), a fixed resistor value, an NC pin, and an approved exception with
owner/expiry. Every rule kind is broken deliberately and must fail on the
exact pin/net.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from akcli.checks import contract
from akcli.cli import main
from akcli.errors import AkcliError
from akcli.readers import kicad

_SCH = """(kicad_sch (version 20230121) (generator eeschema)
  (uuid 66666666-6666-6666-6666-666666666666)
  (paper "A4")
  (lib_symbols
    (symbol "X:REG" (in_bom yes) (on_board yes)
      (property "Reference" "U" (at 0 0 0))
      (property "Value" "REG" (at 0 0 0))
      (symbol "REG_1_1"
        (pin passive line (at 0 -2.54 90) (length 1.27)
          (name "FB1" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -5.08 90) (length 1.27)
          (name "FB2" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -7.62 90) (length 1.27)
          (name "GPIO3" (effects (font (size 1.27 1.27))))
          (number "3" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "X:CONN" (in_bom yes) (on_board yes)
      (property "Reference" "J" (at 0 0 0))
      (property "Value" "CONN" (at 0 0 0))
      (symbol "CONN_1_1"
        (pin passive line (at 0 -2.54 90) (length 1.27)
          (name "A6" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -5.08 90) (length 1.27)
          (name "B6" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -7.62 90) (length 1.27)
          (name "A2" (effects (font (size 1.27 1.27))))
          (number "3" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "X:R" (in_bom yes) (on_board yes)
      (property "Reference" "R" (at 0 0 0))
      (property "Value" "R" (at 0 0 0))
      (symbol "R_1_1"
        (pin passive line (at 0 -2.54 90) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "X:REG") (at 100 100 0) (unit 1)
    (uuid 77777777-0000-0000-0000-000000000001)
    (property "Reference" "U1" (at 100 95 0))
    (property "Value" "REG" (at 100 105 0))
    (pin "1" (uuid 77770000-0000-0000-0000-000000000001))
    (pin "2" (uuid 77770000-0000-0000-0000-000000000002))
    (pin "3" (uuid 77770000-0000-0000-0000-000000000003)))
  (symbol (lib_id "X:CONN") (at 150 100 0) (unit 1)
    (uuid 77777777-0000-0000-0000-000000000002)
    (property "Reference" "J1" (at 150 95 0))
    (property "Value" "CONN" (at 150 105 0))
    (pin "1" (uuid 77770000-0000-0000-0000-000000000011))
    (pin "2" (uuid 77770000-0000-0000-0000-000000000012))
    (pin "3" (uuid 77770000-0000-0000-0000-000000000013)))
  (symbol (lib_id "X:R") (at 200 100 0) (unit 1)
    (uuid 77777777-0000-0000-0000-000000000003)
    (property "Reference" "R7" (at 200 95 0))
    (property "Value" "150k" (at 200 105 0))
    (pin "1" (uuid 77770000-0000-0000-0000-000000000021)))
  (label "VFB2" (at 100 105.08 0))
  (wire (pts (xy 100 105.08) (xy 200 105.08)))
  (wire (pts (xy 200 105.08) (xy 200 102.54)))
  (label "GND" (at 100 102.54 0))
  (label "PAIR0" (at 150 102.54 0))
  (wire (pts (xy 150 102.54) (xy 150 105.08)))
)"""

_CONTRACT = """
protocol_version = 1

[[contract]]
id = "reg-feedback-node"
evidence = ["ACME-REG datasheet, Table 7"]
require = [ { pin = "U1.FB2", net = "VFB2" } ]
forbid  = [ { pin = "U1.FB1", net = "VFB2" } ]

[[contract]]
id = "usb-c-flip-pair"
require_same_net = [ ["J1.A6", "J1.B6"] ]
forbid_same_net  = [ ["J1.A2", "J1.B6"] ]

[[contract]]
id = "feedback-value"
component = "R7"
value = "150k"

[[contract]]
id = "gpio3-unused"
nc = ["U1.GPIO3"]

[[contract]]
id = "thermal-via"
waived = true
reason = "exposed-pad thermal vias approved"
owner = "hw-lead"
expires = "2099-01-01"
"""


@pytest.fixture()
def sch(tmp_path):
    p = tmp_path / "c.kicad_sch"
    p.write_text(_SCH)
    return kicad.read_sch(str(p))


def _doc(text: str, tmp_path: Path) -> dict:
    p = tmp_path / "contract.toml"
    p.write_text(text)
    return contract.load(p)


def _by_code(findings):
    out = {}
    for f in findings:
        out.setdefault(f.code, []).append(f)
    return out


def test_all_rules_pass_and_exception_is_distinct(sch, tmp_path):
    findings = contract.run(sch, _doc(_CONTRACT, tmp_path))
    by = _by_code(findings)
    assert len(by.get("CONTRACT_PASS", [])) == 4
    assert len(by.get("CONTRACT_WAIVED", [])) == 1
    assert "CONTRACT_FAIL" not in by
    assert "hw-lead" in by["CONTRACT_WAIVED"][0].message


def test_broken_require_names_the_pin_and_net(sch, tmp_path):
    text = _CONTRACT.replace('pin = "U1.FB2", net = "VFB2"',
                             'pin = "U1.FB1", net = "VFB2"')
    text = text.replace('forbid  = [ { pin = "U1.FB1", net = "VFB2" } ]\n', "")
    findings = contract.run(sch, _doc(text, tmp_path))
    fails = _by_code(findings).get("CONTRACT_FAIL", [])
    assert len(fails) == 1
    assert "U1.FB1" in fails[0].message and "VFB2" in fails[0].message
    assert "Table 7" in fails[0].message              # evidence carried through


def test_broken_forbid_fires(sch, tmp_path):
    text = _CONTRACT.replace('forbid  = [ { pin = "U1.FB1", net = "VFB2" } ]',
                             'forbid  = [ { pin = "U1.FB2", net = "VFB2" } ]')
    fails = _by_code(contract.run(sch, _doc(text, tmp_path))).get("CONTRACT_FAIL", [])
    assert any("forbidden net" in f.message for f in fails)


def test_broken_same_net_pair_fires(sch, tmp_path):
    text = _CONTRACT.replace('require_same_net = [ ["J1.A6", "J1.B6"] ]',
                             'require_same_net = [ ["J1.A6", "J1.A2"] ]')
    fails = _by_code(contract.run(sch, _doc(text, tmp_path))).get("CONTRACT_FAIL", [])
    assert any("J1.A6" in f.message and "J1.A2" in f.message for f in fails)


def test_forbid_same_net_fires_on_short(sch, tmp_path):
    text = _CONTRACT.replace('forbid_same_net  = [ ["J1.A2", "J1.B6"] ]',
                             'forbid_same_net  = [ ["J1.A6", "J1.B6"] ]')
    fails = _by_code(contract.run(sch, _doc(text, tmp_path))).get("CONTRACT_FAIL", [])
    assert any("must NOT share" in f.message for f in fails)


def test_wrong_value_fires(sch, tmp_path):
    text = _CONTRACT.replace('value = "150k"', 'value = "56k"')
    fails = _by_code(contract.run(sch, _doc(text, tmp_path))).get("CONTRACT_FAIL", [])
    assert any("R7" in f.message and "56k" in f.message for f in fails)


def test_value_list_and_normalization(sch, tmp_path):
    text = _CONTRACT.replace('value = "150k"', 'value = ["150 K", "154k"]')
    findings = contract.run(sch, _doc(text, tmp_path))
    assert not _by_code(findings).get("CONTRACT_FAIL")


def test_nc_violation_fires(sch, tmp_path):
    text = _CONTRACT.replace('nc = ["U1.GPIO3"]', 'nc = ["U1.FB2"]')
    fails = _by_code(contract.run(sch, _doc(text, tmp_path))).get("CONTRACT_FAIL", [])
    assert any("U1.FB2" in f.message and "unconnected" in f.message for f in fails)


def test_unknown_pin_is_a_failure_not_a_pass(sch, tmp_path):
    text = _CONTRACT.replace('pin = "U1.FB2"', 'pin = "U1.NOPE"')
    fails = _by_code(contract.run(sch, _doc(text, tmp_path))).get("CONTRACT_FAIL", [])
    assert any("U1.NOPE" in f.message and "not found" in f.message for f in fails)


def test_expired_exception_warns_instead_of_passing(sch, tmp_path):
    text = _CONTRACT.replace('expires = "2099-01-01"', 'expires = "2020-01-01"')
    findings = contract.run(sch, _doc(text, tmp_path))
    by = _by_code(findings)
    assert "CONTRACT_WAIVED" not in by
    assert len(by.get("CONTRACT_EXCEPTION_EXPIRED", [])) == 1


def test_load_rejects_duplicate_ids(tmp_path):
    bad = "[[contract]]\nid = \"a\"\n[[contract]]\nid = \"a\"\n"
    (tmp_path / "c.toml").write_text(bad)
    with pytest.raises(AkcliError) as ei:
        contract.load(tmp_path / "c.toml")
    assert ei.value.code == "BAD_CONFIG"


def test_load_rejects_future_protocol(tmp_path):
    (tmp_path / "c.toml").write_text("protocol_version = 99\n[[contract]]\nid = \"a\"\n")
    with pytest.raises(AkcliError) as ei:
        contract.load(tmp_path / "c.toml")
    assert ei.value.code == "PROTOCOL_MISMATCH"


def test_cli_check_contract(sch, tmp_path, capsys):
    sch_path = tmp_path / "c.kicad_sch"          # already written by fixture
    (tmp_path / "contract.toml").write_text(_CONTRACT)
    code = main(["check", str(sch_path), "--contract",
                 str(tmp_path / "contract.toml"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    codes = {f["code"] for f in doc["findings"]}
    assert code == 0                              # passes + a NOTE-level waiver
    assert "CONTRACT_PASS" in codes and "CONTRACT_WAIVED" in codes

    broken = _CONTRACT.replace('pin = "U1.FB2", net = "VFB2"',
                               'pin = "U1.FB1", net = "VFB2"')
    (tmp_path / "contract.toml").write_text(broken)
    code = main(["check", str(sch_path), "--contract",
                 str(tmp_path / "contract.toml")])
    assert code == 1


# --------------------------------------------------------------------------- #
# value ranges: { nominal, tolerance } / { min, max }
# --------------------------------------------------------------------------- #
_EXAMPLE = (Path(__file__).resolve().parents[1]
            / "examples" / "contract" / "example.contract.toml")


def _write(tmp_path: Path, text: str) -> Path:
    # utf-8 pinned: these contracts carry "±" / "Ω" and load() reads utf-8.
    p = tmp_path / "contract.toml"
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def _value_rule(value: str) -> str:
    return ('[[contract]]\nid = "r7-window"\ncomponent = "R7"\n'
            f"value = {value}\n")


def _sch_valued(tmp_path: Path, value: str):
    """The fixture board with R7's Value field replaced."""
    assert _SCH.count('(property "Value" "150k"') == 1
    p = tmp_path / "valued.kicad_sch"
    p.write_text(_SCH.replace('(property "Value" "150k"',
                              f'(property "Value" "{value}"'),
                 encoding="utf-8", newline="\n")
    return kicad.read_sch(str(p))


def _eval_value(tmp_path: Path, actual: str, value: str):
    doc = contract.load(_write(tmp_path, _value_rule(value)))
    return _by_code(contract.run(_sch_valued(tmp_path, actual), doc))


def _load_error(tmp_path: Path, text: str) -> AkcliError:
    with pytest.raises(AkcliError) as ei:
        contract.load(_write(tmp_path, text))
    return ei.value


@pytest.mark.parametrize("actual, ok", [
    ("150k", True),
    ("151.5k", True),       # exactly ON the +1 % edge
    ("148.5k", True),       # exactly ON the -1 % edge
    ("148k5", True),        # the same edge, IEC infix spelling
    ("151.6k", False),
    ("148.4k", False),
    ("15k", False),
])
def test_range_nominal_tolerance_is_inclusive_at_the_edges(tmp_path, actual, ok):
    by = _eval_value(tmp_path, actual, '{ nominal = "150k", tolerance = "1%" }')
    assert ("CONTRACT_PASS" in by) is ok
    assert ("CONTRACT_FAIL" in by) is (not ok)


@pytest.mark.parametrize("nominal, tolerance, actual, ok", [
    # edges where a bare float compare lands on the wrong side
    # (1.02e-12 - 1e-12 > 0.02 * 1e-12 in IEEE-754)
    ("1p", "2%", "1.02p", True),
    ("1p", "2%", "0.98p", True),
    ("1p", "10%", "1.1p", True),
    ("1n", "5%", "0.95n", True),
    ("1p", "2%", "1.021p", False),      # the slack is not a free pass
    ("1p", "2%", "0.979p", False),
    ("150k", "0%", "150k", True),       # 0 % = numeric equality
    ("150k", "0%", "150.1k", False),
    ("150k", "±0.5 %", "150.75k", True),
])
def test_range_tolerance_edge_survives_float_noise(
        tmp_path, nominal, tolerance, actual, ok):
    by = _eval_value(
        tmp_path, actual,
        f'{{ nominal = "{nominal}", tolerance = "{tolerance}" }}')
    assert ("CONTRACT_PASS" in by) is ok
    assert ("CONTRACT_FAIL" in by) is (not ok)


@pytest.mark.parametrize("value, actual, ok", [
    ('{ min = "100n", max = "1u" }', "100n", True),     # ON the lower bound
    ('{ min = "100n", max = "1u" }', "1u", True),       # ON the upper bound
    ('{ min = "100n", max = "1u" }', "470n", True),
    ('{ min = "100n", max = "1u" }', "99n", False),
    ('{ min = "100n", max = "1u" }', "1.1u", False),
    ('{ min = "100k" }', "100k", True),                 # min-only
    ('{ min = "100k" }', "10M", True),
    ('{ min = "100k" }', "99.9k", False),
    ('{ max = "150k" }', "150k", True),                 # max-only
    ('{ max = "150k" }', "1R", True),
    ('{ max = "150k" }', "150.1k", False),
    ('{ min = "150k", max = "150k" }', "150k", True),   # degenerate window
    ('{ min = 1000, max = 2200.5 }', "2k2", True),      # TOML numbers as bounds
    ('{ min = 1000, max = 2200.5 }', "2k21", False),
])
def test_range_min_max_bounds_are_inclusive_and_optional(
        tmp_path, value, actual, ok):
    by = _eval_value(tmp_path, actual, value)
    assert ("CONTRACT_PASS" in by) is ok
    assert ("CONTRACT_FAIL" in by) is (not ok)


@pytest.mark.parametrize("actual", [
    "150k", "150K", "150 k", "150k0", "0.15M", "150000", "1.5e5",
    "150k\u03a9",           # Greek capital omega
    "150k\u2126",           # ohm sign
    "150kohm", "150 kOhms",
])
def test_range_accepts_engineering_notation_with_a_unit_tail(tmp_path, actual):
    by = _eval_value(tmp_path, actual, '{ nominal = "150k", tolerance = "1%" }')
    assert "CONTRACT_PASS" in by and "CONTRACT_FAIL" not in by


@pytest.mark.parametrize("actual", [
    "470n", "470nF", "470 nF", "0.47uF", "0.47UF", "0.47\u00b5F", "0.47\u03bcF",
    "470NF",
])
def test_range_accepts_capacitor_spellings(tmp_path, actual):
    by = _eval_value(tmp_path, actual, '{ min = "100nF", max = "1uF" }')
    assert "CONTRACT_PASS" in by and "CONTRACT_FAIL" not in by


@pytest.mark.parametrize("actual", [
    "150k 1%", "150k/0603", "150K 0.1% 0402", "150k, 1%, 0.1W",
])
def test_range_reads_a_bom_style_value_by_its_leading_token(tmp_path, actual):
    by = _eval_value(tmp_path, actual, '{ nominal = "150k", tolerance = "1%" }')
    assert "CONTRACT_PASS" in by and "CONTRACT_FAIL" not in by


@pytest.mark.parametrize("actual", [
    "150k foo",     # an unrecognized trailing token is never guessed past
    "0603 150k",    # only the LEADING token is the value
    "DNP",
    "see BOM",
    "",
    "100f",         # femto in SPICE notation, not 100 F: refused, never guessed
])
def test_range_unparseable_value_fails_and_says_so(tmp_path, actual):
    by = _eval_value(tmp_path, actual, '{ nominal = "150k", tolerance = "1%" }')
    assert "CONTRACT_PASS" not in by
    [f] = by["CONTRACT_FAIL"]
    assert f.message.startswith("r7-window: R7: value ")
    assert "cannot be parsed as a number" in f.message
    assert "150k ±1%" in f.message            # the range it could not check
    if actual:
        assert repr(actual) in f.message


@pytest.mark.parametrize("value, actual, want", [
    ('{ nominal = "150k", tolerance = "1%" }', "160k", "150k ±1%"),
    ('{ min = "100n", max = "1u" }', "2.2u", "100n to 1u"),
    ('{ min = "100k" }', "10k", "at least 100k"),
    ('{ max = "150k" }', "1M", "at most 150k"),
])
def test_range_failure_names_the_part_the_value_and_the_window(
        tmp_path, value, actual, want):
    [f] = _eval_value(tmp_path, actual, value)["CONTRACT_FAIL"]
    assert f.message == f"r7-window: R7: value {actual!r}, expected {want}"


def test_range_rule_obeys_severity_and_missing_component(sch, tmp_path):
    text = (_value_rule('{ min = "1M" }') + 'severity = "warning"\n'
            + _value_rule('{ min = "1M" }').replace("r7-window", "ghost")
                                           .replace('"R7"', '"R99"'))
    by = _by_code(contract.run(sch, contract.load(_write(tmp_path, text))))
    fails = {f.message.split(":")[0]: f for f in by["CONTRACT_FAIL"]}
    assert fails["r7-window"].severity.name == "WARNING"
    assert "component 'R99' not found" in fails["ghost"].message


@pytest.mark.parametrize("value, needle", [
    ('{ nominal = "150k", tolerance = "1%", fudge = 1 }', "unknown key(s) fudge"),
    ('{ minimum = "100n" }', "unknown key(s) minimum"),
    ('{ min = "abc" }', "min 'abc' is not a value"),
    ('{ max = "" }', "max '' is not a value"),
    ('{ nominal = "150k 1%", tolerance = "1%" }', "nominal '150k 1%' is not a value"),
    ('{ min = true }', "min True is not a value"),
    ('{ max = inf }', "max inf is not a value"),
    ('{ min = "1u", max = "100n" }', "min '1u' is greater than max '100n'"),
    ('{ nominal = "150k", tolerance = "1" }', "is not a percentage"),
    ('{ nominal = "150k", tolerance = "0.01" }', "is not a percentage"),
    ('{ nominal = "150k", tolerance = 0.01 }', "is not a percentage"),
    ('{ nominal = "150k", tolerance = 1 }', "is not a percentage"),
    ('{ nominal = "150k", tolerance = "-1%" }', "is not a percentage"),
    ('{ nominal = "150k", tolerance = "one %" }', "is not a percentage"),
    ('{ nominal = "150k" }', "required together"),
    ('{ tolerance = "1%" }', "required together"),
    ('{ nominal = "150k", tolerance = "1%", max = "200k" }', "separate forms"),
    ("{}", "empty range"),
    ('[ "150k", { min = "100k" } ]', "cannot be a list element"),
])
def test_load_rejects_a_malformed_range_table(tmp_path, value, needle):
    err = _load_error(tmp_path, _value_rule(value))
    assert err.code == "BAD_CONFIG"
    assert "'r7-window'" in err.message and needle in err.message


def test_load_validates_the_range_of_a_waived_rule_too(tmp_path):
    text = (_value_rule('{ min = "1u", max = "100n" }')
            + 'waived = true\nowner = "hw-lead"\nreason = "pending respin"\n')
    assert _load_error(tmp_path, text).code == "BAD_CONFIG"


def test_unvalidated_malformed_range_fails_instead_of_passing(sch):
    """run() on a doc that never went through load() must not pass (or raise)."""
    doc = {"contract": [{"id": "x", "component": "R7",
                         "value": {"min": "1u", "max": "100n"}}]}
    by = _by_code(contract.run(sch, doc))
    assert "CONTRACT_PASS" not in by
    assert "invalid value range" in by["CONTRACT_FAIL"][0].message


# --------------------------------------------------------------------------- #
# structured evidence: { source, page, url } tables mixed with strings
# --------------------------------------------------------------------------- #
_EVIDENCE = """evidence = [
  "ACME-REG datasheet, Table 7",
  { source = "ACME-REG datasheet", page = 12, url = "https://example.com/reg.pdf" },
  { source = "layout guide", page = 3 },
  { source = "app note AN-1", url = "https://example.com/an1" },
  { source = "design review" },
]
"""
_EVIDENCE_SUFFIX = (
    "  [evidence: ACME-REG datasheet, Table 7; "
    "ACME-REG datasheet p.12 <https://example.com/reg.pdf>; "
    "layout guide p.3; app note AN-1 <https://example.com/an1>; "
    "design review]")


def test_structured_evidence_renders_in_file_order(sch, tmp_path):
    passing = _value_rule('"150k"') + _EVIDENCE
    [f] = contract.run(sch, contract.load(_write(tmp_path, passing)))
    assert f.code == "CONTRACT_PASS"
    assert f.message == "r7-window: holds" + _EVIDENCE_SUFFIX

    failing = _value_rule('"56k"') + _EVIDENCE
    [f] = contract.run(sch, contract.load(_write(tmp_path, failing)))
    assert f.code == "CONTRACT_FAIL"
    assert f.message.endswith(_EVIDENCE_SUFFIX)


def test_structured_evidence_reaches_waived_and_expired_findings(sch, tmp_path):
    text = ('[[contract]]\nid = "w"\nwaived = true\nowner = "hw-lead"\n'
            'reason = "approved"\nexpires = "2099-01-01"\n' + _EVIDENCE)
    doc = contract.load(_write(tmp_path, text))
    [f] = contract.run(sch, doc)
    assert f.code == "CONTRACT_WAIVED" and f.message.endswith(_EVIDENCE_SUFFIX)
    [f] = contract.run(sch, doc, today=dt.date(2100, 1, 1))
    assert f.code == "CONTRACT_EXCEPTION_EXPIRED"
    assert f.message.endswith(_EVIDENCE_SUFFIX)


def test_string_only_evidence_message_is_unchanged(sch, tmp_path):
    text = _value_rule('"150k"') + 'evidence = ["DS Table 7", "DS §4.2"]\n'
    [f] = contract.run(sch, contract.load(_write(tmp_path, text)))
    assert f.message == "r7-window: holds  [evidence: DS Table 7; DS §4.2]"


@pytest.mark.parametrize("evidence, needle", [
    ('[ { page = 12 } ]', "needs a non-empty source"),
    ('[ { source = "" } ]', "needs a non-empty source"),
    ('[ { source = "  " } ]', "needs a non-empty source"),
    ('[ { source = 7 } ]', "needs a non-empty source"),
    ('[ { source = "DS", pg = 12 } ]', "unknown key(s) pg"),
    ('[ { source = "DS", page = "12" } ]', "page '12' is not a positive integer"),
    ('[ { source = "DS", page = 0 } ]', "page 0 is not a positive integer"),
    ('[ { source = "DS", page = true } ]', "page True is not a positive integer"),
    ('[ { source = "DS", page = 1.5 } ]', "page 1.5 is not a positive integer"),
    ('[ { source = "DS", url = "" } ]', "url '' is not a non-empty string"),
    ('[ { source = "DS", url = 5 } ]', "url 5 is not a non-empty string"),
    ('[ "DS", 12 ]', "entry 12 is neither a string nor a table"),
    ('"ACME-REG datasheet"', "evidence must be a list"),
    ('{ source = "DS" }', "evidence must be a list"),
])
def test_load_rejects_malformed_evidence(tmp_path, evidence, needle):
    err = _load_error(tmp_path, _value_rule('"150k"') + f"evidence = {evidence}\n")
    assert err.code == "BAD_CONFIG"
    assert "'r7-window'" in err.message and needle in err.message


def test_cli_json_finding_shape_is_unchanged_by_structured_evidence(
        sch, tmp_path, capsys):
    """Evidence stays message text: no new per-finding JSON field."""
    sch_path = tmp_path / "c.kicad_sch"          # already written by fixture

    def _findings(text: str) -> list[dict]:
        code = main(["check", str(sch_path), "--contract",
                     str(_write(tmp_path, text)), "--json"])
        assert code == 0
        return [f for f in json.loads(capsys.readouterr().out)["findings"]
                if f["code"].startswith("CONTRACT_")]

    [plain] = _findings(_value_rule('"150k"') + 'evidence = ["DS Table 7"]\n')
    [rich] = _findings(_value_rule('{ nominal = "150k", tolerance = "1%" }')
                       + _EVIDENCE)
    assert set(rich) == set(plain) and "evidence" not in rich
    assert rich["code"] == "CONTRACT_PASS"
    assert rich["message"] == "r7-window: holds" + _EVIDENCE_SUFFIX


def test_cli_malformed_contract_is_a_usage_error(sch, tmp_path, capsys):
    sch_path = tmp_path / "c.kicad_sch"          # already written by fixture
    bad = _write(tmp_path, _value_rule('{ min = "1u", max = "100n" }'))
    code = main(["check", str(sch_path), "--contract", str(bad), "--json"])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "BAD_CONFIG"


# --------------------------------------------------------------------------- #
# waivers: an approved exception names its owner and its reason
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fields", [
    'reason = "approved"\n',                        # no owner
    'owner = "hw-lead"\n',                          # no reason
    "",                                             # neither
    'owner = ""\nreason = "approved"\n',            # empty owner
    'owner = "hw-lead"\nreason = "   "\n',          # blank reason
    'owner = 7\nreason = "approved"\n',             # not a string
])
def test_load_rejects_a_waiver_without_owner_or_reason(tmp_path, fields):
    err = _load_error(tmp_path, '[[contract]]\nid = "w"\nwaived = true\n' + fields)
    assert err.code == "BAD_CONFIG"
    assert "'w'" in err.message and "owner AND reason" in err.message


def test_unwaived_rule_needs_no_owner_or_reason(sch, tmp_path):
    text = '[[contract]]\nid = "plain"\nwaived = false\nnc = ["U1.GPIO3"]\n'
    [f] = contract.run(sch, contract.load(_write(tmp_path, text)))
    assert f.code == "CONTRACT_PASS"


# --------------------------------------------------------------------------- #
# the shipped example
# --------------------------------------------------------------------------- #
def test_shipped_example_loads_and_shows_every_form(sch):
    doc = contract.load(_EXAMPLE)
    rules = doc["contract"]
    assert any(isinstance(r.get("value"), dict) for r in rules)      # a range
    assert any(isinstance(e, dict)                                   # a table
               for r in rules for e in r.get("evidence", []))
    assert any(isinstance(e, str)
               for r in rules for e in r.get("evidence", []))
    assert any(r.get("waived") for r in rules)
    # It is written for a different board; it must still evaluate cleanly.
    findings = contract.run(sch, doc)
    assert findings and all(f.code.startswith("CONTRACT_") for f in findings)


@pytest.mark.parametrize("body", ['value = "150k"', 'component = "R7"'])
def test_half_a_value_rule_is_rejected_on_load(tmp_path, body):
    # Either key alone used to be skipped silently and reported as a PASS.
    p = tmp_path / "c.toml"
    p.write_text('protocol_version = 1\n[[contract]]\nid = "half"\n'
                 f'{body}\n', encoding="utf-8", newline="\n")
    with pytest.raises(AkcliError) as exc:
        contract.load(p)
    assert "needs both component and value" in str(exc.value)
