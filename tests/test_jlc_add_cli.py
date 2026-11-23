"""CLI-layer tests for `akcli jlc add` (offline; captured EasyEDA fixtures).

Complements ``test_jlc2kicad.py`` (driver layer): these run the real argparse
handler through ``cli.main`` — flag validation, exit-code mapping, and the
``--place`` op-list emission.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from akcli import cli
from akcli.commands import jlc as jlc_cmd
from akcli._vendor.jlc2kicadlib import _http

FIX = Path(__file__).parent / "fixtures" / "jlc"


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body, self.status = body, status

    def read(self, n=-1):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FixtureOpener:
    def __init__(self):
        self.by_uuid = {
            p.stem.rsplit("_", 1)[-1]: p.read_bytes()
            for p in FIX.glob("C25804_*_*.json")
        }

    def open(self, request, *, timeout=None):
        url = request.full_url
        if url.endswith("/svgs"):
            return _FakeResp((FIX / "C25804_svgs.json").read_bytes())
        for uuid, body in self.by_uuid.items():
            if url.endswith(uuid):
                return _FakeResp(body)
        return _FakeResp(b"", status=404)


@pytest.fixture()
def offline(monkeypatch):
    monkeypatch.setattr(_http, "opener", _FixtureOpener())
    # jlcsearch enrichment is a separate network path — keep the test offline.
    monkeypatch.setattr(jlc_cmd, "_easyeda_enrich", lambda lcsc: None)


def test_add_with_place_emits_oplist(tmp_path, offline, capsys):
    out = tmp_path / "lib"
    rc = cli.main(["jlc", "add", "C25804", "--out", str(out),
                   "--place", "--designator", "R1", "--at", "2000", "1000"])
    assert rc == 0
    place = json.loads((out / "place.json").read_text())
    (op,) = place["ops"]
    assert op["op"] == "place_component"
    assert op["designator"] == "R1"
    assert op["lib_id"].startswith("akcli:")
    assert op["footprint"].startswith("footprint:")
    assert (op["x_mil"], op["y_mil"]) == (2000.0, 1000.0)
    assert (out / "symbol" / "akcli.kicad_sym").is_file()


def test_place_requires_designator_and_at(tmp_path, offline):
    assert cli.main(["jlc", "add", "C25804", "--out", str(tmp_path / "a"),
                     "--place", "--at", "1", "1"]) == 2
    assert cli.main(["jlc", "add", "C25804", "--out", str(tmp_path / "b"),
                     "--place", "--designator", "R1"]) == 2


def test_missing_cnumber_is_usage_error(offline):
    assert cli.main(["jlc", "add"]) == 2


def test_part_not_found_exits_4(tmp_path, monkeypatch):
    class _NotFound:
        def open(self, request, *, timeout=None):
            return _FakeResp(b'{"success": false, "result": []}')

    monkeypatch.setattr(_http, "opener", _NotFound())
    monkeypatch.setattr(jlc_cmd, "_easyeda_enrich", lambda lcsc: None)
    assert cli.main(["jlc", "add", "C999999999", "--out", str(tmp_path)]) == 4


def test_network_error_exits_7(tmp_path, monkeypatch):
    class _Down:
        def open(self, request, *, timeout=None):
            raise OSError("no route")

    monkeypatch.setattr(_http, "opener", _Down())
    monkeypatch.setattr(jlc_cmd, "_easyeda_enrich", lambda lcsc: None)
    assert cli.main(["jlc", "add", "C25804", "--out", str(tmp_path)]) == 7


def test_place_oplist_value_is_the_part_value_not_the_mpn(tmp_path, offline, capsys):
    """The op-list Value must be the converted symbol's Value ("10k"), which the
    library and the placed instance then agree on."""
    out = tmp_path / "lib"
    assert cli.main(["jlc", "add", "C25804", "--out", str(out), "--place",
                     "--designator", "R1", "--at", "0", "0"]) == 0
    (op,) = json.loads((out / "place.json").read_text())["ops"]
    assert op["value"] == "10k"


def test_place_hint_prints_a_draw_command_that_actually_works(tmp_path, offline,
                                                             capsys):
    """The printed hint used to omit --symbols, so following it was a guaranteed
    exit 6 (SYMBOL_NOT_FOUND): the symbol only exists in the fresh library."""
    import shlex

    out = tmp_path / "lib"
    assert cli.main(["jlc", "add", "C25804", "--out", str(out), "--place",
                     "--designator", "R1", "--at", "2000", "1000"]) == 0
    hint = next(ln for ln in capsys.readouterr().out.splitlines()
                if "apply with:" in ln)
    assert "--symbols" in hint
    sym = str(out / "symbol" / "akcli.kicad_sym")
    assert sym in hint

    target = tmp_path / "sheet.kicad_sch"
    assert cli.main(["new", str(target)]) == 0
    argv = shlex.split(hint.split("apply with:", 1)[1].strip(),
                       posix=(os.name != "nt"))[1:]      # drop the "akcli" word
    argv = [str(target) if a == "<target.kicad_sch>" else a for a in argv]
    assert cli.main(argv) == 0
    text = target.read_text(encoding="utf-8")
    assert '"R1"' in text
    assert '(property "Value" "10k"' in text


def test_converted_passive_pins_keep_erc_clean(tmp_path, offline, capsys):
    """End of the chain the pin-type fix exists for: two converted resistors
    wired together must NOT raise ERC_FLOATING_INPUT (they did as `input` pins)."""
    out = tmp_path / "lib"
    assert cli.main(["jlc", "add", "C25804", "--out", str(out)]) == 0
    sym = out / "symbol" / "akcli.kicad_sym"
    lib_id = "akcli:0603WAF1002T5E"

    target = tmp_path / "sheet.kicad_sch"
    assert cli.main(["new", str(target)]) == 0
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({"protocol_version": 2, "target_format": "kicad", "ops": [
        {"op": "place_component", "lib_id": lib_id, "designator": "R1",
         "x_mil": 2000, "y_mil": 1000},
        {"op": "place_component", "lib_id": lib_id, "designator": "R2",
         "x_mil": 2000, "y_mil": 2000},
        {"op": "add_wire", "vertices": ["R1.2", "R2.1"]},
    ]}), encoding="utf-8", newline="\n")
    assert cli.main(["draw", str(target), "--ops", str(ops),
                     "--symbols", str(sym), "--apply"]) == 0
    capsys.readouterr()

    cli.main(["check", str(target), "--erc", "--json", "--exit-zero"])
    codes = {f["code"] for f in json.loads(capsys.readouterr().out)["findings"]}
    assert "ERC_FLOATING_INPUT" not in codes

    # ... and the rule really is pin-type gated: forcing the cached pins back to
    # `input` (the pre-fix conversion output) reproduces the finding.
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("(pin passive line", "(pin input line"),
                      encoding="utf-8", newline="\n")
    cli.main(["check", str(target), "--erc", "--json", "--exit-zero"])
    codes = {f["code"] for f in json.loads(capsys.readouterr().out)["findings"]}
    assert "ERC_FLOATING_INPUT" in codes


# --------------------------------------------------------------------------- #
# GUI-open guard: an existing library file KiCad holds open is not rewritten
# --------------------------------------------------------------------------- #
class _NoNetwork:
    """Opener for the refusal path: the lock must stop the add BEFORE any fetch."""

    def open(self, request, *, timeout=None):
        raise AssertionError(f"network touched before the refusal: {request.full_url}")


def _lock(path: Path) -> Path:
    from akcli.safety import gui_lock_path

    lck = gui_lock_path(path)
    lck.write_text("{}", encoding="utf-8", newline="\n")
    return lck


@pytest.fixture()
def converted(tmp_path, offline, capsys) -> Path:
    """An output dir that already holds the converted C25804 library."""
    out = tmp_path / "lib"
    assert cli.main(["jlc", "add", "C25804", "--out", str(out)]) == 0
    capsys.readouterr()
    return out


def test_add_refuses_when_the_symbol_library_is_gui_locked(converted, monkeypatch,
                                                           capsys):
    # A second add rewrites symbol/akcli.kicad_sym in place (appends, or
    # replaces under --force) — with or without --force.
    sym = converted / "symbol" / "akcli.kicad_sym"
    before = sym.read_bytes()
    lck = _lock(sym)
    monkeypatch.setattr(_http, "opener", _NoNetwork())

    for extra in ([], ["--force"]):
        rc = cli.main(["jlc", "add", "C25804", "--out", str(converted),
                       *extra, "--json"])
        io = capsys.readouterr()
        assert rc == 6, io.err
        err = json.loads(io.out)["error"]
        assert err["code"] == "TARGET_LOCKED" and err["exit"] == 6
        assert lck.name in err["message"]
        assert "--allow-open" in err["remediation"]
        assert "TARGET_LOCKED" in io.err
        assert sym.read_bytes() == before                # nothing rewritten


def test_add_allow_open_writes_under_the_lock(converted, capsys):
    sym = converted / "symbol" / "akcli.kicad_sym"
    _lock(sym)
    rc = cli.main(["jlc", "add", "C25804", "--out", str(converted),
                   "--force", "--allow-open", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0 and doc["error_code"] is None
    assert str(sym) in doc["artifacts"]


def test_add_footprint_lock_only_matters_when_footprints_are_rewritten(
        converted, capsys):
    (mod,) = (converted / "footprint").glob("*.kicad_mod")
    before = mod.read_bytes()
    _lock(mod)
    # without --force an existing footprint is skipped, not rewritten
    assert cli.main(["jlc", "add", "C25804", "--out", str(converted)]) == 0
    capsys.readouterr()
    # --force overwrites it, --3d-path absolute re-points its model path
    for extra in (["--force"], ["--3d-path", "absolute"]):
        rc = cli.main(["jlc", "add", "C25804", "--out", str(converted), *extra])
        assert rc == 6, extra
        assert "TARGET_LOCKED" in capsys.readouterr().err
    assert mod.read_bytes() == before


def test_add_into_a_fresh_dir_ignores_a_stray_lock(tmp_path, offline, capsys):
    # Only an EXISTING file can be open in KiCad: a leftover lock next to a
    # library that is not there yet must not block the first conversion.
    out = tmp_path / "lib"
    (out / "symbol").mkdir(parents=True)
    _lock(out / "symbol" / "akcli.kicad_sym")
    assert cli.main(["jlc", "add", "C25804", "--out", str(out)]) == 0
    assert (out / "symbol" / "akcli.kicad_sym").is_file()


# --------------------------------------------------------------------------- #
# footprint-library nickname: --footprint-lib > the project fp-lib-table >
# the historic 'footprint' default (with the table line to add on stderr)
# --------------------------------------------------------------------------- #
_LIB = '(lib (name "{name}")(type "KiCad")(uri "{uri}")(options "")(descr ""))'


def _project(tmp_path: Path, *libs: tuple[str, str]) -> Path:
    """A KiCad project whose fp-lib-table registers ``libs`` (name, uri);
    returns the `jlc add --out` directory inside it (``libs/jlc``)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "board.kicad_pro").write_text("{}\n", encoding="utf-8", newline="\n")
    rows = "".join(f"  {_LIB.format(name=n, uri=u)}\n" for n, u in libs)
    (proj / "fp-lib-table").write_text(
        f"(fp_lib_table\n  (version 7)\n{rows})\n", encoding="utf-8", newline="\n")
    return proj / "libs" / "jlc"


