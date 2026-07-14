"""Tests for the sim device-resolution ladder, datasheet fits and builtin lib.

Covers :mod:`akcli.sim.models`:

* :func:`spice_value` engineering-notation -> SPICE conversion (the M/MEG fix);
* :func:`fit_diode` reproducing the live-session BAT54H numbers;
* :func:`resolve` ladder precedence, Sim.Pins reorder, skip/unmodeled statuses;
* ``builtin.lib`` parsing (balanced ``.subckt``/``.ends``, pure ASCII).
"""

from __future__ import annotations

import math

import pytest

from akcli.errors import AkcliError
from akcli.model import Component, Pin
from akcli.sim import models
from akcli.sim.models import (
    DeviceCard,
    builtin_names,
    fit_diode,
    load_builtin,
    resolve,
    spice_value,
)


def _comp(designator, value=None, library_ref="Device:R", parameters=None, pins=None):
    return Component(
        designator=designator,
        library_ref=library_ref,
        x_mil=0.0,
        y_mil=0.0,
        value=value,
        parameters=dict(parameters or {}),
        pins=list(pins or []),
    )


class _Spec:
    """Minimal SimSpec stand-in exposing a ``.models`` dict."""

    def __init__(self, models):
        self.models = models


# --- spice_value ------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("1M", "1MEG"),        # KiCad mega -> SPICE MEG (the headline fix)
    ("1meg", "1MEG"),      # SPICE-native spelling round-trips
    ("1MEG", "1MEG"),
    ("100n", "100n"),
    ("4.7k", "4.7k"),
    ("10k", "10k"),
    ("2M2", "2.2MEG"),     # IEC 60062 embedded-mega notation
    ("4R7", "4.7"),        # IEC decimal marker
    ("470", "470"),
    ("4700", "4700"),      # bare number passes through
    ("1e-7", "1e-7"),      # bare scientific passes through
    ("0.2", "0.2"),        # NOT '200 mmm' — the calc-side milli bug is absent
    ("3m", "3m"),          # milli stays milli
    ("2.2u", "2.2u"),
    ("47p", "47p"),
])
def test_spice_value_table(text, expected):
    assert spice_value(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("4.7kohm", "4.7k"),   # spelled-out unit stripped
    ("100nF", "100n"),     # farad stripped, prefix kept
    ("10uH", "10u"),       # henry stripped
    ("1Mohm", "1MEG"),
    ("220 ohm", "220"),
])
def test_spice_value_strips_units(text, expected):
    assert spice_value(text) == expected


def test_spice_value_milli_vs_mega_never_crossed():
    # SPICE 'M' == milli, mega == 'MEG'. We must never emit a bare 'M' for mega
    # nor turn milli into mega.
    assert spice_value("1M") == "1MEG"
    assert spice_value("1m") == "1m"
    assert "MEG" in spice_value("2.2M")


def test_spice_value_unparseable_passes_through():
    assert spice_value("=DNP") == "=DNP"
    assert spice_value("") == ""


# --- fit_diode --------------------------------------------------------------
def test_fit_diode_bat54h_single_point_plus_prior():
    # Live-session BAT54H: 0.37 V @ 20 mA, Schottky prior N=1.05 -> IS ~ 2.4e-8.
    r = fit_diode([(0.37, 20e-3)], n_prior=1.05)
    assert r["N"] == pytest.approx(1.05)
    assert r["IS"] == pytest.approx(2.4e-8, rel=0.05)
    assert r["RS"] == 0.0
    assert r["CJO"] is None


def test_fit_diode_bat54h_rs_point():
    # RS solved from the max-column point (0.6 V, 200 mA) -> ~0.84 ohm.
    r = fit_diode([(0.37, 20e-3)], n_prior=1.05, rs_point=(0.6, 0.2), cjo=50e-12)
    assert r["RS"] == pytest.approx(0.84, abs=0.02)
    assert r["CJO"] == 50e-12
    assert "IS=" in r["model_card"] and "N=" in r["model_card"]
    assert "RS=" in r["model_card"] and "CJO=" in r["model_card"]
    assert r["model_card"].startswith(".model ")


def test_fit_diode_reproduces_reference_math():
    # Recompute the reference formula exactly.
    n, v1, i1 = 1.05, 0.37, 20e-3
    is_expected = i1 / math.exp(v1 / (n * models.VT))
    r = fit_diode([(0.37, 20e-3)], n_prior=1.05)
    assert r["IS"] == pytest.approx(is_expected, rel=1e-9)


