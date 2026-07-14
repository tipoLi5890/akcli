"""Tests for adapters.dts (SPEC §3.8).

All DTS input is SYNTHETIC inline text written to ``tmp_path`` -- nothing depends
on any project-specific file. The final test proves the adapter output feeds straight
into ``checks.pinmap.run`` as its ``expected`` argument.
"""

from __future__ import annotations

from pathlib import Path

from akcli import model
from akcli.adapters import dts
from akcli.checks import pinmap
from akcli.config import Config
from akcli.report import Severity

# --- synthetic devicetree fixtures -------------------------------------------

DTS_GPIO = """
/ {
    leds {
        compatible = "gpio-leds";
        led_red: led_0 {
            gpios = <&gpio0 25 GPIO_ACTIVE_LOW>;
            label = "Red LED";
        };
        led_grn: led_1 {
            gpios = <&gpio1 5 (GPIO_ACTIVE_HIGH)>;
        };
    };

    buttons {
        sw0: button_0 {
            gpios = <&gpio0 0x0b GPIO_PULL_UP>;   /* hex pin 11 */
        };
    };
};

&spi0 {
    cs-gpios = <&gpio0 12 GPIO_ACTIVE_LOW>;
};
"""

DTS_PINCTRL = """
&pinctrl {
    uart0_default: uart0_default {
        group1 {
            psels = <NRF_PSEL(UART_TX, 0, 6)>,
                    <NRF_PSEL(UART_RX, 0, 8)>;
        };
    };
    i2c0_default: i2c0_default {
        group1 {
            psels = <NRF_PSEL(TWIM_SDA, 0, 26)>,
                    <NRF_PSEL(TWIM_SCL, 1, 27)>;
        };
    };
};
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# --- phandle GPIO form -------------------------------------------------------

def test_parse_gpio_phandle_form(tmp_path):
    parsed = dts.parse_dts(_write(tmp_path, "gpio.overlay", DTS_GPIO))
    by_gpio = {g["gpio"]: g for g in parsed["gpios"]}

    assert set(by_gpio) == {"P0.25", "P1.5", "P0.11", "P0.12"}
    # node label drives the signal name (not the "led_0" node *name*)
    assert by_gpio["P0.25"]["signal"] == "led_red"
    assert by_gpio["P1.5"]["signal"] == "led_grn"
    assert by_gpio["P0.11"]["pin"] == 11  # hex 0x0b decoded
    # property-derived suffix: spi0 + cs-gpios -> spi0_cs
    assert by_gpio["P0.12"]["signal"] == "spi0_cs"
    # the "compatible"/"label" string properties must NOT leak in as GPIOs
    assert all(g["property"] not in ("compatible", "label") for g in parsed["gpios"])


def test_parse_pinctrl_nrf_psel(tmp_path):
    parsed = dts.parse_dts(_write(tmp_path, "pinctrl.dtsi", DTS_PINCTRL))
    by_gpio = {p["gpio"]: p for p in parsed["psels"]}

    assert by_gpio["P0.6"]["function"] == "UART_TX"
    assert by_gpio["P0.6"]["signal"] == "UART_TX"
    assert by_gpio["P0.8"]["function"] == "UART_RX"
    assert by_gpio["P0.26"]["state"] == "i2c0_default"
    assert by_gpio["P1.27"]["function"] == "TWIM_SCL"
    assert set(by_gpio) == {"P0.6", "P0.8", "P0.26", "P1.27"}


def test_to_expected_table_orientation(tmp_path):
    parsed = dts.parse_dts(_write(tmp_path, "all.overlay", DTS_GPIO + DTS_PINCTRL))
    table = dts.to_expected_table(parsed)

    # keyed by GPIO (pin reference), value = signal/node name
    assert table["P0.25"] == "led_red"
    assert table["P0.6"] == "UART_TX"
    assert table["P0.26"] == "TWIM_SDA"


def test_comments_do_not_break_parsing(tmp_path):
    text = """
    / {
        // a line comment with a fake gpios = <&gpio0 99 X>;
        node_a: thing {
            /* block comment <&gpio0 88 X> */
            gpios = <&gpio0 7 GPIO_ACTIVE_HIGH>;  // real one
        };
    };
    """
    parsed = dts.parse_dts(_write(tmp_path, "c.overlay", text))
    gpios = {g["gpio"] for g in parsed["gpios"]}
    assert gpios == {"P0.7"}  # commented-out 99/88 ignored


# --- end-to-end: output feeds checks.pinmap.run ------------------------------

def test_expected_table_feeds_pinmap(tmp_path):
    table = dts.to_expected_table(
        dts.parse_dts(_write(tmp_path, "board.overlay", DTS_GPIO + DTS_PINCTRL))
    )

    # Synthetic schematic: MCU U3 whose pins carry the GPIO names the DTS uses.
    mcu = model.Component(
        designator="U3",
        library_ref="MCU",
        x_mil=0.0,
        y_mil=0.0,
        pins=[
            model.Pin(number="10", name="P0.25", x_mil=0.0, y_mil=0.0),  # match
            model.Pin(number="12", name="P0.6", x_mil=0.0, y_mil=0.0),   # match
            model.Pin(number="14", name="P0.8", x_mil=0.0, y_mil=0.0),   # mismatch
        ],
    )
    sch = model.Schematic(
        source_path="synthetic",
        source_format="altium",
        components=[mcu],
        nets=[
            model.Net(name="led_red", members=[("U3", "10")]),
            model.Net(name="UART_TX", members=[("U3", "12")]),
            model.Net(name="WRONG_NET", members=[("U3", "14")]),  # DTS says UART_RX
        ],
    )

    findings = pinmap.run(sch, Config(mcu_designator="U3"), expected=table)

    matched = {f.message for f in findings if f.code == "PINMAP_MATCH"}
    assert any("P0.25" in m or "led_red" in m for m in matched)
    assert any("UART_TX" in m for m in matched)

    mism = [f for f in findings if f.code == "PINMAP_MISMATCH"]
    assert len(mism) == 1
    assert mism[0].severity == Severity.WARNING
    assert "UART_RX" in mism[0].message  # expected (from DTS) vs schematic
    assert "WRONG_NET" in mism[0].message
