"""Tests for adapters.pinout_md (SPEC §3.8).

All markdown is SYNTHETIC inline text written to ``tmp_path``; nothing depends on
any project-specific file. The final test proves the parsed table feeds straight into
``checks.pinmap.run`` as its (advisory) ``expected`` argument.
"""

from __future__ import annotations

from pathlib import Path

from akcli import model
from akcli.adapters import pinout_md
from akcli.checks import pinmap
from akcli.config import Config
from akcli.report import Severity

# --- synthetic markdown fixtures ---------------------------------------------

MD_BILINGUAL = """\
# Insole pinout

Some prose before the table.

| GPIO  | 網路名 (net-name) | 韌體節點 (firmware-node) |
|-------|-------------------|--------------------------|
| P0.25 | LED1_GPIO_RD      | led_red                  |
| P0.06 | UART_TX           | uart_tx                  |
| P0.08 | UART_RX           | uart_rx                  |

trailing prose after the table.
"""

MD_ENGLISH = """\
| Pin | Net      | Notes        |
|-----|----------|--------------|
| 2   | VBUS     | usb power    |
| 3   | GND      |              |
| 4   |          | unused row   |
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_parse_bilingual_table_gpio_to_net(tmp_path):
    table = pinout_md.parse_pinout_md(_write(tmp_path, "pinout.md", MD_BILINGUAL))
    # GPIO column is the key; the *net* column wins over firmware-node as value.
    assert table == {
        "P0.25": "LED1_GPIO_RD",
        "P0.06": "UART_TX",
        "P0.08": "UART_RX",
    }


def test_value_header_override_selects_firmware_node(tmp_path):
    table = pinout_md.parse_pinout_md(
        _write(tmp_path, "pinout.md", MD_BILINGUAL),
        value_header="韌體節點",
    )
    assert table["P0.25"] == "led_red"


def test_english_table_skips_blank_value_rows(tmp_path):
    table = pinout_md.parse_pinout_md(_write(tmp_path, "p.md", MD_ENGLISH))
    # "Pin" -> key, "Net" -> value; the blank-net row (pin 4) is dropped.
    assert table == {"2": "VBUS", "3": "GND"}


def test_no_table_returns_empty(tmp_path):
    table = pinout_md.parse_pinout_md(
        _write(tmp_path, "none.md", "# Heading\n\njust prose, no table.\n")
    )
    assert table == {}


# --- end-to-end: output feeds checks.pinmap.run ------------------------------

def test_expected_table_feeds_pinmap(tmp_path):
    table = pinout_md.parse_pinout_md(_write(tmp_path, "pinout.md", MD_BILINGUAL))

    mcu = model.Component(
        designator="U3",
        library_ref="MCU",
        x_mil=0.0,
        y_mil=0.0,
        pins=[
            model.Pin(number="10", name="P0.25", x_mil=0.0, y_mil=0.0),  # match
            model.Pin(number="12", name="P0.6", x_mil=0.0, y_mil=0.0),   # mismatch
        ],
    )
    sch = model.Schematic(
        source_path="synthetic",
        source_format="altium",
        components=[mcu],
        nets=[
            model.Net(name="LED1_GPIO_RD", members=[("U3", "10")]),
            model.Net(name="SOMETHING_ELSE", members=[("U3", "12")]),  # md says UART_TX
        ],
    )

    findings = pinmap.run(sch, Config(mcu_designator="U3"), expected=table)

    assert any(f.code == "PINMAP_MATCH" and "LED1_GPIO_RD" in f.message for f in findings)

    mism = [f for f in findings if f.code == "PINMAP_MISMATCH"]
    assert len(mism) == 1
    # advisory source: pinmap still reports mismatch as WARNING (schematic wins)
    assert mism[0].severity == Severity.WARNING
    assert "UART_TX" in mism[0].message
