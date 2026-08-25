"""``akcli.readers.kicad_pro`` — the degrade-only ``.kicad_pro`` reader.

The contract this file exists to pin is a NEGATIVE one: **read() never
raises**. Every caller is on a path that must survive a hand-edited or
truncated project file — ``resolve_root`` uses it as a tie-break (a bad file
must fall back to the stem rule, not turn ``akcli read`` into a parse error)
and ``project check`` exists to REPORT the damage, so a reader that threw
would take out both. Each malformed shape below therefore asserts
``ok is False`` plus a non-empty ``error``, never ``pytest.raises``.
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli import safety
from akcli.readers import kicad_pro as kpro

_ROOT_UUID = "11111111-2222-4333-8444-555555555555"

_FULL = {
    "libraries": {
        "pinned_footprint_libs": ["proj_fp"],
        "pinned_symbol_libs": ["proj_sym", "extra"],
    },
    "meta": {"filename": "demo.kicad_pro", "version": 1},
    "net_settings": {"classes": [{"name": "Default"}, {"name": "Power"}]},
    "sheets": [[_ROOT_UUID, "Root"], ["66666666-7777-4888-8999-aaaaaaaaaaaa", "Power"]],
    "text_variables": {"REV": "A2", "BOARD": "demo"},
}


def _write(tmp_path: Path, body: str, name: str = "demo.kicad_pro") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8", newline="\n")
    return p


def test_read_extracts_every_field(tmp_path):
    pro = kpro.read(_write(tmp_path, json.dumps(_FULL)))
    assert pro.ok is True and pro.error is None
    assert pro.meta_filename == "demo.kicad_pro" and pro.version == 1
    assert pro.sheets == [(_ROOT_UUID, "Root"),
                          ("66666666-7777-4888-8999-aaaaaaaaaaaa", "Power")]
    assert pro.text_variables == {"REV": "A2", "BOARD": "demo"}
    assert pro.net_classes == ["Default", "Power"]
    assert pro.pinned_symbol_libs == ["proj_sym", "extra"]
    assert pro.pinned_footprint_libs == ["proj_fp"]
    assert kpro.root_sheet_uuid(pro) == _ROOT_UUID


def test_read_ignores_unknown_sections(tmp_path):
    """A newer KiCad's extra sections must not make the file unreadable."""
    doc = dict(_FULL, cvpcb={"equivalence_files": []}, schematic={"legacy": 1},
               future_section_akcli_never_heard_of=[1, 2, 3])
    pro = kpro.read(_write(tmp_path, json.dumps(doc)))
    assert pro.ok is True and kpro.root_sheet_uuid(pro) == _ROOT_UUID


def test_read_degrades_field_by_field(tmp_path):
    """One wrong-typed field must not cost the others."""
    doc = {"meta": "not-an-object", "sheets": [[_ROOT_UUID, "Root"], "junk",
                                               [1, 2], [_ROOT_UUID]],
           "text_variables": ["not", "a", "map"],
           "net_settings": {"classes": "nope"},
           "libraries": {"pinned_symbol_libs": ["a", 7, None]}}
    pro = kpro.read(_write(tmp_path, json.dumps(doc)))
    assert pro.ok is True
    assert pro.meta_filename is None and pro.version is None
    assert pro.sheets == [(_ROOT_UUID, "Root")]      # bad rows dropped, not fatal
    assert pro.text_variables == {} and pro.net_classes == []
    assert pro.pinned_symbol_libs == ["a"]           # non-strings dropped


def test_read_rejects_a_true_version_without_crashing(tmp_path):
    """`bool` is an `int` subclass — a JSON `true` is not a schema version."""
    pro = kpro.read(_write(tmp_path, json.dumps({"meta": {"version": True}})))
    assert pro.ok is True and pro.version is None


def test_read_of_a_missing_file_is_not_ok(tmp_path):
    pro = kpro.read(tmp_path / "nope.kicad_pro")
    assert pro.ok is False and pro.error
    assert kpro.root_sheet_uuid(pro) is None
    # the empty defaults are still readable: callers branch on `ok`, not on
    # having to guard every attribute
    assert pro.sheets == [] and pro.text_variables == {}


def test_read_of_truncated_json_is_not_ok(tmp_path):
    pro = kpro.read(_write(tmp_path, '{"sheets": [["' + _ROOT_UUID))
    assert pro.ok is False and "JSON" in (pro.error or "")


def test_read_of_a_json_array_is_not_ok(tmp_path):
    pro = kpro.read(_write(tmp_path, "[1, 2, 3]"))
    assert pro.ok is False and "project object" in (pro.error or "")


def test_read_of_a_directory_is_not_ok(tmp_path):
    (tmp_path / "d.kicad_pro").mkdir()
    pro = kpro.read(tmp_path / "d.kicad_pro")
    assert pro.ok is False and pro.error


def test_read_of_undecodable_bytes_is_not_ok(tmp_path):
    p = tmp_path / "demo.kicad_pro"
    p.write_bytes(b'{"meta": "\xff\xfe"}')
    pro = kpro.read(p)
    assert pro.ok is False and "UTF-8" in (pro.error or "")


def test_read_refuses_an_oversized_file_by_stat(tmp_path, monkeypatch):
    """The size guard runs on `stat`, so a hostile file is never loaded."""
    p = _write(tmp_path, json.dumps(_FULL))
    monkeypatch.setattr(safety, "MAX_FILE_BYTES", 4)
    pro = kpro.read(p)
    assert pro.ok is False and "read cap" in (pro.error or "")


def test_root_sheet_uuid_is_none_without_sheets(tmp_path):
    assert kpro.root_sheet_uuid(kpro.read(_write(tmp_path, "{}"))) is None
    empty = kpro.read(_write(tmp_path, json.dumps({"sheets": [["", "Root"]]})))
    assert kpro.root_sheet_uuid(empty) is None


def test_find_accepts_a_directory_or_the_file(tmp_path):
    pro = _write(tmp_path, json.dumps(_FULL))
    assert kpro.find(tmp_path) == pro
    assert kpro.find(pro) == pro


def test_find_returns_none_for_a_non_project(tmp_path):
    (tmp_path / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8",
                                              newline="\n")
    assert kpro.find(tmp_path) is None
    assert kpro.find(tmp_path / "board.kicad_sch") is None
    assert kpro.find(tmp_path / "does-not-exist") is None


def test_find_is_deterministic_with_several_projects(tmp_path):
    """Sorted, not arbitrary — so two runs (and `libtable`) agree."""
    _write(tmp_path, "{}", "zeta.kicad_pro")
    first = _write(tmp_path, "{}", "alpha.kicad_pro")
    assert kpro.find(tmp_path) == first