def test_fit_diode_two_point_least_squares():
    # Two well-behaved points near N=1.05 -> a clean least-squares fit.
    n_true, is_true = 1.05, 2.4e-8
    pts = []
    for i in (1e-3, 10e-3):
        v = n_true * models.VT * math.log(i / is_true)
        pts.append((v, i))
    r = fit_diode(pts, n_prior=1.05)
    assert r["N"] == pytest.approx(1.05, rel=0.05)
    assert r["IS"] == pytest.approx(is_true, rel=0.1)


def test_fit_diode_clamps_and_warns_on_bad_slope():
    # Two eyeballed points implying a wildly high N get clamped to 2.5 + noted.
    pts = [(0.20, 1e-3), (0.30, 10e-3)]  # the reference's "curves are eyeball" case
    r = fit_diode(pts, n_prior=1.05)
    assert 0.9 <= r["N"] <= 2.5
    assert r["note"]  # a warning is surfaced


def test_fit_diode_empty_raises():
    with pytest.raises(ValueError):
        fit_diode([])


# --- resolve ladder ---------------------------------------------------------
def test_resolve_heuristic_passive():
    card = resolve(_comp("R1", "4.7k"), None)
    assert card.status == "ok"
    assert card.letter == "R"
    assert card.value == "4.7k"


def test_resolve_heuristic_passive_mega():
    card = resolve(_comp("R2", "1M"), None)
    assert card.value == "1MEG"


def test_resolve_heuristic_diode_is_unmodeled():
    # A diode NEEDS a model — never invent one silently.
    card = resolve(_comp("D1", library_ref="Device:D"), None)
    assert card.status == "unmodeled"
    assert card.letter == ""
    assert "fit_diode" in card.note
    assert "fit-diode" not in card.note  # no phantom 'akcli sim fit-diode' subcommand


def test_resolve_heuristic_transistor_is_unmodeled():
    card = resolve(_comp("Q1", library_ref="Device:Q_NPN"), None)
    assert card.status == "unmodeled"


def test_resolve_heuristic_connector_is_skipped():
    card = resolve(_comp("J1", library_ref="Connector:Conn_01x02"), None)
    assert card.status == "skip"


def test_resolve_heuristic_hash_designator_skipped():
    card = resolve(_comp("#PWR01", library_ref="power:GND"), None)
    assert card.status == "skip"


def test_resolve_passive_missing_value_unmodeled():
    card = resolve(_comp("C9", value=None), None)
    assert card.status == "unmodeled"


# --- ladder precedence ------------------------------------------------------
def test_resolve_sim_fields_beat_spec_models_beat_heuristic():
    # Same component, all three rungs would fire; Sim.* must win.
    comp = _comp("R5", "4.7k", parameters={"Sim.Device": "R", "Sim.Params": "1k"})
    spec = _Spec({"R5": {"device": "R", "params": "2k"}})
    card = resolve(comp, spec)
    assert card.note == "from Sim.* fields"
    assert card.value == "1k"


def test_resolve_spec_models_beats_heuristic():
    comp = _comp("R6", "4.7k")
    spec = _Spec({"R6": {"device": "R", "params": "2k"}})
    card = resolve(comp, spec)
    assert card.note == "from spec.models"
    assert card.value == "2k"


def test_resolve_spec_models_by_lib_id():
    comp = _comp("D2", library_ref="Diode:BAT54H")
    card = ".model DBAT D(IS=2.4e-8 N=1.05)"
    spec = _Spec({"Diode:BAT54H": {"device": "D", "model_name": "DBAT",
                                   "model_card": card}})
    got = resolve(comp, spec)
    assert got.status == "ok"
    assert got.letter == "D"
    assert got.model_name == "DBAT"
    assert got.model_card == card


def test_resolve_spec_models_skip():
    comp = _comp("R7", "1k")
    spec = _Spec({"R7": {"skip": True}})
    assert resolve(comp, spec).status == "skip"


# --- Sim.Pins reorder -------------------------------------------------------
def test_resolve_sim_pins_reorder():
    comp = _comp("Q3", library_ref="Transistor:BC547",
                 parameters={"Sim.Device": "NPN", "Sim.Name": "BC547",
                             "Sim.Pins": "1=2 2=1 3=3"})
    card = resolve(comp, spec=None)
    assert card.letter == "Q"
    # model terminal 1 -> symbol pin 2, terminal 2 -> symbol pin 1, ...
    assert card.pin_order == ["2", "1", "3"]


def test_resolve_sim_enable_zero_skips():
    comp = _comp("R8", "1k", parameters={"Sim.Device": "R", "Sim.Enable": "0"})
    assert resolve(comp, None).status == "skip"


# --- builtin subckt loading via ladder --------------------------------------
def test_resolve_sim_subckt_loads_builtin_model_card():
    comp = _comp("U1", library_ref="Timer:NE555",
                 parameters={"Sim.Device": "SUBCKT", "Sim.Name": "AKCLI_COMP555"})
    card = resolve(comp, None)
    assert card.letter == "X"
    assert card.model_name == "AKCLI_COMP555"
    assert card.model_card is not None
    assert ".subckt AKCLI_COMP555" in card.model_card