def _add(out: Path, capsys, *extra: str) -> tuple[dict, str]:
    rc = cli.main(["jlc", "add", "C25804", "--out", str(out), "--json", *extra])
    io = capsys.readouterr()
    assert rc == 0, io.err
    return json.loads(io.out), io.err


def _footprint_field(out: Path) -> str:
    text = (out / "symbol" / "akcli.kicad_sym").read_text(encoding="utf-8")
    (field,) = re.findall(r'\(property "Footprint" "([^"]*)"', text)
    return field


_OWN_DIR = "${KIPRJMOD}/libs/jlc/footprint"       # where a default add writes


def test_footprint_lib_flag_wins_over_the_table(tmp_path, offline, capsys):
    out = _project(tmp_path, ("proj_jlc", _OWN_DIR))
    doc, err = _add(out, capsys, "--footprint-lib", "mine")
    assert (doc["footprint_lib"], doc["footprint_lib_source"]) == ("mine", "flag")
    assert _footprint_field(out).startswith("mine:")
    # the flag still names the directory too — the table is not consulted
    assert list((out / "mine").glob("*.kicad_mod"))
    assert not (out / "footprint").exists()
    assert "nickname" not in err


@pytest.mark.parametrize("uri", [
    _OWN_DIR,                                     # the portable spelling
    "${KIPRJMOD}/libs/../libs/jlc/footprint/",    # same directory, unnormalized
    "libs/jlc/footprint",                         # project-relative, no variable
    "<abs>",                                      # this machine's absolute path
])
def test_single_matching_table_entry_supplies_the_nickname(tmp_path, offline,
                                                           capsys, uri):
    if uri == "<abs>":
        uri = (tmp_path / "proj" / "libs" / "jlc" / "footprint").as_posix()
    out = _project(tmp_path, ("Resistor_SMD", "${KICAD9_FOOTPRINT_DIR}/R.pretty"),
                   ("proj_jlc", uri))
    table = out.parents[1] / "fp-lib-table"
    before = table.read_bytes()

    doc, err = _add(out, capsys, "--place", "--designator", "R1", "--at", "0", "0")
    assert doc["footprint_lib"] == "proj_jlc"
    assert doc["footprint_lib_source"] == "fp-lib-table"
    # the nickname moved; the files did not
    (mod,) = (out / "footprint").glob("*.kicad_mod")
    assert _footprint_field(out) == f"proj_jlc:{mod.stem}"
    # --place agrees with the library symbol
    (op,) = doc["place"]["ops"]
    assert op["footprint"] == f"proj_jlc:{mod.stem}"
    assert json.loads((out / "place.json").read_text(encoding="utf-8")) == doc["place"]
    assert "taken from" in err and "proj_jlc" in err
    assert table.read_bytes() == before           # a lib table is never edited

    # ... and KiCad-side resolution is real: the project audit finds the
    # footprint under that nickname (no FOOTPRINT_LIB_UNREGISTERED/_MISSING).
    from akcli import libtable
    ws = libtable.discover(out.parents[1])
    assert ws.locate_fp_nick("proj_jlc") == "project"
    assert libtable.nicknames_for_dir(ws.fp_table, out / "footprint") == ["proj_jlc"]


