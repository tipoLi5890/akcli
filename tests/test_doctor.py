"""Tests for ``akcli doctor`` — offline, discovery fully monkeypatched."""

from __future__ import annotations

import json

from akcli.cli import main
from akcli.commands import doctor as doc


def test_doctor_reports_and_exits_zero_by_default(capsys):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    for name in ("python", "akcli", "schemas", "kicad-cli", "ngspice", "config"):
        assert name in out
    assert "network" not in out              # offline by default


def test_doctor_json_shape(capsys, monkeypatch):
    monkeypatch.setattr(doc, "_find_kicad_cli", lambda: None)
    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True             # nothing required
    kc = payload["checks"]["kicad-cli"]
    assert kc["ok"] is False and "hint" in kc
    assert payload["checks"]["python"]["ok"] is True


def test_doctor_require_gates_exit(monkeypatch, capsys):
    monkeypatch.setattr(doc, "_find_kicad_cli", lambda: None)
    assert main(["doctor", "--require", "kicad-cli"]) == 1
    out = capsys.readouterr().out
    assert "missing: kicad-cli" in out
    # multiple/comma parsing + present capability passes
    monkeypatch.setattr(doc, "_find_kicad_cli", lambda: "/usr/bin/kicad-cli")
    assert main(["doctor", "--require", "python,kicad-cli"]) == 0


def test_doctor_require_unknown_is_usage_error(capsys):
    assert main(["doctor", "--require", "bogus"]) == 2
    assert "--require accepts" in capsys.readouterr().err


def test_doctor_network_is_optin_and_requirable(monkeypatch, capsys):
    calls = []

    def fake_net():
        calls.append(1)
        return (False, "http://x unreachable: nope", "hint text")

    monkeypatch.setattr(doc, "_check_network", fake_net)
    assert main(["doctor"]) == 0
    assert not calls                         # not probed without the flag
    capsys.readouterr()
    assert main(["doctor", "--require", "network"]) == 1
    assert calls                             # --require network implies probe


def test_doctor_ngspice_env_off(monkeypatch, capsys):
    monkeypatch.setenv("AKCLI_NGSPICE", "off")
    assert main(["doctor", "--require", "ngspice"]) == 1
    assert "missing: ngspice" in capsys.readouterr().out
