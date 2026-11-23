"""Library baseline: ``library audit --write-baseline`` / ``--baseline`` and
the ``release preflight`` ``library-baseline`` / ``pcb-library`` gates.

The project under test is a copy of ``tests/fixtures/kicad/fp_attrs`` (a
KiCad-10-written board + ``.pretty``) plus a symbol library and the schematic
the board implements. Expected hashes are always RECOMPUTED from the bytes on
disk — a literal sha256 in a test would break on a checkout that translated
line endings, which is exactly the failure the docs warn users about.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from akcli import contract, library_baseline, libtable
from akcli.cli import main
from akcli.errors import AkcliError
from tests._fp_attrs_project import BOARD, make_project
from tests._fp_attrs_project import edit as _edit
from tests._fp_attrs_project import waive as _waive
from tests._fp_attrs_project import write as _write

ROOT = Path(__file__).resolve().parent.parent
SYM_FIX = ROOT / "tests" / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym"
DEFAULT = library_baseline.DEFAULT_FILENAME

_SYM_TABLE = ('(sym_lib_table\n\t(version 7)\n\t(lib (name "mysym") '
              '(type "KiCad") (uri "${KIPRJMOD}/sym/Device.kicad_sym") '
              '(options "") (descr ""))\n)\n')


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def proj(tmp_path) -> Path:
    """fp_attrs + a project symbol library; the schematic's symbols come from
    ``Device`` (global table only), so the audit itself is clean."""
    d = make_project(tmp_path, nick="Device")
    (d / "sym").mkdir()
    shutil.copy(SYM_FIX, d / "sym" / "Device.kicad_sym")
    _write(d / "sym-lib-table", _SYM_TABLE)
    return d


def _build(proj: Path) -> dict:
    return library_baseline.build(libtable.discover(proj))


def _diff(proj: Path, base: dict) -> list:
    return library_baseline.compare(base, _build(proj))


def _codes(findings) -> list[tuple[str, str, list[str]]]:
    return [(f.code, f.severity.value, f.refs) for f in findings]


# --------------------------------------------------------------------------- #
# content
# --------------------------------------------------------------------------- #
def test_document_records_every_project_library(proj):
    doc = _build(proj)
    assert doc["schema_version"] == contract.SCHEMA_VERSIONS["library-baseline"] == "1.0"
    assert set(doc) == {"schema_version", "symbol_libraries",
                        "footprint_libraries", "global_libraries"}

    (sym,) = doc["symbol_libraries"]
    assert sym == {
        "nickname": "mysym",
        "uri": "${KIPRJMOD}/sym/Device.kicad_sym",      # exactly as written
        "path": "sym/Device.kicad_sym",                 # project-relative posix
        "status": "ok",
        "files": {"Device.kicad_sym": _sha(proj / "sym" / "Device.kicad_sym")},
    }

    (fp,) = doc["footprint_libraries"]
    pretty = proj / "proj.pretty"
    assert (fp["nickname"], fp["uri"], fp["path"], fp["status"]) == (
        "proj", "${KIPRJMOD}/proj.pretty", "proj.pretty", "ok")
    assert fp["files"] == {m.name: _sha(m) for m in pretty.glob("*.kicad_mod")}
    assert len(fp["files"]) == 4
    assert fp["models"] == [
        {"file": "PinHeader_1x02.kicad_mod",
         "ref": "${KIPRJMOD}/3d/PinHeader_1x02.step",
         "path": "3d/PinHeader_1x02.step", "status": "hashed",
         "sha256": _sha(proj / "3d" / "PinHeader_1x02.step")},
        {"file": "R_0402.kicad_mod", "ref": "${KIPRJMOD}/3d/R_0402.step",
         "path": "3d/R_0402.step", "status": "hashed",
         "sha256": _sha(proj / "3d" / "R_0402.step")},
    ]
    # libraries the schematics reach only through the GLOBAL table: by
    # nickname, nothing else
    assert doc["global_libraries"] == {"symbol": ["Device"],
                                       "footprint": ["Resistor_SMD"]}


def test_provenance_json_in_a_library_is_hashed(proj):
    prov = proj / "proj.pretty" / "provenance.json"
    _write(prov, '{"source": {"sha256": "abc"}}\n')
    files = _build(proj)["footprint_libraries"][0]["files"]
    assert files["provenance.json"] == _sha(prov)
    # ... and only that: other strays in the .pretty are not library content
    _write(proj / "proj.pretty" / "R_0402.kicad_mod.bak", "old\n")
    assert set(_build(proj)["footprint_libraries"][0]["files"]) == set(files)


def test_single_file_footprint_library_is_hashed_as_that_file(proj):
    """KiCad can register a non-.pretty library (Altium ``.PcbLib``, Eagle
    ``.lbr``, legacy ``.mod``): there is no directory to walk — the file is
    the library, and it must not be recorded as an empty one."""
    lib = proj / "vendor.PcbLib"
    lib.write_bytes(b"\xd0\xcf\x11\xe0 not really a PcbLib")
    _edit(proj / "fp-lib-table", "\n)\n",
          '\n\t(lib (name "vendor") (type "Altium") '
          '(uri "${KIPRJMOD}/vendor.PcbLib") (options "") (descr ""))\n)\n')
    rec = next(r for r in _build(proj)["footprint_libraries"]
               if r["nickname"] == "vendor")
    assert rec["files"] == {"vendor.PcbLib": _sha(lib)}
    assert (rec["status"], rec["path"], rec["models"]) == ("ok", "vendor.PcbLib", [])


def test_unresolvable_and_missing_models_stay_listed(proj):
    mod = proj / "proj.pretty" / "R_0402.kicad_mod"
    _edit(mod, "${KIPRJMOD}/3d/R_0402.step", "${AKCLI_NO_SUCH_VAR}/R_0402.step")
    (proj / "3d" / "PinHeader_1x02.step").unlink()
    models = {m["file"]: m for m in _build(proj)["footprint_libraries"][0]["models"]}
    assert models["R_0402.kicad_mod"] == {
        "file": "R_0402.kicad_mod", "ref": "${AKCLI_NO_SUCH_VAR}/R_0402.step",
        "path": None, "sha256": None, "status": "unresolved"}
    assert models["PinHeader_1x02.kicad_mod"]["status"] == "missing"
    assert models["PinHeader_1x02.kicad_mod"]["sha256"] is None
    assert models["PinHeader_1x02.kicad_mod"]["path"] == "3d/PinHeader_1x02.step"
    assert library_baseline.summary(_build(proj))["models_unresolved"] == 2


def test_kicad_builtin_references_are_recorded_not_hashed(proj, tmp_path, monkeypatch):
    """A ``${KICAD<N>_*_DIR}`` reference is KiCad's own stock file: recorded by
    reference whether or not it resolves here, so the baseline is the same on
    a machine with a different (or no) KiCad."""
    stock = tmp_path / "stock"
    (stock / "3d").mkdir(parents=True)
    _write(stock / "3d" / "R.step", "stock model\n")
    shutil.copytree(proj / "proj.pretty", stock / "Stock.pretty")
    _edit(proj / "proj.pretty" / "R_0402.kicad_mod",
          "${KIPRJMOD}/3d/R_0402.step", "${KICAD99_3DMODEL_DIR}/3d/R.step")
    _edit(proj / "fp-lib-table", "\n)\n",
          '\n\t(lib (name "Stock") (type "KiCad") '
          '(uri "${KICAD99_FOOTPRINT_DIR}/Stock.pretty") (options "") (descr ""))\n)\n')

    def record() -> tuple[dict, dict]:
        libs = {r["nickname"]: r for r in _build(proj)["footprint_libraries"]}
        model = next(m for m in libs["proj"]["models"]
                     if m["file"] == "R_0402.kicad_mod")
        return libs["Stock"], model

    unresolvable = record()
    monkeypatch.setenv("KICAD99_3DMODEL_DIR", str(stock))
    monkeypatch.setenv("KICAD99_FOOTPRINT_DIR", str(stock))
    assert record() == unresolvable                     # same record either way
    lib, model = unresolvable
    assert lib == {"nickname": "Stock", "status": "builtin", "path": None,
                   "uri": "${KICAD99_FOOTPRINT_DIR}/Stock.pretty",
                   "files": {}, "models": []}
    assert model == {"file": "R_0402.kicad_mod", "status": "builtin",
                     "ref": "${KICAD99_3DMODEL_DIR}/3d/R.step",
                     "path": None, "sha256": None}


@pytest.mark.parametrize("ref,builtin", [
    ("${KICAD10_3DMODEL_DIR}/a.step", True),
    ("${KICAD_SYMBOL_DIR}/Device.kicad_sym", True),
    ("${KICAD9_FOOTPRINT_DIR}/R.pretty", True),
    ("${KISYS3DMOD}/Resistor_SMD.3dshapes/R.wrl", True),    # KiCad <= 5 names
    ("${KISYSMOD}/R.pretty", True),
    ("${KIPRJMOD}/3d/a.step", False),                       # the project itself
    ("${MY_LIBS}/a.step", False),
    ("3d/a.step", False),
    ("", False),
])
def test_is_kicad_builtin_ref(ref, builtin):
    assert libtable.is_kicad_builtin_ref(ref) is builtin


def test_library_outside_the_project_is_hashed_with_a_null_path(proj, tmp_path, monkeypatch):
    shared = tmp_path / "shared" / "Team.pretty"
    shutil.copytree(proj / "proj.pretty", shared)
    monkeypatch.setenv("AKCLI_TEAM_LIBS", str(tmp_path / "shared"))
    _edit(proj / "fp-lib-table", "\n)\n",
          '\n\t(lib (name "Team") (type "KiCad") '
          '(uri "${AKCLI_TEAM_LIBS}/Team.pretty") (options "") (descr ""))\n)\n')
    team = next(r for r in _build(proj)["footprint_libraries"]
                if r["nickname"] == "Team")
    assert team["path"] is None and team["status"] == "ok"
    assert team["files"] == {m.name: _sha(m) for m in shared.glob("*.kicad_mod")}
    # the same entry without the variable: recorded, explicitly unresolved
    monkeypatch.delenv("AKCLI_TEAM_LIBS")
    team = next(r for r in _build(proj)["footprint_libraries"]
                if r["nickname"] == "Team")
    assert (team["status"], team["files"], team["path"]) == ("unresolved", {}, None)


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #
def test_file_is_canonical_and_machine_independent(proj, monkeypatch):
    target = proj / DEFAULT
    assert library_baseline.write(target, _build(proj)) is True
    data = target.read_bytes()
    text = data.decode("utf-8")
    assert text.endswith("}\n") and b"\r" not in data
    assert text == library_baseline.dumps(json.loads(text))        # sorted keys
    # nothing machine- or run-specific: no absolute path, no date, no tool version
    assert str(proj) not in text and proj.as_posix() not in text
    assert str(proj.parent) not in text
    for key in ("generated", "timestamp", "date", "akcli_version", "tool"):
        assert f'"{key}"' not in text

    # same bytes again -> nothing is rewritten
    assert library_baseline.write(target, _build(proj)) is False

    # ... and the same bytes however the project is spelled
    monkeypatch.chdir(proj)
    assert library_baseline.dumps(
        library_baseline.build(libtable.discover(Path(".")))) == text
    monkeypatch.chdir(proj.parent)
    assert library_baseline.dumps(
        library_baseline.build(libtable.discover(Path(proj.name)))) == text


def test_table_order_does_not_change_the_baseline(proj):
    before = library_baseline.dumps(_build(proj))
    extra = ('\t(lib (name "aaa") (type "KiCad") (uri "${KIPRJMOD}/proj.pretty")'
             ' (options "") (descr ""))\n')
    table = proj / "fp-lib-table"
    head, tail = table.read_text(encoding="utf-8").split('\t(lib (name "proj")')
    _write(table, head + extra + '\t(lib (name "proj")' + tail)
    first = library_baseline.dumps(_build(proj))
    _write(table, head + '\t(lib (name "proj")' + tail.replace("\n)\n", "\n" + extra + ")\n"))
    assert library_baseline.dumps(_build(proj)) == first != before
    assert [r["nickname"] for r in _build(proj)["footprint_libraries"]] == [
        "aaa", "proj"]


def test_document_validates_against_the_published_schema(proj):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "schemas" / "library-baseline.schema.json")
                        .read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    _edit(proj / "proj.pretty" / "R_0402.kicad_mod",
          "${KIPRJMOD}/3d/R_0402.step", "${AKCLI_NO_SUCH_VAR}/R_0402.step")
    _edit(proj / "fp-lib-table", "\n)\n",
          '\n\t(lib (name "Stock") (type "KiCad") '
          '(uri "${KICAD99_FOOTPRINT_DIR}/Stock.pretty") (options "") (descr ""))\n)\n')
    doc = _build(proj)
    assert [e.message for e in validator.iter_errors(doc)] == []
    # the schema is strict: an unknown key is refused
    doc["footprint_libraries"][0]["mtime"] = 1
    assert list(validator.iter_errors(doc))


# --------------------------------------------------------------------------- #
# compare: each finding code, positive + negative
# --------------------------------------------------------------------------- #
def test_unchanged_project_has_no_differences(proj):
    assert _diff(proj, _build(proj)) == []


def test_drift_when_a_library_file_changes(proj):
    base = _build(proj)
    mod = proj / "proj.pretty" / "R_0402.kicad_mod"
    mod.write_bytes(mod.read_bytes() + b"\n")          # one byte is enough
    (hit,) = _diff(proj, base)
    assert (hit.code, hit.severity.value) == ("LIB_BASELINE_DRIFT", "error")
    assert hit.refs == ["proj", "proj:R_0402.kicad_mod"]
    assert "R_0402.kicad_mod changed since the baseline" in hit.message

    # a line-ending conversion IS drift: the hash is over raw bytes
    sym = proj / "sym" / "Device.kicad_sym"
    sym.write_bytes(sym.read_bytes().replace(b"\n", b"\r\n"))
    codes = _codes(_diff(proj, base))
    assert ("LIB_BASELINE_DRIFT", "error", ["mysym", "mysym:Device.kicad_sym"]) in codes


def test_missing_when_a_baseline_file_is_gone(proj):
    base = _build(proj)
    (proj / "proj.pretty" / "TestPoint.kicad_mod").unlink()
    assert _codes(_diff(proj, base)) == [
        ("LIB_BASELINE_MISSING", "error", ["proj", "proj:TestPoint.kicad_mod"])]


def test_untracked_when_a_file_is_not_in_the_baseline(proj):
    base = _build(proj)
    shutil.copy(proj / "proj.pretty" / "TestPoint.kicad_mod",
                proj / "proj.pretty" / "New.kicad_mod")
    assert _codes(_diff(proj, base)) == [
        ("LIB_BASELINE_UNTRACKED", "warning", ["proj", "proj:New.kicad_mod"])]


def test_model_drift_missing_and_untracked(proj):
    base = _build(proj)
    step = proj / "3d" / "R_0402.step"
    step.write_bytes(step.read_bytes() + b"edited\n")
    (proj / "3d" / "PinHeader_1x02.step").unlink()
    by = {f.refs[1]: f for f in _diff(proj, base)}
    drift = by["proj:R_0402.kicad_mod"]
    assert (drift.code, drift.severity.value) == ("LIB_BASELINE_DRIFT", "error")
    assert "${KIPRJMOD}/3d/R_0402.step" in drift.message
    gone = by["proj:PinHeader_1x02.kicad_mod"]
    assert (gone.code, gone.severity.value) == ("LIB_BASELINE_MISSING", "error")
    assert "gone on disk" in gone.message

    # the reverse: the baseline recorded the model as missing, now it exists
    base_without = _build(proj)
    _write(proj / "3d" / "PinHeader_1x02.step", "back\n")
    assert _codes(_diff(proj, base_without)) == [
        ("LIB_BASELINE_UNTRACKED", "warning",
         ["proj", "proj:PinHeader_1x02.kicad_mod"])]


def test_edited_footprint_does_not_double_report_its_models(proj):
    base = _build(proj)
    _edit(proj / "proj.pretty" / "R_0402.kicad_mod",
          "${KIPRJMOD}/3d/R_0402.step", "${KIPRJMOD}/3d/other.step")
    assert _codes(_diff(proj, base)) == [
        ("LIB_BASELINE_DRIFT", "error", ["proj", "proj:R_0402.kicad_mod"])]


def test_uri_drift_and_nickname_rename(proj):
    base = _build(proj)
    shutil.move(str(proj / "proj.pretty"), str(proj / "moved.pretty"))
    _edit(proj / "fp-lib-table", "${KIPRJMOD}/proj.pretty", "${KIPRJMOD}/moved.pretty")
    (hit,) = _diff(proj, base)
    assert (hit.code, hit.refs) == ("LIB_BASELINE_DRIFT", ["proj"])
    assert "uri changed" in hit.message and "moved.pretty" in hit.message

    base = _build(proj)
    _edit(proj / "fp-lib-table", '(name "proj")', '(name "renamed")')
    hit, dangling = _diff(proj, base)
    assert (hit.code, hit.severity.value) == ("LIB_BASELINE_DRIFT", "error")
    assert hit.refs == ["proj", "renamed"]
    assert "'proj' is now registered as 'renamed'" in hit.message
    # ... and the schematic still says `proj:`, which no project table
    # registers any more: a new, unpinned nickname
    assert (dangling.code, dangling.severity.value, dangling.refs) == (
        "LIB_BASELINE_UNTRACKED", "warning", ["proj"])


def test_library_added_to_or_removed_from_the_table(proj):
    base = _build(proj)
    (proj / "sym-lib-table").unlink()
    assert _codes(_diff(proj, base)) == [
        ("LIB_BASELINE_MISSING", "error", ["mysym"])]

    _write(proj / "sym-lib-table", _SYM_TABLE)
    base = _build(proj)
    _edit(proj / "sym-lib-table", "\n)\n",
          '\n\t(lib (name "second") (type "KiCad") '
          '(uri "${KIPRJMOD}/sym/Device.kicad_sym") (options "") (descr ""))\n)\n')
    assert _codes(_diff(proj, base)) == [
        ("LIB_BASELINE_UNTRACKED", "warning", ["second"])]


def test_library_that_stops_resolving_is_one_missing_finding(proj):
    base = _build(proj)
    shutil.rmtree(proj / "proj.pretty")
    (hit,) = _diff(proj, base)
    assert (hit.code, hit.severity.value, hit.refs) == (
        "LIB_BASELINE_MISSING", "error", ["proj"])
    assert "4 baseline files cannot be verified" in hit.message


def test_global_library_set_changes(proj):
    base = _build(proj)
    _edit(proj / "board.kicad_sch", '"Resistor_SMD:R_0402_1005Metric"',
          '"Capacitor_SMD:C_0402"')
    assert _codes(_diff(proj, base)) == [
        ("LIB_BASELINE_MISSING", "error", ["Resistor_SMD"]),
        ("LIB_BASELINE_UNTRACKED", "warning", ["Capacitor_SMD"]),
    ]


# --------------------------------------------------------------------------- #
# load: a baseline that cannot be trusted is an error, never "no drift"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,needle", [
    ("{not json", "not valid JSON"),
    ("[]", "root must be a JSON object"),
    ('{"schema_version": "2.0", "symbol_libraries": [], '
     '"footprint_libraries": [], "global_libraries": {"symbol": [], '
     '"footprint": []}}', "schema_version '2.0'"),
    ('{"symbol_libraries": []}', "schema_version None"),
    ('{"schema_version": "1.0", "symbol_libraries": {}, '
     '"footprint_libraries": [], "global_libraries": {"symbol": [], '
     '"footprint": []}}', "'symbol_libraries' must be a list"),
    ('{"schema_version": "1.0", "symbol_libraries": [], '
     '"footprint_libraries": [{"nickname": "x"}], "global_libraries": '
     '{"symbol": [], "footprint": []}}', "footprint_libraries[0].uri"),
    ('{"schema_version": "1.0", "symbol_libraries": [], '
     '"footprint_libraries": []}', "'global_libraries'"),
])
def test_load_refuses_a_malformed_baseline(tmp_path, text, needle):
    path = tmp_path / "b.json"
    _write(path, text)
    with pytest.raises(AkcliError) as exc:
        library_baseline.load(path)
    assert exc.value.code == "BAD_CONFIG" and needle in exc.value.message


def test_load_accepts_a_newer_minor_version(proj):
    path = proj / DEFAULT
    doc = _build(proj)
    doc["schema_version"] = "1.7"                       # additive minor bump
    _write(path, library_baseline.dumps(doc))
    assert library_baseline.compare(library_baseline.load(path), _build(proj)) == []


def test_load_missing_file_propagates(tmp_path):
    with pytest.raises(FileNotFoundError):
        library_baseline.load(tmp_path / "nope.json")


# --------------------------------------------------------------------------- #
# CLI: library audit --write-baseline / --baseline
# --------------------------------------------------------------------------- #
def test_cli_write_then_compare(proj, capsys):
    assert main(["library", "audit", str(proj), "--write-baseline", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    target = proj / DEFAULT                      # beside the project, committed
    assert doc["schema_version"] == "1"
    assert doc["baseline"]["path"] == target.as_posix()
    assert doc["baseline"]["action"] == "written"
    assert doc["baseline"]["sha256"] == _sha(target)
    assert (doc["baseline"]["libraries"], doc["baseline"]["files"],
            doc["baseline"]["models_hashed"]) == (2, 5, 2)
    assert not (proj / ".akcli").exists()        # never under the state root
    assert json.loads(target.read_text(encoding="utf-8")) == _build(proj)

    # again: byte-identical, reported as unchanged
    before = target.read_bytes()
    assert main(["library", "audit", str(proj), "--write-baseline"]) == 0
    assert "unchanged" in capsys.readouterr().out
    assert target.read_bytes() == before

    assert main(["library", "audit", str(proj), "--baseline", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["baseline"] == {"path": target.as_posix(), "action": "compared",
                               "sha256": _sha(target), "differences": 0}
    assert doc["findings"] == []
    assert main(["library", "audit", str(proj), "--baseline"]) == 0
    out = capsys.readouterr().out
    assert "compared: matches" in out
    assert "every library file matches the baseline" in out


def test_cli_audit_without_a_flag_never_reads_a_baseline(proj, capsys):
    main(["library", "audit", str(proj), "--write-baseline"])
    mod = proj / "proj.pretty" / "R_0402.kicad_mod"
    mod.write_bytes(mod.read_bytes() + b"\n")
    capsys.readouterr()
    assert main(["library", "audit", str(proj), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "baseline" not in doc and doc["findings"] == []


def test_cli_compare_reports_drift_and_honours_waivers(proj, capsys):
    main(["library", "audit", str(proj), "--write-baseline"])
    mod = proj / "proj.pretty" / "R_0402.kicad_mod"
    mod.write_bytes(mod.read_bytes() + b"\n")
    shutil.copy(proj / "proj.pretty" / "TestPoint.kicad_mod",
                proj / "proj.pretty" / "New.kicad_mod")
    capsys.readouterr()

    assert main(["library", "audit", str(proj), "--baseline", "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["baseline"]["differences"] == 2
    assert [(f["code"], f["severity"], f["refs"]) for f in doc["findings"]] == [
        ("LIB_BASELINE_UNTRACKED", "warning", ["proj", "proj:New.kicad_mod"]),
        ("LIB_BASELINE_DRIFT", "error", ["proj", "proj:R_0402.kicad_mod"]),
    ]
    assert main(["library", "audit", str(proj), "--baseline"]) == 1
    out = capsys.readouterr().out
    assert "compared: 2 difference(s)" in out
    assert "ERROR    LIB_BASELINE_DRIFT" in out

    # --fail-on error: the untracked WARNING alone would not gate
    _waive(proj, 'code = "LIB_BASELINE_DRIFT"\nrefs = ["proj:R_0402.kicad_mod"]\n'
                 'reason = "courtyard touch-up, re-baseline next release"')
    assert main(["library", "audit", str(proj), "--baseline",
                 "--fail-on", "error", "--json"]) == 0
    io = capsys.readouterr()
    doc = json.loads(io.out)
    assert [f["code"] for f in doc["findings"]] == ["LIB_BASELINE_UNTRACKED"]
    assert doc["config_waived"] == "1 (0 demoted)"
    assert "waived: LIB_BASELINE_DRIFT [proj,proj:R_0402.kicad_mod] dropped" in io.err

    # a whole-library waiver covers every file of it
    _waive(proj, 'code = "LIB_BASELINE_*"\nrefs = "proj"')
    assert main(["library", "audit", str(proj), "--baseline", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["findings"] == []


def test_cli_explicit_file_and_config_key(proj, capsys):
    explicit = proj / "release" / "libs.json"
    assert main(["library", "audit", str(proj),
                 "--write-baseline", str(explicit)]) == 0
    assert explicit.is_file() and not (proj / DEFAULT).exists()
    assert main(["library", "audit", str(proj), "--baseline", str(explicit)]) == 0
    capsys.readouterr()

    # [paths].library_baseline is the default FILE for both flags
    _write(proj / "akcli.toml", '[paths]\nlibrary_baseline = "cfg/libs.baseline.json"\n')
    assert main(["library", "audit", str(proj), "--write-baseline", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    configured = proj / "cfg" / "libs.baseline.json"
    assert Path(doc["baseline"]["path"]).resolve() == configured.resolve()
    assert configured.is_file() and not (proj / DEFAULT).exists()
    assert main(["library", "audit", str(proj), "--baseline", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert Path(doc["baseline"]["path"]).resolve() == configured.resolve()


def test_cli_baseline_errors(proj, capsys):
    # missing file: FILE_NOT_FOUND (exit 4), as for a missing --intent
    assert main(["library", "audit", str(proj), "--baseline"]) == 4
    assert main(["library", "audit", str(proj), "--baseline",
                 str(proj / "nope.json"), "--json"]) == 4
    capsys.readouterr()

    bad = proj / "bad.json"
    _write(bad, "{not json")
    assert main(["library", "audit", str(proj), "--baseline", str(bad), "--json"]) == 2
    env = json.loads(capsys.readouterr().out)
    assert env["error"]["code"] == "BAD_CONFIG"

    _write(bad, library_baseline.dumps({**_build(proj), "schema_version": "9.0"}))
    assert main(["library", "audit", str(proj), "--baseline", str(bad)]) == 2
    assert "schema_version '9.0'" in capsys.readouterr().err

    assert main(["library", "audit", str(proj), "--baseline",
                 "--write-baseline"]) == 2
    assert "pass one of them" in capsys.readouterr().err


def test_cli_write_refuses_to_clobber_a_file_that_is_not_a_baseline(proj, capsys):
    """``--write-baseline WORD`` reads WORD as FILE — a project path placed
    after the flag must never be overwritten."""
    pro = proj / "x.kicad_pro"
    _write(pro, '{"meta": {"filename": "x.kicad_pro"}}\n')
    before = pro.read_bytes()
    assert main(["library", "audit", "--write-baseline", str(pro)]) == 2
    err = capsys.readouterr().err
    assert "refusing to overwrite" in err and "put the project first" in err
    assert pro.read_bytes() == before
    assert main(["library", "audit", "--write-baseline", str(proj)]) == 2
    assert "is a directory" in capsys.readouterr().err
    assert not (proj / DEFAULT).exists()


# --------------------------------------------------------------------------- #
# relative project directories (two resolution bugs the baseline exposed)
# --------------------------------------------------------------------------- #
def test_relative_project_dir_resolves_kiprjmod_uris_and_models(proj, monkeypatch, capsys):
    # `akcli library audit proj` from the parent: ${KIPRJMOD}/x must not
    # become proj/proj/x
    monkeypatch.chdir(proj.parent)
    assert main(["library", "audit", proj.name, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["findings"] == []
    # `akcli library audit .`: a ${KIPRJMOD} model must not be looked up
    # inside the .pretty
    monkeypatch.chdir(proj)
    assert main(["library", "audit", ".", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["findings"] == []
    ws = libtable.discover(Path("."))
    (lib,) = libtable.load_libraries(ws, kinds=("fp",))
    assert all(m.path is not None and m.path.is_file()
               for f in lib.footprints for m in f.models)


# --------------------------------------------------------------------------- #
# release preflight: library-baseline + pcb-library gates
# --------------------------------------------------------------------------- #
def _preflight(proj: Path, *extra: str) -> list[str]:
    return ["release", "preflight", "--sch", str(proj / "board.kicad_sch"),
            *extra, "--json"]


def _gates(capsys) -> tuple[dict, dict]:
    doc = json.loads(capsys.readouterr().out)
    return doc, {g["gate"]: g for g in doc["gates"]}


def test_preflight_baseline_gate_skips_with_a_reason_when_absent(proj, capsys):
    main(_preflight(proj))
    doc, by = _gates(capsys)
    gate = by["library-baseline"]
    assert gate["status"] == "skipped"
    assert "--library-baseline" in gate["reason"]
    assert "[paths].library_baseline" in gate["reason"]
    assert "library_baseline" not in doc["inputs"]


def test_preflight_baseline_gate_passes_and_records_the_input(proj, capsys):
    main(["library", "audit", str(proj), "--write-baseline"])
    capsys.readouterr()
    target = proj / DEFAULT
    # found at its default location beside the schematic — no flag needed
    main(_preflight(proj))
    doc, by = _gates(capsys)
    assert by["library-baseline"]["status"] == "pass"
    assert by["library-baseline"]["findings"] == []
    assert doc["inputs"]["library_baseline"] == {"path": str(target),
                                                 "sha256": _sha(target)}
    # an explicit flag names another file
    other = proj / "other.json"
    shutil.copy(target, other)
    main(_preflight(proj, "--library-baseline", str(other)))
    doc, by = _gates(capsys)
    assert by["library-baseline"]["status"] == "pass"
    assert doc["inputs"]["library_baseline"]["path"] == str(other)


def test_preflight_baseline_gate_fails_on_drift_and_honours_waivers(proj, capsys):
    main(["library", "audit", str(proj), "--write-baseline"])
    mod = proj / "proj.pretty" / "R_0402.kicad_mod"
    mod.write_bytes(mod.read_bytes() + b"\n")
    capsys.readouterr()
    assert main(_preflight(proj)) == 1
    doc, by = _gates(capsys)
    assert doc["result"] == "FAIL"
    gate = by["library-baseline"]
    assert gate["status"] == "fail"
    assert [f["code"] for f in gate["findings"]] == ["LIB_BASELINE_DRIFT"]

    _waive(proj, 'code = "LIB_BASELINE_DRIFT"\nrefs = ["proj:R_0402.kicad_mod"]')
    main(_preflight(proj))
    _doc, by = _gates(capsys)
    assert by["library-baseline"]["status"] == "pass"


def test_preflight_baseline_gate_config_key_and_hard_errors(proj, capsys):
    cfg_target = proj / "cfg.baseline.json"
    _write(proj / "akcli.toml", '[paths]\nlibrary_baseline = "cfg.baseline.json"\n')
    # a NAMED baseline that does not exist is an error, not a skip
    assert main(_preflight(proj)) == 4
    capsys.readouterr()
    library_baseline.write(cfg_target, _build(proj))
    main(_preflight(proj))
    doc, by = _gates(capsys)
    assert by["library-baseline"]["status"] == "pass"
    assert Path(doc["inputs"]["library_baseline"]["path"]).resolve() == cfg_target.resolve()

    _write(cfg_target, "{not json")
    assert main(_preflight(proj)) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "BAD_CONFIG"


def test_preflight_pcb_library_gate(proj, capsys):
    main(_preflight(proj))
    _doc, by = _gates(capsys)
    assert by["pcb-library"] == {"gate": "pcb-library", "status": "skipped",
                                 "reason": "no --pcb board given", "findings": []}

    main(_preflight(proj, "--pcb", str(proj / BOARD)))
    _doc, by = _gates(capsys)
    gate = by["pcb-library"]
    assert gate["status"] == "pass"                 # only NOTE-level model notes
    assert {f["code"] for f in gate["findings"]} == {"FPLIB_MODEL_MISSING"}
    assert gate["coverage"]["compared"] == 5 and gate["coverage"]["not_compared"] == 1
    assert gate["summary"] == (
        "5 of 6 board footprint(s) compared against the project fp-lib-table; "
        "1 NOT compared (1 library only in the global fp-lib-table)")
    assert by["sch-pcb"]["status"] == "pass"

    # a board edit drifts from the library: its own gate fails, sch-pcb does not
    _edit(proj / BOARD, "(drill 1)", "(drill 0.8)")
    assert main(_preflight(proj, "--pcb", str(proj / BOARD))) == 1
    _doc, by = _gates(capsys)
    assert by["pcb-library"]["status"] == "fail"
    assert by["sch-pcb"]["status"] == "pass"
    assert "FPLIB_PAD_DRILL_DRIFT" in {f["code"] for f in by["pcb-library"]["findings"]}

    _waive(proj, 'code = "FPLIB_PAD_DRILL_DRIFT"\nrefs = ["J1"]')
    main(_preflight(proj, "--pcb", str(proj / BOARD)))
    _doc, by = _gates(capsys)
    assert by["pcb-library"]["status"] == "pass"


def test_preflight_pcb_library_gate_text_and_no_scope_skip(proj, capsys):
    argv = _preflight(proj, "--pcb", str(proj / BOARD))[:-1]        # text mode
    main(argv)
    out = capsys.readouterr().out
    assert "pcb-library" in out
    assert "5 of 6 board footprint(s) compared" in out

    # no project fp-lib-table: nothing can be compared -> skipped, with counts
    (proj / "fp-lib-table").unlink()
    main(_preflight(proj, "--pcb", str(proj / BOARD)))
    _doc, by = _gates(capsys)
    gate = by["pcb-library"]
    assert gate["status"] == "skipped"
    assert "0 of 6 board footprint(s) compared" in gate["reason"]
