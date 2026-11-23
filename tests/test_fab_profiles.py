"""Bundled fab profiles — the packaged mirror, ``--profile <id>`` resolution,
``akcli fab profiles`` and the ``FAB_PROFILE_STALE`` note.

What is pinned here:

* the packaged ``akcli.fab_profiles`` mirror is byte-identical to the fab
  profiles under ``examples/fab`` (and holds nothing else — the example order
  manifest is not a profile), and ``tools/examples_mirror.py`` both detects and
  repairs drift and orphans;
* ``fab.resolve_profile``: an existing FILE beats a bundled id, an id or file
  stem selects a bundled profile, anything else is ``BAD_CONFIG`` naming every
  bundled id;
* a bundled profile more than twelve months past its ``retrieved_at`` raises
  exactly ONE note, which the default exit policy does not fail on — and a
  profile FILE never raises it.

Every staleness assertion injects ``today``: the bundled profile's own date
would otherwise turn these tests red on a calendar day, not on a code change.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

from akcli import fab, fab_profiles
from akcli.cli import main
from akcli.errors import AkcliError
from akcli.readers import kicad
from tests._cli_harness import json_of, run_cli

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_FAB = ROOT / "examples" / "fab"

# A board that fits the bundled JLC 4-layer profile exactly: nothing to report
# but what the test itself provokes.
_CLEAN_BOARD = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (general (thickness 1.0))
  (layers
    (0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" signal) (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 30 0) (end 30 20) (layer "Edge.Cuts") (width 0.1))
)"""

_LOCAL_PROFILE = """
id = "local-override"
vendor = "ACME"
[source]
urls = ["https://example.com/rules"]
retrieved_at = "2020-01-01"
"""


def _canonical_profiles() -> dict[str, bytes]:
    """``examples/fab`` TOMLs that ARE fab profiles (id + [source])."""
    out: dict[str, bytes] = {}
    for path in sorted(EXAMPLES_FAB.glob("*.toml")):
        data = path.read_bytes()
        doc = tomllib.loads(data.decode("utf-8"))
        if doc.get("id") and isinstance(doc.get("source"), dict):
            out[path.name] = data
    return out


def _bundled_ids() -> list[str]:
    return [doc["id"] for doc, _origin in fab.bundled_profiles()]


@pytest.fixture()
def board(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_CLEAN_BOARD, encoding="utf-8", newline="\n")
    return p


@pytest.fixture()
def bundled():
    """The first bundled ``(profile, origin)`` — there is always at least one."""
    return fab.bundled_profiles()[0]


# --------------------------------------------------------------------------- #
# the packaged mirror
# --------------------------------------------------------------------------- #
def test_packaged_profile_mirror_matches_examples_fab():
    """``src/akcli/fab_profiles/*.toml`` is byte-identical to ``examples/fab``.

    The failure this prevents is invisible from the repo: tests here read
    ``examples/fab`` while every installed wheel would check boards against
    yesterday's numbers. Refresh with ``python3 tools/examples_mirror.py
    build``.
    """
    canonical = _canonical_profiles()
    assert canonical, "examples/fab lost its fab profiles"
    mirror = {name: fab_profiles.read_bytes(name)
              for name in fab_profiles.names()}
    assert mirror == canonical, (
        "packaged fab-profile mirror drifted — run `python3 "
        "tools/examples_mirror.py build`")


def test_every_example_claiming_an_id_is_a_shippable_profile():
    """Profiles are picked out of ``examples/fab`` by CONTENT (``id`` +
    ``[source]``), so a profile draft missing its ``[source]`` would simply
    not be mirrored. This closes that gap: anything with an ``id`` must load
    as a profile and must be in the package."""
    for path in sorted(EXAMPLES_FAB.glob("*.toml")):
        doc = tomllib.loads(path.read_bytes().decode("utf-8"))
        if "id" not in doc:
            continue
        assert fab.load_profile(path)["id"] == doc["id"]
        assert path.name in fab_profiles.names(), (
            f"{path.name} has an id but is not bundled — run `python3 "
            "tools/examples_mirror.py build`")