def test_resolve_spec_models_subckt_loads_builtin():
    comp = _comp("U2", library_ref="Comparator:LM393")
    spec = _Spec({"U2": {"device": "SUBCKT", "model_name": "AKCLI_COMPARATOR"}})
    card = resolve(comp, spec)
    assert card.letter == "X"
    assert ".subckt AKCLI_COMPARATOR" in card.model_card


# --- builtin.lib integrity --------------------------------------------------
def test_builtin_names_present():
    names = builtin_names()
    assert {"AKCLI_COMP555", "AKCLI_COMPARATOR", "AKCLI_PHOTOTRANS"} <= names


@pytest.mark.parametrize("name",
                         ["AKCLI_COMP555", "AKCLI_COMPARATOR", "AKCLI_PHOTOTRANS"])
def test_builtin_blocks_balanced_and_ascii(name):
    block = load_builtin(name)
    assert block is not None
    low = block.lower()
    assert low.count(".subckt") == 1
    assert low.count(".ends") == 1
    # first line opens the named subckt, last non-empty line closes it
    assert block.splitlines()[0].lower().startswith(".subckt")
    assert block.strip().splitlines()[-1].lower().startswith(".ends")
    assert block.isascii()


def test_builtin_lib_file_is_pure_ascii():
    from akcli.sim.models import _BUILTIN_LIB
    _BUILTIN_LIB.read_text(encoding="ascii")  # raises if any non-ASCII byte


def test_load_builtin_unknown_returns_none():
    assert load_builtin("NOPE") is None


def test_device_card_defaults():
    c = DeviceCard()
    assert c.letter == ""
    assert c.status == "unmodeled"
    assert c.pin_order is None
    assert c.pin_order_assumed is False


# --- item 1: semantic pin ordering from pin NAMES (diode/BJT) ---------------
def _diode_pins_KA():
    # KiCad stock Device:D / D_Schottky number pin1='K' (cathode), pin2='A'.
    return [Pin("1", "K", 0.0, 0.0), Pin("2", "A", 0.0, 0.0)]


def test_resolve_diode_pin_order_from_pin_names():
    # SPICE wants D <anode> <cathode>; pin NAMES (A=2, K=1) => order [2, 1].
    comp = _comp("D4", library_ref="Device:D_Schottky", pins=_diode_pins_KA())
    spec = _Spec({"D4": {"device": "D", "model_name": "DBAT",
                         "model_card": ".model DBAT D(IS=2.4e-8 N=1.05)"}})
    card = resolve(comp, spec)
    assert card.letter == "D"
    assert card.pin_order == ["2", "1"]
    assert card.pin_order_assumed is False


def test_resolve_bjt_pin_order_from_pin_names():
    # SPICE Q order is collector, base, emitter -> pins named C/B/E.
    comp = _comp("Q1", library_ref="Device:Q_NPN",
                 pins=[Pin("1", "B", 0.0, 0.0), Pin("2", "C", 0.0, 0.0),
                       Pin("3", "E", 0.0, 0.0)])
    spec = _Spec({"Q1": {"device": "Q", "model_name": "QMOD",
                         "model_card": ".model QMOD NPN(BF=100)"}})
    card = resolve(comp, spec)
    assert card.pin_order == ["2", "1", "3"]


def test_resolve_diode_pin_order_assumed_when_names_missing():
    # Nameless pins can't identify polarity -> keep number order, flag it.
    comp = _comp("D1", library_ref="Device:D",
                 pins=[Pin("1", None, 0.0, 0.0), Pin("2", None, 0.0, 0.0)])
    spec = _Spec({"D1": {"device": "D", "model_name": "DM",
                         "model_card": ".model DM D(IS=1e-14)"}})
    card = resolve(comp, spec)
    assert card.pin_order is None
    assert card.pin_order_assumed is True


def test_resolve_explicit_pin_order_not_overridden_by_names():
    # An explicit pin_order (Sim.Pins / spec.models) always wins over names.
    comp = _comp("D2", library_ref="Device:D", pins=_diode_pins_KA())
    spec = _Spec({"D2": {"device": "D", "model_name": "DM",
                         "model_card": ".model DM D(IS=1e-14)",
                         "pin_order": [1, 2]}})
    card = resolve(comp, spec)
    assert card.pin_order == ["1", "2"]
    assert card.pin_order_assumed is False


