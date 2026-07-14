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
