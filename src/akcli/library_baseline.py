"""Library baseline — a committed, hashed record of a project's libraries.

``akcli library audit --write-baseline`` records, for every entry of the
PROJECT ``sym-lib-table`` / ``fp-lib-table``, what the release was built
from: the nickname, the uri as written, where it resolved, and a SHA-256 per
file (the ``.kicad_sym``; every ``.kicad_mod`` of a ``.pretty``; a
``provenance.json`` sitting in the library; every 3D model those footprints
reference). ``--baseline`` — and ``release preflight``'s ``library-baseline``
gate — rebuild the same record from disk and report every difference:

* ``LIB_BASELINE_DRIFT``     — a hash, a uri or a nickname differs (ERROR);
* ``LIB_BASELINE_MISSING``   — recorded, but gone on disk (ERROR);
* ``LIB_BASELINE_UNTRACKED`` — on disk, but not recorded (WARNING).

(Deliberately NOT called a "lock": ``library check-lock`` is the KiCad-GUI
lock probe and ``gate --lock`` is the BOM lockfile.)

The file is a DELIVERABLE (SPEC §10): it lives beside the ``.kicad_pro`` and
is meant to be committed, so it is deterministic and machine-independent —
sorted keys, sorted lists, ``\\n`` newlines, no timestamps, no tool version,
no absolute paths. Three rules follow from "machine-independent":

* a path is recorded relative to the project directory (posix), or ``null``
  when it lies outside the project — the content hash still pins it;
* a library or model reached through one of KiCad's built-in
  ``${KICAD<N>_*_DIR}`` variables (or the KiCad <= 5 ``${KISYSMOD}`` /
  ``${KISYS3DMOD}``) is KiCad's OWN stock file: its bytes follow
  the installed KiCad, not the project, so it is recorded by reference only
  (status ``builtin``), never hashed;
* libraries the schematics reach only through the per-user GLOBAL table are
  recorded by nickname only (``global_libraries``) — derived from the
  schematics and the project tables alone, so the list does not depend on
  whether this machine has a global table at all.

Hashes are over RAW BYTES. A line-ending conversion on checkout changes them,
so library files should be marked ``-text`` in ``.gitattributes``
(docs/design-integrity.md).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, NoReturn

from . import contract, libtable
from .errors import fail
from .report import Finding, Severity
from .writers import atomic

__all__ = [
    "DEFAULT_FILENAME", "SCHEMA_VERSION", "build", "compare", "dumps",
    "load", "summary", "write",
]

# The ONE place the default name lives (output-placement census, SPEC §10).
DEFAULT_FILENAME = "akcli-library.baseline.json"
SCHEMA_VERSION = contract.SCHEMA_VERSIONS["library-baseline"]

_KINDS = (("sym", "symbol_libraries", "symbol"),
          ("fp", "footprint_libraries", "footprint"))
_ERROR, _WARNING = Severity.ERROR, Severity.WARNING


# --------------------------------------------------------------------------- #
# build — the record of what is on disk now
# --------------------------------------------------------------------------- #
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path, project_dir: Path) -> str | None:
    """``path`` relative to the project directory (posix), else ``None``."""
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _hash_into(files: dict[str, str], path: Path, name: str) -> None:
    """Record ``name -> sha256`` — or nothing for a file that cannot be read
    (it then compares as MISSING against a baseline that has it)."""
    try:
        files[name] = _sha256(path)
    except OSError:
        pass


def _library_record(lib: libtable.LoadedLibrary, project_dir: Path) -> dict[str, Any]:
    entry = lib.entry
    rec: dict[str, Any] = {
        "nickname": entry.name,
        "uri": entry.uri,
        "path": None,
        "status": "ok",
        "files": {},
    }
    if lib.kind == "fp":
        rec["models"] = []
    if libtable.is_kicad_builtin_ref(entry.uri):
        rec["status"] = "builtin"
        return rec
    if lib.unresolved:
        rec["status"] = "unresolved"
        return rec
    rec["path"] = _rel(lib.path, project_dir)
    if not lib.exists:
        rec["status"] = "missing"
        return rec

    files: dict[str, str] = rec["files"]
    if lib.kind == "sym":
        if lib.path.is_dir():
            for sym in sorted(lib.path.glob("*.kicad_sym")):
                _hash_into(files, sym, sym.name)
        else:
            _hash_into(files, lib.path, lib.path.name)
        return rec

    if lib.path.is_file():
        # a single-file footprint library (the Altium/Eagle/legacy plugin
        # types KiCad can register): the file IS the library
        _hash_into(files, lib.path, lib.path.name)
        return rec
    # every .kicad_mod, readable or not — an unparseable footprint is still
    # part of what ships
    for mod in sorted(lib.path.glob("*.kicad_mod")):
        _hash_into(files, mod, mod.name)
    provenance = lib.path / "provenance.json"
    if provenance.is_file():
        _hash_into(files, provenance, provenance.name)

    models: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for loaded in lib.footprints:
        for m in loaded.models:
            key = (loaded.file.name, m.ref)
            if key in seen:
                continue
            seen.add(key)
            mrec: dict[str, Any] = {"file": loaded.file.name, "ref": m.ref,
                                    "path": None, "sha256": None,
                                    "status": "unresolved"}
            if libtable.is_kicad_builtin_ref(m.ref):
                mrec["status"] = "builtin"
            elif m.path is not None:
                mrec["path"] = _rel(m.path, project_dir)
                mrec["status"] = "missing"
                if m.path.is_file():
                    try:
                        mrec["sha256"] = _sha256(m.path)
                        mrec["status"] = "hashed"
                    except OSError:
                        pass
            models.append(mrec)
    rec["models"] = sorted(models, key=lambda r: (r["file"], r["ref"]))
    return rec


def _global_nicknames(ws: libtable.Workspace,
                      sch_paths: list[Path]) -> dict[str, list[str]]:
    """Nicknames the schematics use that the PROJECT tables do not register."""
    from .readers import kicad as kreader

    used: dict[str, set[str]] = {"symbol": set(), "footprint": set()}
    for sch_path in sch_paths:
        try:
            sch = kreader.read_sch(str(sch_path))
        except Exception:
            continue                    # `library audit` reports SCH_UNREADABLE
        for comp in sch.components:
            for lib_id, table, key in (
                    (comp.library_ref, ws.sym_table, "symbol"),
                    (comp.footprint, ws.fp_table, "footprint")):
                if not lib_id or ":" not in lib_id:
                    continue
                nick = lib_id.split(":", 1)[0]
                if nick and (table is None or table.get(nick) is None):
                    used[key].add(nick)
    return {key: sorted(names) for key, names in used.items()}


def build(ws: libtable.Workspace,
          sch_paths: list[Path] | None = None) -> dict[str, Any]:
    """The baseline document for ``ws`` as it is on disk right now.

    ``sch_paths`` selects the schematics whose library nicknames feed
    ``global_libraries`` (default: the project's discovered schematics — the
    set ``library audit`` audits).
    """
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "symbol_libraries": [],
        "footprint_libraries": [],
    }
    by_kind = {kind: key for kind, key, _label in _KINDS}
    for lib in libtable.load_libraries(ws):
        doc[by_kind[lib.kind]].append(_library_record(lib, ws.project_dir))
    for _kind, key, _label in _KINDS:
        doc[key].sort(key=lambda r: (r["nickname"], r["uri"]))
    doc["global_libraries"] = _global_nicknames(
        ws, list(sch_paths if sch_paths is not None else ws.schematics))
    return doc


def summary(doc: dict[str, Any]) -> dict[str, int]:
    """Counts for the one-line report: libraries, hashed files, model states."""
    libs = [*doc["symbol_libraries"], *doc["footprint_libraries"]]
    models = [m for lib in libs for m in lib.get("models", [])]
    return {
        "libraries": len(libs),
        "files": sum(len(lib["files"]) for lib in libs),
        "models_hashed": sum(1 for m in models if m["status"] == "hashed"),
        "models_unresolved": sum(
            1 for m in models if m["status"] in ("unresolved", "missing")),
        "models_builtin": sum(1 for m in models if m["status"] == "builtin"),
        "global_libraries": sum(
            len(v) for v in doc["global_libraries"].values()),
    }


# --------------------------------------------------------------------------- #
# serialize / load / write
# --------------------------------------------------------------------------- #
def dumps(doc: dict[str, Any]) -> str:
    """Canonical text: sorted keys, 2-space indent, one trailing ``\\n``."""
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True,
                      allow_nan=False) + "\n"


def _bad(path: Path, why: str) -> NoReturn:
    fail("BAD_CONFIG",
         f"library baseline {path}: {why} — regenerate it with "
         "`akcli library audit --write-baseline`")


def _check_library(path: Path, where: str, rec: object, with_models: bool) -> None:
    if not isinstance(rec, dict):
        _bad(path, f"{where} must be an object")
    for key in ("nickname", "uri", "status"):
        if not isinstance(rec.get(key), str):
            _bad(path, f"{where}.{key} must be a string")
    if not (rec.get("path") is None or isinstance(rec.get("path"), str)):
        _bad(path, f"{where}.path must be a string or null")
    files = rec.get("files")
    if not (isinstance(files, dict)
            and all(isinstance(k, str) and isinstance(v, str)
                    for k, v in files.items())):
        _bad(path, f"{where}.files must map file names to sha256 strings")
    if not with_models:
        return
    models = rec.get("models")
    if not isinstance(models, list):
        _bad(path, f"{where}.models must be a list")
    for i, m in enumerate(models):
        if not (isinstance(m, dict)
                and all(isinstance(m.get(k), str)
                        for k in ("file", "ref", "status"))):
            _bad(path, f"{where}.models[{i}] needs string file/ref/status")


def load(path: Path | str) -> dict[str, Any]:
    """Read and validate a baseline file.

    ``FileNotFoundError`` propagates (the CLI maps it to exit 4, as for a
    missing ``--intent`` file); anything unparseable, mis-shaped or of another
    major ``schema_version`` is ``BAD_CONFIG`` — a baseline that cannot be
    trusted must never compare as "no drift".
    """
    p = Path(path)
    data = p.read_bytes()
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        _bad(p, f"not valid JSON ({exc})")
    if not isinstance(doc, dict):
        _bad(p, "root must be a JSON object")
    version = doc.get("schema_version")
    if not isinstance(version, str) or (
            version.split(".", 1)[0] != SCHEMA_VERSION.split(".", 1)[0]):
        _bad(p, f"schema_version {version!r} is not a supported "
                f"{SCHEMA_VERSION.split('.', 1)[0]}.x baseline")
    for _kind, key, _label in _KINDS:
        libs = doc.get(key)
        if not isinstance(libs, list):
            _bad(p, f"'{key}' must be a list")
        for i, rec in enumerate(libs):
            _check_library(p, f"{key}[{i}]", rec,
                           with_models=(key == "footprint_libraries"))
    glob = doc.get("global_libraries")
    if not (isinstance(glob, dict)
            and all(isinstance(glob.get(k), list)
                    and all(isinstance(n, str) for n in glob[k])
                    for k in ("symbol", "footprint"))):
        _bad(p, "'global_libraries' must hold 'symbol' and 'footprint' "
                "nickname lists")
    result: dict[str, Any] = doc
    return result


def write(path: Path | str, doc: dict[str, Any]) -> bool:
    """Write ``doc`` atomically; ``False`` when the file already holds exactly
    these bytes (nothing is touched, so the mtime does not churn).

    Binary write through :mod:`.writers.atomic` — the bytes are the canonical
    text on every platform. No backup copy: this is a committed deliverable,
    its history is the repository's.
    """
    p = Path(path)
    text = dumps(doc)
    try:
        current = p.read_bytes()
    except OSError:
        current = b""
    if current == text.encode("utf-8"):
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic.atomic_write(p, text, hashlib.sha256(current).hexdigest(), None)
    return True


# --------------------------------------------------------------------------- #
# compare — baseline vs. what is on disk now
# --------------------------------------------------------------------------- #
def _n(count: int, what: str) -> str:
    return f"{count} {what}" + ("" if count == 1 else "s")


def _compare_models(out: list[Finding], label: str, nick: str,
                    base: dict[str, Any], cur: dict[str, Any],
                    unchanged: set[str]) -> None:
    """3D models of footprint files whose own bytes did not change.

    A changed/added/removed ``.kicad_mod`` is already reported as a file; its
    model list changing with it is that same edit, not a second problem.
    """
    cur_by = {(m["file"], m["ref"]): m for m in cur.get("models", [])}
    for b in base.get("models", []):
        key = (b["file"], b["ref"])
        c = cur_by.get(key)
        if c is None or b["file"] not in unchanged:
            continue
        refs = [nick, f"{nick}:{b['file']}"]
        what = (f"{label} library '{nick}': 3D model {b['ref']!r} "
                f"of {b['file']}")
        if b["status"] == "hashed" and c["status"] == "hashed":
            if b.get("sha256") != c.get("sha256"):
                out.append(Finding(
                    "LIB_BASELINE_DRIFT", _ERROR,
                    f"{what} changed since the baseline (sha256 "
                    f"{str(b.get('sha256'))[:12]}… -> "
                    f"{str(c.get('sha256'))[:12]}…)", refs=refs))
        elif b["status"] == "hashed":
            why = ("no longer resolves (unresolvable path variable)"
                   if c["status"] == "unresolved" else "is gone on disk")
            out.append(Finding(
                "LIB_BASELINE_MISSING", _ERROR,
                f"{what} is hashed in the baseline but {why}", refs=refs))
        elif c["status"] == "hashed":
            out.append(Finding(
                "LIB_BASELINE_UNTRACKED", _WARNING,
                f"{what} now exists on disk but the baseline recorded it as "
                f"{b['status']} — it is not pinned", refs=refs))


def _compare_library(out: list[Finding], label: str,
                     base: dict[str, Any], cur: dict[str, Any]) -> None:
    nick = cur["nickname"]
    refs = [nick]
    if base["uri"] != cur["uri"]:
        out.append(Finding(
            "LIB_BASELINE_DRIFT", _ERROR,
            f"{label} library '{nick}': uri changed since the baseline "
            f"({base['uri']!r} -> {cur['uri']!r})", refs=refs))
    b_status, c_status = base["status"], cur["status"]
    if b_status != c_status:
        if b_status == "ok":
            out.append(Finding(
                "LIB_BASELINE_MISSING", _ERROR,
                f"{label} library '{nick}' ({cur['uri']}) is {c_status} on "
                f"this machine — its {_n(len(base['files']), 'baseline file')} "
                "cannot be verified", refs=refs))
        elif c_status == "ok":
            out.append(Finding(
                "LIB_BASELINE_UNTRACKED", _WARNING,
                f"{label} library '{nick}' ({cur['uri']}) now resolves with "
                f"{_n(len(cur['files']), 'file')}, but the baseline recorded "
                f"it as {b_status} — none of them is pinned", refs=refs))
        else:
            out.append(Finding(
                "LIB_BASELINE_DRIFT", _ERROR,
                f"{label} library '{nick}' ({cur['uri']}): recorded as "
                f"{b_status} in the baseline, {c_status} now", refs=refs))
        return
    if b_status != "ok":
        return                      # nothing was hashed on either side
    b_files, c_files = base["files"], cur["files"]
    unchanged: set[str] = set()
    for name in sorted(set(b_files) | set(c_files)):
        file_refs = [nick, f"{nick}:{name}"]
        if name not in c_files:
            out.append(Finding(
                "LIB_BASELINE_MISSING", _ERROR,
                f"{label} library '{nick}': {name} is in the baseline but "
                "gone on disk", refs=file_refs))
        elif name not in b_files:
            out.append(Finding(
                "LIB_BASELINE_UNTRACKED", _WARNING,
                f"{label} library '{nick}': {name} is on disk but not in "
                "the baseline", refs=file_refs))
        elif b_files[name] != c_files[name]:
            out.append(Finding(
                "LIB_BASELINE_DRIFT", _ERROR,
                f"{label} library '{nick}': {name} changed since the "
                f"baseline (sha256 {b_files[name][:12]}… -> "
                f"{c_files[name][:12]}…)", refs=file_refs))
        else:
            unchanged.add(name)
    _compare_models(out, label, nick, base, cur, unchanged)


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> list[Finding]:
    """Findings for every difference between a loaded baseline and
    :func:`build`'s record of the disk.

    ``refs`` (what a config ``[[waiver]]`` scopes on) are the library
    nickname, plus ``nickname:file`` for a finding about one file — so one
    waiver can cover a whole library or a single footprint file.
    """
    out: list[Finding] = []
    for kind, key, label in _KINDS:
        base = {r["nickname"]: r for r in baseline[key]}
        cur = {r["nickname"]: r for r in current[key]}
        # A renamed nickname: same resolved location, different name.
        gone = {n: r for n, r in base.items() if n not in cur}
        new = {n: r for n, r in cur.items() if n not in base}
        renamed: dict[str, str] = {}
        for old, b in sorted(gone.items()):
            match = next((n for n, c in sorted(new.items())
                          if n not in renamed.values()
                          and b["path"] is not None
                          and c["path"] == b["path"]), None)
            if match is not None:
                renamed[old] = match
        for name in sorted(set(base) | set(cur)):
            if name in renamed:
                target = renamed[name]
                out.append(Finding(
                    "LIB_BASELINE_DRIFT", _ERROR,
                    f"{label} library '{name}' is now registered as "
                    f"'{target}' (same location {base[name]['path']}) — "
                    "every lib_id that names it must follow",
                    refs=[name, target]))
                _compare_library(out, label, base[name], cur[target])
            elif name in renamed.values():
                continue
            elif name not in cur:
                out.append(Finding(
                    "LIB_BASELINE_MISSING", _ERROR,
                    f"{label} library '{name}' ({base[name]['uri']}) is in "
                    f"the baseline but no longer in the project "
                    f"{kind}-lib-table", refs=[name]))
            elif name not in base:
                out.append(Finding(
                    "LIB_BASELINE_UNTRACKED", _WARNING,
                    f"{label} library '{name}' ({cur[name]['uri']}) is in "
                    f"the project {kind}-lib-table but not in the baseline",
                    refs=[name]))
            else:
                _compare_library(out, label, base[name], cur[name])
        b_glob = set(baseline["global_libraries"][label])
        c_glob = set(current["global_libraries"][label])
        for name in sorted(b_glob - c_glob):
            out.append(Finding(
                "LIB_BASELINE_MISSING", _ERROR,
                f"{label} library nickname '{name}' is in the baseline's "
                "global_libraries, but no schematic uses it outside the "
                f"project {kind}-lib-table any more", refs=[name]))
        for name in sorted(c_glob - b_glob):
            out.append(Finding(
                "LIB_BASELINE_UNTRACKED", _WARNING,
                f"{label} library nickname '{name}' is used by the "
                f"schematics without a project {kind}-lib-table entry (it "
                "resolves, if at all, through the global table) and is not "
                "in the baseline's global_libraries", refs=[name]))
    return out