@pytest.mark.parametrize("libs", [
    (),                                           # an empty table
    (("other", "${KIPRJMOD}/libs/other.pretty"),),
    # an entry for a PARENT directory does not serve a flat .pretty library
    (("parent", "${KIPRJMOD}/libs/jlc"),),
    (("unresolved", "${AKCLI_TEST_NO_SUCH_VAR}/libs/jlc/footprint"),),
])
def test_no_matching_entry_keeps_the_default_and_prints_the_line_to_add(
        tmp_path, offline, capsys, libs):
    out = _project(tmp_path, *libs)
    table = out.parents[1] / "fp-lib-table"
    before = table.read_bytes()

    doc, err = _add(out, capsys)
    assert (doc["footprint_lib"], doc["footprint_lib_source"]) == ("footprint", "default")
    assert _footprint_field(out).startswith("footprint:")
    assert _LIB.format(name="footprint", uri=_OWN_DIR) in err
    assert str(table.resolve()) in err            # names the file to edit
    assert table.read_bytes() == before

    # the printed line is the fix: once it is in the table the add resolves
    table.write_text(
        before.decode("utf-8").replace(
            "(version 7)\n",
            "(version 7)\n  " + _LIB.format(name="footprint", uri=_OWN_DIR) + "\n"),
        encoding="utf-8", newline="\n")
    doc, err = _add(out, capsys)
    assert doc["footprint_lib_source"] == "fp-lib-table"
    assert "(lib " not in err


