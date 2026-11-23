"""Output placement policy gate — SPEC §10 (state / cache / deliverable).

akcli's promise to the user's repo: every file the tool writes is classified.

* **State** (journal, rotated backups) lives in ``.akcli/`` and the state
  root SELF-IGNORES — whoever creates it drops ``.akcli/.gitignore``
  containing ``*``, so ``git status`` stays clean without akcli ever
  touching a user file.
* **Cache** (jlc search cache, fetched datasheet PDFs) defaults OUTSIDE the
  CWD (XDG cache tree) and is never load-bearing for review results.
* **Deliverables** (libraries, the datasheet facts store, SVG, the schematic
  itself) default in-repo — they are committed team assets that survive
  handoff — but only to the whitelisted, documented locations.

The census at the bottom freezes the set of CWD-relative default-path
literals so a new write surface cannot ship unclassified.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from akcli import journal, safety

SRC = Path(__file__).resolve().parents[1] / "src" / "akcli"


# --------------------------------------------------------------------------- #
# state: .akcli/ self-ignore
# --------------------------------------------------------------------------- #
def test_ensure_workspace_dir_writes_self_ignore(tmp_path):
    target = tmp_path / "board.kicad_sch"
    root = journal.ensure_workspace_dir(target)
    assert root == tmp_path / ".akcli"
    gi = root / ".gitignore"
    assert gi.read_text(encoding="utf-8") == "*\n"


def test_ensure_workspace_dir_keeps_user_gitignore(tmp_path):
    root = tmp_path / ".akcli"
    root.mkdir()
    gi = root / ".gitignore"
    gi.write_text("# mine\n", encoding="utf-8", newline="\n")
    journal.ensure_workspace_dir(tmp_path / "board.kicad_sch")
    assert gi.read_text(encoding="utf-8") == "# mine\n"


def test_journal_record_self_ignores_state_root(tmp_path, monkeypatch):
    monkeypatch.delenv("AKCLI_JOURNAL", raising=False)
    target = tmp_path / "board.kicad_sch"
    journal.record(target, "draw", "applied")
    assert (tmp_path / ".akcli" / ".gitignore").read_text(
        encoding="utf-8") == "*\n"
    assert (tmp_path / ".akcli" / "journal.jsonl").is_file()


def test_ensure_backups_dir_self_ignores(tmp_path):
    b = journal.ensure_backups_dir(tmp_path / "board.kicad_sch")
    assert b == tmp_path / ".akcli" / "backups"
    assert b.is_dir()
    assert (tmp_path / ".akcli" / ".gitignore").is_file()


def test_safety_backup_hook_self_ignores_akcli_parent(tmp_path):
    f = tmp_path / "board.kicad_sch"
    f.write_bytes(b"old")
    bdir = tmp_path / ".akcli" / "backups"
    safety.atomic_write_with_backup(f, b"new", backup_dir=bdir)
    assert (bdir / "board.kicad_sch.bak").read_bytes() == b"old"
    assert (tmp_path / ".akcli" / ".gitignore").read_text(
        encoding="utf-8") == "*\n"


def test_safety_backup_hook_leaves_other_dirs_alone(tmp_path):
    f = tmp_path / "board.kicad_sch"
    f.write_bytes(b"old")
    bdir = tmp_path / "mybackups"
    safety.atomic_write_with_backup(f, b"new", backup_dir=bdir)
    assert not (bdir / ".gitignore").exists()
    assert not (tmp_path / ".gitignore").exists()


# --------------------------------------------------------------------------- #
# cache: defaults stay out of the CWD
# --------------------------------------------------------------------------- #
def test_cache_defaults_outside_cwd(tmp_path, monkeypatch):
    from akcli.parts import datasheet as ds
    from akcli.parts import search as parts_search
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.delenv("AKCLI_JLC_CACHE", raising=False)
    monkeypatch.delenv("AKCLI_DATASHEET_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    for d in (parts_search.default_cache_dir(), ds.default_dir()):
        assert d is not None
        assert not d.is_relative_to(proj)
        assert d.is_relative_to(tmp_path / "xdg")


def test_facts_store_root_is_project_local(tmp_path, monkeypatch):
    # facts are DELIVERABLE-class: the store defaults to the project-local
    # ./datasheets (a committed team asset — same numbers for everyone,
    # survives handoff), never to a personal cache dir
    from akcli.commands.review import _facts_store_root
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    assert _facts_store_root(argparse.Namespace(dir=None)) == Path("datasheets")
    # explicit --dir always wins
    assert _facts_store_root(argparse.Namespace(dir="x")) == Path("x")


def test_review_analyze_never_reads_personal_cache():
    # reproducibility: the facts feeding review/release findings must come
    # from the project (sch-relative ./datasheets or explicit --facts/--dir),
    # never silently from one user's XDG cache
    for mod in ("commands/review.py", "commands/release.py"):
        text = (SRC / mod).read_text(encoding="utf-8")
        assert "default_dir" not in text, mod


# --------------------------------------------------------------------------- #
# census: CWD-relative default-path literals are frozen
# --------------------------------------------------------------------------- #
def _files_containing(pattern: str) -> set[str]:
    rx = re.compile(pattern)
    hits: set[str] = set()
    for py in sorted(SRC.rglob("*.py")):
        if "_vendor" in py.parts:
            continue
        if rx.search(py.read_text(encoding="utf-8")):
            hits.add(py.relative_to(SRC).as_posix())
    return hits


def test_facts_default_resolves_through_single_choke_point():
    # the ./datasheets default lives ONLY in review._facts_store_root (where
    # the policy is documented) — not scattered across argparse defaults or
    # ad-hoc `or "datasheets"` fallbacks that would drift independently
    assert _files_containing(r'default="datasheets"') == set()
    assert _files_containing(r'or "datasheets"') == set()


def test_library_baseline_defaults_beside_the_project(tmp_path):
    # DELIVERABLE-class: the baseline is a committed record a release gate
    # compares against, so it defaults beside the .kicad_pro — never under
    # the self-ignored .akcli/ state root, never into a per-user cache
    from akcli import config, library_baseline, libtable
    from akcli.commands.library import _baseline_path
    ws = libtable.discover(tmp_path)
    cfg = config.Config()
    default = _baseline_path(ws, cfg, "")
    assert default == tmp_path / library_baseline.DEFAULT_FILENAME
    assert ".akcli" not in default.parts
    # precedence: explicit FILE > [paths].library_baseline > the default
    cfg.paths["library_baseline"] = str(tmp_path / "rel" / "libs.json")
    assert _baseline_path(ws, cfg, "") == tmp_path / "rel" / "libs.json"
    assert _baseline_path(ws, cfg, "x.json") == Path("x.json")


def test_cwd_default_literal_census():
    # `Path("datasheets")` is only the documented back-compat probe in the
    # review facts commands; `akcli-parts` is only the jlc add deliverable
    # default. A new file in either set = a new unclassified CWD default —
    # classify it per SPEC §10 and extend this census deliberately.
    assert _files_containing(r'Path\("datasheets"\)') == {"commands/review.py"}
    assert _files_containing(r"akcli-parts") == {"commands/jlc.py"}
    # the library-baseline default NAME is one constant (help texts may
    # mention it inside a sentence; only this module holds it as a value)
    assert _files_containing(r'"akcli-library\.baseline\.json"') == {
        "library_baseline.py"}