# --- item 3: spec.models entry with missing/unknown device ------------------
def test_spec_models_infers_letter_from_model_card_diode():
    comp = _comp("U5")
    spec = _Spec({"U5": {"model_name": "DFIT",
                         "model_card": ".model DFIT D(IS=1e-9)"}})
    card = resolve(comp, spec)
    assert card.letter == "D"


def test_spec_models_infers_x_from_subckt_card():
    comp = _comp("U6")
    spec = _Spec({"U6": {"model_name": "MYSUB",
                         "model_card": ".subckt MYSUB a b\nR1 a b 1k\n.ends"}})
    card = resolve(comp, spec)
    assert card.letter == "X"


def test_spec_models_infers_q_from_npn_card():
    comp = _comp("U7")
    spec = _Spec({"U7": {"model_name": "Q2N",
                         "model_card": ".model Q2N NPN(BF=200)"}})
    assert resolve(comp, spec).letter == "Q"


def test_spec_models_unknown_device_raises_bad_config():
    comp = _comp("U8")
    spec = _Spec({"U8": {"model_name": "MYSTERY"}})  # no device, no card
    with pytest.raises(AkcliError) as ei:
        resolve(comp, spec)
    assert ei.value.code == "BAD_CONFIG"
    assert "U8" in str(ei.value)


# --- item 8: fit_diode single-point input validation ------------------------
def test_fit_diode_clamps_low_prior():
    r = fit_diode([(0.3, 1e-3)], n_prior=0.1)
    assert r["N"] == pytest.approx(0.9)
    assert r["note"]


def test_fit_diode_clamps_high_prior():
    r = fit_diode([(0.3, 1e-3)], n_prior=10)
    assert r["N"] == pytest.approx(2.5)
    assert r["note"]


def test_fit_diode_zero_prior_raises():
    with pytest.raises(AkcliError) as ei:
        fit_diode([(0.3, 1e-3)], n_prior=0)
    assert ei.value.code == "BAD_CONFIG"


def test_fit_diode_negative_prior_raises():
    with pytest.raises(AkcliError) as ei:
        fit_diode([(0.3, 1e-3)], n_prior=-1.0)
    assert ei.value.code == "BAD_CONFIG"


def test_fit_diode_rs_point_zero_current_raises():
    with pytest.raises(AkcliError) as ei:
        fit_diode([(0.3, 1e-3)], rs_point=(1.0, 0.0))
    assert ei.value.code == "BAD_CONFIG"


def test_fit_diode_rs_point_negative_voltage_raises():
    with pytest.raises(AkcliError) as ei:
        fit_diode([(0.3, 1e-3)], rs_point=(-1.0, 0.2))
    assert ei.value.code == "BAD_CONFIG"


def test_fit_diode_negative_current_point_raises():
    # 0.3@-1m: a sign typo on the current would yield a negative IS.
    with pytest.raises(AkcliError) as ei:
        fit_diode([(0.3, -1e-3)])
    assert ei.value.code == "BAD_CONFIG"


def test_fit_diode_negative_voltage_point_raises():
    # -0.3@1m: a negated forward voltage yields an absurd IS (~63 A).
    with pytest.raises(AkcliError) as ei:
        fit_diode([(-0.3, 1e-3)])
    assert ei.value.code == "BAD_CONFIG"


# --- fit-diode --apply round-trip: native Sim.Device=D + Sim.Params ---------
def test_resolve_native_diode_sim_params_synthesizes_model():
    # Exactly what `fit-diode --apply --write` stamps: Sim.Device=D + Sim.Params,
    # no Sim.Name. resolve must synthesize a model name + a .model card so the
    # deck emits a *modeled* diode, not a bare, unparseable element.
    comp = _comp("D4", library_ref="Device:D",
                 parameters={"Sim.Device": "D",
                             "Sim.Params": "IS=1.5843e-08 N=1.0500"})
    card = resolve(comp, None)
    assert card.status == "ok"
    assert card.letter == "D"
    assert card.model_name == "AKCLI_D4"
    assert card.model_card == ".model AKCLI_D4 D(IS=1.5843e-08 N=1.0500)"


def test_resolve_native_npn_sim_params_synthesizes_npn_model():
    comp = _comp("Q2", library_ref="Device:Q_NPN",
                 parameters={"Sim.Device": "NPN", "Sim.Params": "BF=180 IS=1e-14"})
    card = resolve(comp, None)
    assert card.letter == "Q"
    assert card.model_name == "AKCLI_Q2"
    assert card.model_card == ".model AKCLI_Q2 NPN(BF=180 IS=1e-14)"


# --- item 12: unmodeled hint references real surfaces, not a phantom cmd -----
def test_unmodeled_note_has_no_phantom_subcommand():
    card = resolve(_comp("D9", library_ref="Device:D"), None)
    assert "akcli sim fit-diode" not in card.note
    assert "fit_diode" in card.note