def test_several_matching_entries_keep_the_default_and_list_the_candidates(
        tmp_path, offline, capsys):
    out = _project(tmp_path, ("jlc_a", _OWN_DIR), ("jlc_b", "libs/jlc/footprint"))
    doc, err = _add(out, capsys)
    assert (doc["footprint_lib"], doc["footprint_lib_source"]) == ("footprint", "default")
    assert _footprint_field(out).startswith("footprint:")
    assert "2 nicknames (jlc_a, jlc_b)" in err
    assert "--rename-footprint-lib footprint=jlc_a" in err
    assert "(lib " not in err                     # nothing to add: pick one


def test_several_matches_including_the_default_need_no_hint(tmp_path, offline,
                                                            capsys):
    out = _project(tmp_path, ("jlc_a", _OWN_DIR), ("footprint", _OWN_DIR))
    doc, err = _add(out, capsys)
    assert (doc["footprint_lib"], doc["footprint_lib_source"]) == ("footprint", "default")
    assert "nickname" not in err                  # 'footprint:' already resolves


def test_default_nickname_registered_elsewhere_suggests_a_free_one(tmp_path,
                                                                   offline, capsys):
    # 'footprint' is taken by another library, so adding a second 'footprint'
    # line would be a duplicate nickname — the hint names a free one instead.
    out = _project(tmp_path, ("footprint", "${KIPRJMOD}/libs/other"),
                   ("jlc", "${KIPRJMOD}/libs/third"))
    doc, err = _add(out, capsys)
    assert (doc["footprint_lib"], doc["footprint_lib_source"]) == ("footprint", "default")
    assert _LIB.format(name="jlc_2", uri=_OWN_DIR) in err
    assert "--force" in err


