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

from akcli import cli  # noqa: E402
from akcli.model import SCHEMA_VERSION  # noqa: E402

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
# Root schemas/ is canonical; src/akcli/schemas/ ships in wheels.

PACKAGED = ROOT / "src" / "akcli" / "schemas"

_ROOT_SCHEMAS = sorted(p.name for p in (ROOT / "schemas").glob("*.json"))
# BOTH sides: ops._schema_text() prefers the packaged mirror, so a mirror
# whose canonical original was deleted/renamed is not harmless dead weight —
# the wheel would keep serving that retired schema. Iterating only the
# canonical side (as this test and tools/sync_schemas.py both used to) can
# never see it.
_ALL_SCHEMAS = sorted(set(_ROOT_SCHEMAS)
                      | {p.name for p in PACKAGED.glob("*.json")})


@pytest.mark.parametrize("name", _ALL_SCHEMAS)
def test_packaged_schema_identical_to_root(name):
    """EVERY canonical schema ships byte-identically in the wheel — and only
    canonical schemas ship (no orphans)."""
    packaged_path, root_path = PACKAGED / name, ROOT / "schemas" / name
    assert root_path.exists(), (
        f"{name}: orphaned packaged mirror — schemas/{name} is gone but "
        f"src/akcli/schemas/{name} still ships (and ops._schema_text() reads "
        f"the mirror FIRST). Run `python3 tools/sync_schemas.py` to prune it."
    )
    assert packaged_path.exists(), (
        f"{name}: missing from the packaged mirror — "
        f"copy schemas/{name} to src/akcli/schemas/{name}"
    )
    root_text = root_path.read_text(encoding="utf-8")
    assert packaged_path.read_text(encoding="utf-8") == root_text, (
        f"{name}: packaged mirror drifted from canonical schemas/{name} — "
        f"copy the root file over src/akcli/schemas/{name}"
    )


def test_schema_ids_share_one_host():
    """Every schema's $id resolves under the one canonical host."""
    for name in _ROOT_SCHEMAS:
        doc = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
        assert doc["$id"].startswith(
            "https://github.com/tipoLi5890/akcli/schemas/"), (
            f"{name}: $id host drifted: {doc['$id']}")


def test_sync_schemas_tool_sees_both_sides(tmp_path, monkeypatch, capsys):
    """`tools/schemas.py sync --check` must flag an orphaned mirror."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_tools_schemas", ROOT / "tools" / "schemas.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    canonical, mirror = tmp_path / "schemas", tmp_path / "mirror"
    canonical.mkdir()
    mirror.mkdir()
    (canonical / "a.json").write_text("{}\n", encoding="utf-8", newline="\n")
    (mirror / "a.json").write_text("{}\n", encoding="utf-8", newline="\n")
    (mirror / "gone.json").write_text("{}\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(mod, "SCHEMA_DIR", canonical)
    monkeypatch.setattr(mod, "MIRROR", mirror)

    assert mod.main(["sync", "--check"]) == 1
    assert "orphan" in capsys.readouterr().out
    assert mod.main(["sync"]) == 0              # sync mode prunes it
    assert not (mirror / "gone.json").exists()
    assert mod.main(["sync", "--check"]) == 0


def test_ops_loaders_use_packaged_or_root():
    from akcli import ops

    assert ops.load_schema()["title"] == "AkcliOpList"
    assert ops.load_capabilities()["ops"]["place_component"]["kicad"] is True


# ----------------------------------------------- template <-> schema drift ----

OPS_SCHEMA = json.loads((ROOT / "schemas" / "ops.schema.json").read_text())
OPS_VALIDATOR = jsonschema.Draft202012Validator(OPS_SCHEMA)


def _all_op_names():
    from akcli import ops

    return sorted(ops.OP_NAMES | ops.MACRO_OPS)


@pytest.mark.parametrize("name", _all_op_names())
def test_op_template_validates_against_schema(name):
    """op_template(x) must satisfy ops.schema.json for EVERY op and macro.

    This is the permanent anti-drift gate: any new op/macro (or field rename)
    that touches only one side of the code/schema pair fails here.
    """
    from akcli import ops

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