def test_order_manifest_example_is_not_bundled():
    # examples/fab also holds the example ORDER manifest; it is declared
    # purchase intent, not a profile, and must never become a `--profile` id.
    assert (EXAMPLES_FAB / "order.toml").is_file()
    assert "order.toml" not in fab_profiles.names()
    with pytest.raises(AkcliError) as ei:
        fab.resolve_profile("order")
    assert ei.value.code == "BAD_CONFIG"


def test_every_bundled_profile_is_valid_dated_and_uniquely_named():
    profiles = fab.bundled_profiles()
    assert profiles
    ids = [doc["id"] for doc, _ in profiles]
    assert len(set(ids)) == len(ids), f"duplicate bundled profile ids: {ids}"
    for doc, origin in profiles:
        assert origin.bundled and origin.path is None
        assert origin.sha256 == hashlib.sha256(
            fab_profiles.read_bytes(origin.name)).hexdigest()
        # Shipping a profile without a retrieval date would make the
        # staleness note permanent ("age unknown") for every user.
        assert fab.retrieved_at(doc) is not None, origin.name
        assert doc["source"]["urls"], origin.name


def test_bundled_lookup_rejects_paths():
    """Profile names are matched against the roster, never path-joined."""
    with pytest.raises(ValueError):
        fab_profiles.read_bytes("../fab.py")
    with pytest.raises(ValueError):
        fab_profiles.read_bytes("__init__.py")


