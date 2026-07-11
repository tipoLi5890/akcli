"""`read --json` exports must validate against schemas/schematic.schema.json.

This is the machine-consumer contract (schema_version). Runs everywhere the
``[dev]`` extra is installed (CI); skips only when jsonschema is absent.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

from altium_kicad_cli import cli  # noqa: E402
from altium_kicad_cli.model import SCHEMA_VERSION  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas" / "schematic.schema.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)

FIXTURES = [
    "tests/fixtures/kicad/board_v8.kicad_sch",
    "tests/fixtures/kicad/board_v7.kicad_sch",
    "tests/fixtures/t_junction.SchDoc",          # Altium, incl. unnamed nets
    "tests/fixtures/no_erc.SchDoc",
]


def _read_json(path: str) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert cli.main(["read", str(ROOT / path), "--json"]) == 0
    return json.loads(buf.getvalue())


@pytest.mark.parametrize("fixture", FIXTURES)
def test_read_json_validates_against_schema(fixture):
    doc = _read_json(fixture)
    errors = ["/".join(map(str, e.path)) + ": " + e.message
              for e in VALIDATOR.iter_errors(doc)]
    assert errors == [], f"{fixture} drifted from schematic.schema.json"


def test_schema_pins_model_schema_version():
    """The schema's const must move together with model.SCHEMA_VERSION."""
    assert SCHEMA["properties"]["schema_version"]["const"] == SCHEMA_VERSION


# ------------------------------------------------------- packaged mirror ----
# Root schemas/ is canonical; src/altium_kicad_cli/schemas/ ships in wheels.

PACKAGED = ROOT / "src" / "altium_kicad_cli" / "schemas"


@pytest.mark.parametrize("name", ["ops.schema.json", "ops.capabilities.json"])
def test_packaged_schema_identical_to_root(name):
    root_text = (ROOT / "schemas" / name).read_text()
    packaged_text = (PACKAGED / name).read_text()
    assert packaged_text == root_text, (
        f"{name}: packaged mirror drifted from canonical schemas/{name} — "
        f"copy the root file over src/altium_kicad_cli/schemas/{name}"
    )


def test_ops_loaders_use_packaged_or_root():
    from altium_kicad_cli import ops

    assert ops.load_schema()["title"] == "AkcliOpList"
    assert ops.load_capabilities()["ops"]["place_component"]["kicad"] is True


# ----------------------------------------------- template <-> schema drift ----

OPS_SCHEMA = json.loads((ROOT / "schemas" / "ops.schema.json").read_text())
OPS_VALIDATOR = jsonschema.Draft202012Validator(OPS_SCHEMA)


def _all_op_names():
    from altium_kicad_cli import ops

    return sorted(ops.OP_NAMES | ops.MACRO_OPS)


@pytest.mark.parametrize("name", _all_op_names())
def test_op_template_validates_against_schema(name):
    """op_template(x) must satisfy ops.schema.json for EVERY op and macro.

    This is the permanent anti-drift gate: any new op/macro (or field rename)
    that touches only one side of the code/schema pair fails here.
    """
    from altium_kicad_cli import ops

    doc = {
        "protocol_version": ops.PROTOCOL_VERSION,
        "target_format": "kicad",
        "ops": [ops.op_template(name)],
    }
    errors = ["/".join(map(str, e.path)) + ": " + e.message
              for e in OPS_VALIDATOR.iter_errors(doc)]
    assert errors == [], f"template for {name!r} does not validate: {errors}"
    # the zero-dependency validator must agree with the schema
    assert ops.validate_oplist(doc) == []
