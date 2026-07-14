"""Tests for the `akcli expected` subcommand (DTS / pinout.md -> expected JSON)."""

from __future__ import annotations

import json

from akcli import cli

_DTS = """
/ {
    leds {
        led_red: led_0 {
            gpios = <&gpio0 25 GPIO_ACTIVE_LOW>;
        };
    };
};
&pinctrl {
    uart0_default: uart0_default {
        group1 {
            psels = <NRF_PSEL(UART_TX, 0, 6)>, <NRF_PSEL(UART_RX, 0, 8)>;
        };
    };
};
"""

_MD = """
# Pinout

| Pin  | Signal   |
|------|----------|
| P0.6 | UART_TX  |
| P0.8 | UART_RX  |
"""


def test_dts_to_json_file(tmp_path, capsys):
    src = tmp_path / "board.overlay"
    src.write_text(_DTS)
    out = tmp_path / "expected.json"
    rc = cli.main(["expected", str(src), "-o", str(out)])
    assert rc == 0
    table = json.loads(out.read_text())
    assert table["P0.25"]  # led assignment extracted
    assert table["P0.6"] == "UART_TX" and table["P0.8"] == "UART_RX"
    # and the emitted file is exactly what pinmap --expected consumes
    assert cli._load_expected(str(out)) == table


def test_markdown_to_stdout(tmp_path, capsys):
    src = tmp_path / "pinout.md"
    src.write_text(_MD)
    rc = cli.main(["expected", str(src)])
    assert rc == 0
    table = json.loads(capsys.readouterr().out)
    assert table == {"P0.6": "UART_TX", "P0.8": "UART_RX"}


def test_empty_extraction_is_a_finding(tmp_path, capsys):
    src = tmp_path / "empty.dts"
    src.write_text("/ { };\n")
    rc = cli.main(["expected", str(src)])
    assert rc == 1  # vacuous table must not read as success
    assert "no pin assignments" in capsys.readouterr().err


def test_unsupported_extension_is_usage_error(tmp_path, capsys):
    src = tmp_path / "pins.txt"
    src.write_text("P0.1,X\n")
    rc = cli.main(["expected", str(src)])
    assert rc == 2


def test_missing_file_exits_4(capsys):
    rc = cli.main(["expected", "no/such/file.dts"])
    assert rc == 4