def test_package_data_ships_every_bundled_file():
    """Each non-Python file in the package is covered by a package-data glob
    — the declaration IS the wheel content; a source-tree test cannot notice a
    wheel built without it."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"]["setuptools"]["package-data"]
    globs = data["akcli.fab_profiles"]
    names = fab_profiles.names()
    assert names
    for name in names:
        assert any(fnmatch.fnmatchcase(name, g) for g in globs), name


# --------------------------------------------------------------------------- #
# tools/examples_mirror.py — build / check over BOTH mirrors
# --------------------------------------------------------------------------- #
def _load_mirror_tool():
    spec = importlib.util.spec_from_file_location(
        "examples_mirror", ROOT / "tools" / "examples_mirror.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mirror_tool(tmp_path, monkeypatch):
    """The mirror tool, re-rooted at a synthetic repo under ``tmp_path``."""
    tool = _load_mirror_tool()
    root = tmp_path / "repo"
    paths = {
        "ROOT": root,
        "SOURCE": root / "examples" / "circuits",
        "MIRROR": root / "src" / "akcli" / "examples",
        "PROFILE_SOURCE": root / "examples" / "fab",
        "PROFILE_MIRROR": root / "src" / "akcli" / "fab_profiles",
    }
    for name, value in paths.items():
        monkeypatch.setattr(tool, name, value)
    (paths["SOURCE"] / "demo").mkdir(parents=True)
    (paths["SOURCE"] / "demo" / "ops.json").write_bytes(b'{"ops": []}\n')
    paths["PROFILE_SOURCE"].mkdir(parents=True)
    (paths["PROFILE_SOURCE"] / "p1.toml").write_bytes(
        b'id = "p1"\n[source]\nurls = ["https://example.com"]\n')
    (paths["PROFILE_SOURCE"] / "order.toml").write_bytes(
        b'delivery_format = "single"\n')
    paths["PROFILE_MIRROR"].mkdir(parents=True)
    (paths["PROFILE_MIRROR"] / "__init__.py").write_bytes(b'"""data"""\n')
    return tool


def test_mirror_tool_builds_and_checks_both_mirrors(mirror_tool, capsys):
    tool = mirror_tool
    assert tool.main(["check"]) == 1            # nothing mirrored yet
    out = capsys.readouterr().out
    assert "example mirror drift" in out and "demo" in out
    assert "fab profile mirror drift" in out and "p1.toml" in out

    assert tool.main(["build"]) == 0
    capsys.readouterr()
    assert (tool.MIRROR / "demo" / "ops.json").read_bytes() == b'{"ops": []}\n'
    assert (tool.PROFILE_MIRROR / "p1.toml").read_bytes() == (
        tool.PROFILE_SOURCE / "p1.toml").read_bytes()
    # the order manifest is not a profile; the package module is left alone
    assert sorted(p.name for p in tool.PROFILE_MIRROR.iterdir()) == [
        "__init__.py", "p1.toml"]

    assert tool.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "example mirror up to date (1 example(s))" in out
    assert "fab profile mirror up to date (1 fab profile(s))" in out


def test_mirror_tool_detects_and_repairs_profile_drift(mirror_tool, capsys):
    tool = mirror_tool
    assert tool.main(["build"]) == 0
    src = tool.PROFILE_SOURCE / "p1.toml"
    src.write_bytes(src.read_bytes() + b'vendor = "ACME"\n')
    capsys.readouterr()

    assert tool.main(["check"]) == 1            # a drifted profile fails CI...
    out = capsys.readouterr().out
    assert "fab profile mirror drift" in out and "p1.toml" in out
    assert "example mirror up to date" in out   # ...without blaming the other
    assert b"ACME" not in (tool.PROFILE_MIRROR / "p1.toml").read_bytes(), (
        "`check` must never write")

    assert tool.main(["build"]) == 0
    assert tool.main(["check"]) == 0
    assert b"ACME" in (tool.PROFILE_MIRROR / "p1.toml").read_bytes()


def test_mirror_tool_prunes_orphaned_profiles(mirror_tool, capsys):
    tool = mirror_tool
    assert tool.main(["build"]) == 0
    (tool.PROFILE_SOURCE / "p1.toml").unlink()  # canonical revision retired
    capsys.readouterr()

    assert tool.main(["check"]) == 1
    assert "orphan: no examples/fab/p1.toml" in capsys.readouterr().out
    assert (tool.PROFILE_MIRROR / "p1.toml").exists()

    assert tool.main(["build"]) == 0
    assert not (tool.PROFILE_MIRROR / "p1.toml").exists()
    assert (tool.PROFILE_MIRROR / "__init__.py").exists()   # never the package
    assert tool.main(["check"]) == 0


def test_mirror_tool_refuses_an_unparseable_example(mirror_tool):
    # A profile with a TOML typo must stop the build, not silently drop out
    # of the wheel as "not a profile".
    (mirror_tool.PROFILE_SOURCE / "broken.toml").write_bytes(b'id = "x\n')
    with pytest.raises(SystemExit) as ei:
        mirror_tool.main(["check"])
    assert "examples/fab/broken.toml" in str(ei.value)


def test_the_repo_mirrors_are_in_sync(capsys):
    """The CI drift gate, as a test: both real mirrors are current."""
    assert _load_mirror_tool().main(["check"]) == 0, capsys.readouterr().out


# --------------------------------------------------------------------------- #
# fab.resolve_profile — precedence
# --------------------------------------------------------------------------- #
def test_resolve_by_id_and_by_file_stem(bundled):
    doc, origin = bundled
    stem = origin.name.removesuffix(".toml")
    for arg in (doc["id"], stem):
        got, got_origin = fab.resolve_profile(arg)
        assert got == doc
        assert got_origin == origin
        assert got_origin.bundled is True and got_origin.path is None
    assert origin.sha256 == hashlib.sha256(
        (EXAMPLES_FAB / origin.name).read_bytes()).hexdigest()


def test_an_existing_file_beats_a_bundled_id(tmp_path, monkeypatch, bundled):
    doc, _origin = bundled
    monkeypatch.chdir(tmp_path)
    assert fab.resolve_profile(doc["id"])[1].bundled is True

    # Same spelling, now also a file in the working directory: the file wins,
    # so a profile committed to a project is never shadowed by a bundled one.
    shadow = tmp_path / doc["id"]
    shadow.write_text(_LOCAL_PROFILE, encoding="utf-8", newline="\n")
    got, origin = fab.resolve_profile(doc["id"])
    assert got["id"] == "local-override"
    assert origin.bundled is False
    assert origin.path == doc["id"]
    assert origin.sha256 == hashlib.sha256(shadow.read_bytes()).hexdigest()

    shadow.unlink()
    assert fab.resolve_profile(doc["id"])[1].bundled is True


def test_a_profile_file_resolves_as_before(tmp_path):
    p = tmp_path / "mine.toml"
    p.write_text(_LOCAL_PROFILE, encoding="utf-8", newline="\n")
    got, origin = fab.resolve_profile(p)
    assert got == fab.load_profile(p)
    assert origin == fab.ProfileOrigin(
        False, str(p), "mine.toml", hashlib.sha256(p.read_bytes()).hexdigest())


def test_unknown_profile_lists_every_bundled_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(AkcliError) as ei:
        fab.resolve_profile("no-such-profile")
    assert ei.value.code == "BAD_CONFIG"
    for pid in _bundled_ids():
        assert pid in ei.value.message
    # a path that looks like a file but does not exist is the same error —
    # never a silent fall-through to some bundled default
    with pytest.raises(AkcliError) as ei:
        fab.resolve_profile(str(tmp_path / "missing.toml"))
    assert ei.value.code == "BAD_CONFIG"
    assert _bundled_ids()[0] in ei.value.message


def test_a_malformed_profile_file_is_bad_config_not_a_fallback(tmp_path):
    # An existing-but-broken file must fail as itself; resolving it to a
    # bundled profile instead would check the board against the wrong policy.
    p = tmp_path / "broken.toml"
    p.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(AkcliError) as ei:
        fab.resolve_profile(p)
    assert ei.value.code == "BAD_CONFIG" and "broken.toml" in ei.value.message


# --------------------------------------------------------------------------- #
# CLI: --profile <id>, `fab profiles`
# --------------------------------------------------------------------------- #
def test_cli_fab_check_accepts_a_bundled_id(board, bundled):
    doc, origin = bundled
    out = json_of(["fab", "check", str(board), "--profile", doc["id"],
                   "--json", "--fail-on", "never"], expect=0)
    assert out["schema_version"]
    assert out["profile"] == {"id": doc["id"], "vendor": doc.get("vendor"),
                              "path": None, "bundled": True,
                              "sha256": origin.sha256}


def test_cli_fab_check_reports_a_file_profile_as_not_bundled(board, tmp_path):
    p = tmp_path / "mine.toml"
    p.write_text(_LOCAL_PROFILE, encoding="utf-8", newline="\n")
    out = json_of(["fab", "check", str(board), "--profile", str(p), "--json"],
                  expect=0)
    assert out["profile"]["bundled"] is False
    assert out["profile"]["path"] == str(p)
    assert out["profile"]["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert out["findings"] == []          # 2020 date, but a FILE is never stale


def test_cli_unknown_profile_is_a_usage_class_error(board, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc, out, err = run_cli(["fab", "check", str(board), "--profile", "nope"])
    assert rc == 2 and out == ""
    assert "BAD_CONFIG" in err
    for pid in _bundled_ids():
        assert pid in err


def test_cli_fab_explain_accepts_a_bundled_id(bundled):
    doc, _origin = bundled
    rc, out, _err = run_cli(["fab", "explain", "FAB_PROFILE_STALE",
                             "--profile", doc["id"]])
    assert rc == 0
    assert f"profile: {doc['id']}" in out
    assert doc["source"]["urls"][0] in out


def test_cli_fab_profiles_text(bundled):
    rc, out, err = run_cli(["fab", "profiles"])
    assert rc == 0 and err == ""
    profiles = fab.bundled_profiles()
    assert f"bundled fab profiles ({len(profiles)}):" in out
    for doc, origin in profiles:
        assert doc["id"] in out and origin.name in out
        assert str(doc.get("vendor")) in out
        assert fab.retrieved_at(doc).isoformat() in out
        for url in doc["source"]["urls"]:
            assert url in out


def test_cli_fab_profiles_json_is_stamped_and_complete():
    doc = json_of(["fab", "profiles", "--json"], expect=0)
    assert doc["schema_version"]                      # version-stamped
    rows = {row["file"]: row for row in doc["profiles"]}
    canonical = _canonical_profiles()
    assert set(rows) == set(canonical)
    for name, data in canonical.items():
        src = tomllib.loads(data.decode("utf-8"))
        assert rows[name] == {
            "id": src["id"],
            "vendor": src.get("vendor"),
            "file": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "source": {"urls": src["source"]["urls"],
                       "retrieved_at": str(src["source"]["retrieved_at"])},
        }


# --------------------------------------------------------------------------- #
# FAB_PROFILE_STALE
# --------------------------------------------------------------------------- #
def _stale(findings):
    return [f for f in findings if f.code == "FAB_PROFILE_STALE"]


def test_bundled_profile_older_than_12_months_raises_one_note(board, bundled):
    doc, origin = bundled
    pcb = kicad.read_pcb(str(board))
    got = fab.retrieved_at(doc)
    anniversary = got.replace(year=got.year + 1)

    # exactly twelve months old is not yet "older than twelve months"
    assert _stale(fab.check(pcb, doc, today=anniversary, origin=origin)) == []
    assert _stale(fab.check(pcb, doc, today=got, origin=origin)) == []

    findings = fab.check(pcb, doc, today=anniversary + dt.timedelta(days=1),
                         origin=origin)
    notes = _stale(findings)
    assert len(notes) == 1                               # ONE, not one per rule
    assert notes[0].severity.value == "note"
    assert doc["id"] in notes[0].message
    assert got.isoformat() in notes[0].message
    assert findings == notes                             # the board is clean

    # ...and stays exactly one however old it gets
    assert len(_stale(fab.check(pcb, doc, today=dt.date(2099, 1, 1),
                                origin=origin))) == 1


def test_a_profile_file_is_never_stale(board, tmp_path):
    p = tmp_path / "mine.toml"
    p.write_text(_LOCAL_PROFILE, encoding="utf-8", newline="\n")   # 2020-01-01
    doc, origin = fab.resolve_profile(p)
    pcb = kicad.read_pcb(str(board))
    assert fab.check(pcb, doc, today=dt.date(2099, 1, 1), origin=origin) == []
    # and the pre-existing call shape (no origin at all) is unchanged
    assert fab.check(pcb, doc, today=dt.date(2099, 1, 1)) == []


def test_bundled_profile_without_a_date_is_flagged_as_age_unknown(board, bundled):
    doc, origin = bundled
    undated = {**doc, "source": {"urls": doc["source"]["urls"]}}
    pcb = kicad.read_pcb(str(board))
    notes = _stale(fab.check(pcb, undated, today=dt.date(2026, 1, 1),
                             origin=origin))
    assert len(notes) == 1 and notes[0].severity.value == "note"
    assert "retrieved_at" in notes[0].message


def test_retrieved_at_accepts_both_toml_spellings():
    quoted = {"source": {"retrieved_at": "2026-07-14"}}
    native = tomllib.loads("[source]\nretrieved_at = 2026-07-14\n")
    assert fab.retrieved_at(quoted) == fab.retrieved_at(native) == dt.date(
        2026, 7, 14)
    assert fab.retrieved_at({"source": {"retrieved_at": "last summer"}}) is None
    assert fab.retrieved_at({}) is None


def test_leap_day_profile_goes_stale_without_an_invalid_date(board, bundled):
    doc, origin = bundled
    leap = {**doc, "source": {**doc["source"], "retrieved_at": "2024-02-29"}}
    pcb = kicad.read_pcb(str(board))
    assert _stale(fab.check(pcb, leap, today=dt.date(2025, 2, 28),
                            origin=origin)) == []
    assert len(_stale(fab.check(pcb, leap, today=dt.date(2025, 3, 1),
                                origin=origin))) == 1


def test_cli_stale_note_does_not_fail_the_default_policy(board, bundled,
                                                         monkeypatch):
    doc, _origin = bundled
    monkeypatch.setattr(fab, "_today", lambda: dt.date(2099, 1, 1))
    argv = ["fab", "check", str(board), "--profile", doc["id"], "--json"]

    out = json_of(argv, expect=0)        # default --fail-on warning: NOTE passes
    assert [(f["code"], f["severity"]) for f in out["findings"]] == [
        ("FAB_PROFILE_STALE", "note")]
    # the note is real, not hidden: a stricter policy does act on it
    json_of([*argv, "--fail-on", "note"], expect=1)

    rc, text, _err = run_cli(argv[:-1])                  # text mode
    assert rc == 0
    assert "NOTE" in text and "FAB_PROFILE_STALE" in text
    assert "bundled profile" in text


def test_cli_stale_note_is_absent_inside_the_twelve_months(board, bundled,
                                                           monkeypatch):
    doc, _origin = bundled
    monkeypatch.setattr(fab, "_today", lambda: fab.retrieved_at(doc))
    assert main(["fab", "check", str(board), "--profile", doc["id"]]) == 0
    out = json_of(["fab", "check", str(board), "--profile", doc["id"],
                   "--json"], expect=0)
    assert out["findings"] == []