def test_no_project_keeps_the_default_with_an_absolute_uri(tmp_path, offline,
                                                           capsys):
    out = tmp_path / "lib"
    doc, err = _add(out, capsys)
    assert (doc["footprint_lib"], doc["footprint_lib_source"]) == ("footprint", "default")
    uri = (out / "footprint").resolve().as_posix()
    assert _LIB.format(name="footprint", uri=uri) in err


def test_project_without_a_table_gets_a_kiprjmod_relative_line(tmp_path, offline,
                                                               capsys):
    out = _project(tmp_path)
    (out.parents[1] / "fp-lib-table").unlink()    # .kicad_pro only
    doc, err = _add(out, capsys)
    assert doc["footprint_lib_source"] == "default"
    assert "has no fp-lib-table" in err
    assert _LIB.format(name="footprint", uri=_OWN_DIR) in err


def test_unreadable_table_costs_the_lookup_not_the_add(tmp_path, offline, capsys):
    out = _project(tmp_path, ("proj_jlc", _OWN_DIR))
    (out.parents[1] / "fp-lib-table").write_text(
        "(fp_lib_table (lib (name", encoding="utf-8", newline="\n")
    doc, err = _add(out, capsys)
    assert doc["error_code"] is None
    assert (doc["footprint_lib"], doc["footprint_lib_source"]) == ("footprint", "default")
    assert "could not be read" in err
