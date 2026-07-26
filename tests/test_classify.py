"""The shared classification vocabulary (``akcli.classify``).

One implementation answers "what class of refdes is this?", "is this net a
rail?" and "what role does this part play?" for the check layer, the review
layer and the sim layer alike. These tests pin the behaviour the six former
copies disagreed about — and the positive/negative pairs of every widened
heuristic.
"""

from __future__ import annotations

import pytest

from akcli import classify


# --------------------------------------------------------------------------- #
# refdes prefixes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "ref,want",
    [
        ("R12", "R"), ("IC2", "IC"), ("C1", "C"), ("FB1", "FB"),
        ("$U3", "U"),                 # synthesized refdes: the $ is not a class
        ("$U7@sheet1", "U"),
        ("u5", "U"), (" R3 ", "R"),
        ("U_MAIN1", "U"),             # underscore terminates the letter run
        ("#PWR01", ""),               # power port: not a refdes class
        ("", ""), (None, ""), ("123", ""),
    ],
)
def test_refdes_prefix(ref, want):
    assert classify.refdes_prefix(ref) == want


# --------------------------------------------------------------------------- #
# rail names: derived rails vs signals ABOUT a rail
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    ["VBUS", "VCC", "+3V3", "3V3", "V3V3_AUX", "VBAT_F", "VBUS_FUSED",
     "VBUS_CONN", "VBUS_PROT", "VCC_3V3_A", "VIN-FILT", "VSYS_MAIN"],
)
def test_is_power_positive(name):
    assert classify.is_power(name), name


@pytest.mark.parametrize(
    "name",
    [
        "VBUS_DET",        # detect line, not the rail
        "VIN_SENSE", "VBAT_STAT", "VSYS_PG", "VCC_EN", "VBUS_OC",
        "VBUSOUT",         # glued: indistinguishable from an ordinary word
        "SDA", "NRST", "MUX_Y3", "", None,
        "GND", "AGND", "USB_GND",
    ],
)
def test_is_power_negative(name):
    assert not classify.is_power(name), name


@pytest.mark.parametrize("name", ["GND", "GROUND", "VSS", "AGND", "GND_USB",
                                  "USB_GND", "PGND"])
def test_is_ground_positive(name):
    assert classify.is_ground(name), name


@pytest.mark.parametrize("name", ["VCC", "V3V3", "SDA", "", None])
def test_is_ground_negative(name):
    assert not classify.is_ground(name), name


def test_power_root_reports_the_rail_it_derives_from():
    assert classify.power_root("VBUS_FUSED") == "VBUS"
    assert classify.power_root("VBUS_DET") is None
    assert classify.power_root("VBUS") is None      # not derived; exact name


@pytest.mark.parametrize(
    "name,stage",
    [("VBUS_FUSED", True), ("VBAT_PROT", True), ("VIN_FILT", True),
     ("VBUS_CONN", False), ("VBAT", False), ("VBUS_5V", False)],
)
def test_is_stage_name(name, stage):
    assert classify.is_stage_name(name) is stage


# --------------------------------------------------------------------------- #
# part roles: library evidence beats the designator prefix
# --------------------------------------------------------------------------- #
def test_crystal_by_prefix_and_by_library():
    # X1 is as conventional a crystal designator as Y1 — and used to classify
    # as a CONNECTOR, which skipped the whole crystal rule family.
    assert classify.is_crystal("X1", "corpus_jlc:SC-32S32_768kHz20PPM7pF",
                               "32.768kHz 7pF 20ppm")
    assert classify.is_crystal("Y1", "Device:Crystal", "8MHz")
    assert classify.is_crystal("U9", "Device:Crystal_GND24", None)  # lib wins
    assert not classify.is_crystal("R1", "Device:R", "10k")


def test_connector_and_crystal_never_both():
    assert not classify.is_connector("X1", "corpus_jlc:SC-32S32_768kHz20PPM7pF",
                                     value="32.768kHz 7pF 20ppm")
    assert classify.is_connector("J1", "Connector_Generic:Conn_01x06")
    assert classify.is_connector("P2", None)
    # an X-designated part in a connector library is still a connector
    assert classify.is_connector("X2", "Connector:USB_C_Receptacle")
    assert not classify.is_crystal("X2", "Connector:USB_C_Receptacle")
    # ...and a USB TVS is a clamp, not the receptacle, however it is named
    assert not classify.is_connector("D1", "Power_Protection:USBLC6-2SC6",
                                     value="USBLC6")


def test_tvs_by_library_or_value_only():
    assert classify.is_tvs("U6", "corpus_jlc:TPD4E05U06DQAR", "TPD4E05U06")
    assert classify.is_tvs("D1", "Power_Protection:USBLC6-2SC6", "USBLC6")
    assert classify.is_tvs("D2", None, None, footprint="ESD_ARRAY_SOT23")
    assert not classify.is_tvs("D3", "Device:D_Schottky", "BAT54")
