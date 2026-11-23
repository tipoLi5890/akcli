"""Project library workspace: ``sym-lib-table`` / ``fp-lib-table`` + audit.

KiCad resolves symbol/footprint nicknames through the project's (and global)
library tables; a nickname mismatch — a symbol whose Footprint field says
``footprint:X`` while the table registers ``proj_jlc`` — is invisible to
ERC and every netlist check, and surfaces only as "footprint not found" in the
GUI. This module makes the project tables a first-class, auditable object:

* :func:`read_table` — parse a ``sym-lib-table`` / ``fp-lib-table``;
* :func:`discover` — build a :class:`Workspace` from a project directory;
* :func:`nearest_project_table` / :func:`nicknames_for_dir` — which project
  table governs a directory, and under which nickname(s) it registers it;
* :func:`load_libraries` — every project table entry, resolved and loaded
  (the one walk ``audit``, the library baseline and ``verify --library`` share);
* :func:`audit` — findings across schematic <-> tables <-> libraries <-> 3D
  (:func:`audit_with_origins` also says which FILE each finding is about);
* :func:`plan_rename` / :func:`plan_model_paths` — the two historically
  hand-``sed``-ed repairs, as reviewable plans (apply via ``library repair``).

Project **and** global tables: KiCad resolves a nickname against the project
table first, then the per-user global table (``~/…/kicad/<ver>/{sym,fp}-lib-table``,
overridable via ``KICAD_CONFIG_HOME``). The global table commonly holds a single
``(type "Table")`` entry that *points at* KiCad's bundled default table, so it
must be expanded recursively (see :func:`read_table`). A nickname registered in
either table resolves — a standard KiCad library (``Device``, ``Connector`` …)
is therefore NOT flagged. Only a nickname absent from BOTH tables is a real
error; when the global table cannot be located the finding is softened (we
cannot confirm the nickname is truly unregistered).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import model
from . import sexpr
from .readers import footprint_lib
from .report import Finding, Severity, anchor
# ONE s-expression quoting authority for the whole tree (escapes backslashes,
# quotes AND control characters) — the hand-rolled repair-site copies escaped
# only the double-quote.
from .sexpr.writer import quote as _quote

# `(type "Table")` indirection depth cap (guards a cyclic/deep table chain).
_MAX_TABLE_DEPTH = 8

__all__ = [
    "LibEntry", "LibTable", "Workspace", "read_table", "discover", "audit",
    "audit_with_origins", "nearest_project_table", "nicknames_for_dir",
    "plan_rename", "plan_model_paths", "RepairEdit", "expand_var",
    "kicad_data_roots", "load_libraries", "LoadedLibrary", "LoadedFootprint",
    "ModelRef", "is_kicad_builtin_ref", "resolve_model",
]


# --------------------------------------------------------------------------- #
# KiCad built-in path variables
# --------------------------------------------------------------------------- #
# KiCad's bundled default lib-tables are written entirely in terms of
# ``${KICAD<N>_SYMBOL_DIR}`` / ``${KICAD<N>_FOOTPRINT_DIR}`` /
# ``${KICAD<N>_3DMODEL_DIR}``. These are **compiled into KiCad** — they are not
# exported to the environment, and on a clean install `kicad_common.json` stores
# them as null — so a purely ``os.environ`` expansion resolves NOTHING and every
# standard nickname (``Device:R`` …) dies as SYMBOL_NOT_FOUND. We reproduce
# KiCad's defaults by deriving them from the installed KiCad that
# :func:`..drivers.kicad_cli.find` already locates. An environment value always
# wins (that is also how KiCad treats a user override).
_KICAD_PATH_VAR_RE = re.compile(
    r"^KICAD\d*_(SYMBOL|FOOTPRINT|3DMODEL|TEMPLATE)_DIR$")
_KICAD_VAR_SUBDIR = {
    "SYMBOL": "symbols",
    "FOOTPRINT": "footprints",
    "3DMODEL": "3dmodels",
    "TEMPLATE": "template",
}
# keyed by (kicad-cli path, subdir) so a changed KICAD_CLI re-probes
_builtin_cache: dict[tuple[str, str], str | None] = {}


def kicad_data_roots(exe: os.PathLike | str | None) -> list[Path]:
    """Candidate KiCad shared-data roots for a ``kicad-cli`` executable path.

    Pure and ordered (no filesystem access) so it is unit-testable with a
    fabricated ``exe``; the caller keeps the first candidate that exists.

    * macOS app bundle — ``<bundle>.app/Contents/MacOS/kicad-cli`` →
      ``<bundle>.app/Contents/SharedSupport``;
    * POSIX/Windows install prefix — ``<prefix>/bin/kicad-cli[.exe]`` →
      ``<prefix>/share/kicad`` (covers ``/usr``, ``/usr/local``, Flatpak
      ``/app`` and ``C:\\Program Files\\KiCad\\10.0``);
    * plus the distro defaults on non-Windows, for a ``kicad-cli`` that lives
      outside its data prefix (e.g. ``/usr/lib/kicad/bin/kicad-cli``).
    """
    roots: list[Path] = []
    if exe:
        bindir = Path(exe).parent
        if bindir.name == "MacOS" and bindir.parent.name == "Contents":
            roots.append(bindir.parent / "SharedSupport")
        roots.append(bindir.parent / "share" / "kicad")
    if os.name != "nt":
        roots += [Path("/usr/share/kicad"), Path("/usr/local/share/kicad")]
    out: list[Path] = []
    for r in roots:                              # order-preserving dedupe
        if r not in out:
            out.append(r)
    return out


def _kicad_builtin_dir(var: str) -> str | None:
    """KiCad's compiled-in default for ``var``, or ``None`` if not derivable."""
    m = _KICAD_PATH_VAR_RE.match(var)
    if m is None:
        return None
    sub = _KICAD_VAR_SUBDIR[m.group(1)]
    try:
        from .drivers import kicad_cli
        exe = kicad_cli.find()
    except Exception:                            # pragma: no cover - defensive
        return None
    key = (str(exe or ""), sub)
    if key not in _builtin_cache:
        val: str | None = None
        for root in kicad_data_roots(exe):
            cand = root / sub
            try:
                if cand.is_dir():
                    val = str(cand)
                    break
            except OSError:                      # pragma: no cover - defensive
                continue
        _builtin_cache[key] = val
    return _builtin_cache[key]


# KiCad <= 5 spelled its stock footprint / 3D-model roots this way; later
# versions still write them back unchanged (tests/fixtures/kicad/fp_attrs).
_KICAD_LEGACY_PATH_VARS = frozenset({"KISYSMOD", "KISYS3DMOD"})


def is_kicad_builtin_ref(text: str) -> bool:
    """True when a lib-table uri / 3D-model path goes through one of KiCad's
    built-in ``${KICAD<N>_*_DIR}`` variables (or their KiCad <= 5 names
    ``${KISYSMOD}`` / ``${KISYS3DMOD}``) — i.e. it names a file of KiCad's
    OWN stock libraries. What such a reference resolves to depends on the
    KiCad installed on the machine, not on the project.
    """
    return any(_KICAD_PATH_VAR_RE.match(var) or var in _KICAD_LEGACY_PATH_VARS
               for var in re.findall(r"\$\{([^}]+)\}", text or ""))


def expand_var(var: str) -> str | None:
    """Value of a lib-table ``${VAR}``: environment first, KiCad built-ins next.

    ``None`` means "leave the reference unexpanded" (a genuinely unknown
    variable), which callers report as ``LIB_URI_UNRESOLVED``.
    """
    val = os.environ.get(var)
    if val is not None:
        return val
    return _kicad_builtin_dir(var)


@dataclass
class LibEntry:
    name: str
    type: str = "KiCad"
    uri: str = ""
    options: str = ""
    descr: str = ""


@dataclass
class LibTable:
    kind: str                      # "sym" | "fp"
    path: Path | None
    entries: list[LibEntry] = field(default_factory=list)

    def get(self, nickname: str) -> LibEntry | None:
        return next((e for e in self.entries if e.name == nickname), None)


@dataclass
class Workspace:
    project_dir: Path
    project_file: Path | None = None            # *.kicad_pro
    sym_table: LibTable | None = None
    fp_table: LibTable | None = None
    global_sym_table: LibTable | None = None    # per-user KiCad global table
    global_fp_table: LibTable | None = None
    schematics: list[Path] = field(default_factory=list)
    boards: list[Path] = field(default_factory=list)

    @property
    def has_global_sym(self) -> bool:
        return self.global_sym_table is not None

    @property
    def has_global_fp(self) -> bool:
        return self.global_fp_table is not None

    @staticmethod
    def _locate(nick: str, proj: LibTable | None, glob: LibTable | None) -> str | None:
        if proj is not None and proj.get(nick) is not None:
            return "project"
        if glob is not None and glob.get(nick) is not None:
            return "global"
        return None

    def locate_sym_nick(self, nick: str) -> str | None:
        """Where a symbol-lib nickname resolves: ``"project"``/``"global"``/``None``."""
        return self._locate(nick, self.sym_table, self.global_sym_table)

    def locate_fp_nick(self, nick: str) -> str | None:
        """Where a footprint-lib nickname resolves: ``"project"``/``"global"``/``None``."""
        return self._locate(nick, self.fp_table, self.global_fp_table)

    def resolve_uri(self, uri: str) -> tuple[Path, list[str]]:
        """Expand ``${KIPRJMOD}``/env vars in a table URI; returns (path, unresolved).

        Only a BARE relative uri is taken against the project directory. A
        uri anchored by a variable is complete once expanded: when the
        project directory is itself relative (``akcli library audit hw``,
        ``release preflight --sch hw/board.kicad_sch``) ``${KIPRJMOD}/x``
        expands to ``hw/x``, and joining that to ``hw`` again pointed every
        project library at ``hw/hw/x``.
        """
        unresolved: list[str] = []

        def _sub(m: re.Match) -> str:
            var = m.group(1)
            if var == "KIPRJMOD":
                return str(self.project_dir)
            val = expand_var(var)
            if val is None:
                unresolved.append(var)
                return m.group(0)
            return val

        expanded = re.sub(r"\$\{([^}]+)\}", _sub, uri)
        p = Path(expanded)
        if not p.is_absolute() and not uri.lstrip().startswith("${"):
            p = self.project_dir / p
        return p, unresolved


def _expand_env_uri(uri: str) -> Path | None:
    """Expand ``${VAR}`` vars in a lib-table uri (no ``${KIPRJMOD}``)."""
    expanded = re.sub(r"\$\{([^}]+)\}",
                      lambda m: expand_var(m.group(1)) or m.group(0), uri)
    if "${" in expanded:
        return None
    return Path(expanded)


def read_table(path: os.PathLike | str, *, _seen: set | None = None,
               _depth: int = 0) -> LibTable:
    """Parse a ``sym-lib-table`` / ``fp-lib-table`` file.

    A ``(lib (type "Table") ...)`` entry is a KiCad **indirection**: its uri
    points at another lib-table whose entries should be merged in. KiCad's
    global table is often exactly one such entry pointing at the bundled default
    table, so without expansion a read sees zero real libraries. Expansion is
    depth- and cycle-guarded; a missing/unresolvable indirection is dropped
    (the resolution scope just shrinks) rather than raising.
    """
    p = Path(path)
    # Lenient decode ON PURPOSE (unlike the .kicad_sch readers, which decode
    # strictly via safety.read_text_guarded). Lib-tables are ENVIRONMENT, not
    # the document under edit: they are shipped by KiCad installs and by
    # third-party library packs, and a mojibake byte in one library's
    # descr/uri must not take out `library scan` for every OTHER library. A
    # table we cannot recognise still fails loudly below (root-tag check).
    root = sexpr.parse(p.read_text(encoding="utf-8", errors="replace"))
    kind = {"sym_lib_table": "sym", "fp_lib_table": "fp"}.get(root.tag or "")
    if kind is None:
        from .errors import fail
        fail("KICAD_SEXPR_UNTERMINATED",
             f"{p.name}: not a lib table (root tag {root.tag!r})")
    table = LibTable(kind=kind, path=p)
    for lib in root.find_all("lib"):
        entry = LibEntry(name="")
        for child in lib.children or ():
            if not child.is_list or not child.children or len(child.children) < 2:
                continue
            tag, val = child.tag, child.children[1].value
            if tag == "name":
                entry.name = val or ""
            elif tag == "type":
                entry.type = val or "KiCad"
            elif tag == "uri":
                entry.uri = val or ""
            elif tag == "options":
                entry.options = val or ""
            elif tag == "descr":
                entry.descr = val or ""
        if entry.name:
            table.entries.append(entry)

    if any(e.type == "Table" for e in table.entries) and _depth < _MAX_TABLE_DEPTH:
        seen = _seen if _seen is not None else set()
        try:
            seen.add(p.resolve())
        except OSError:
            pass
        merged: list[LibEntry] = []
        for e in table.entries:
            if e.type != "Table":
                merged.append(e)
                continue
            sub_path = _expand_env_uri(e.uri)
            try:
                rp = sub_path.resolve() if sub_path else None
            except OSError:
                rp = None
            if sub_path is None or rp in seen or not sub_path.exists():
                continue                      # missing/cyclic indirection: drop
            try:
                merged.extend(read_table(sub_path, _seen=seen,
                                         _depth=_depth + 1).entries)
            except Exception:
                continue
        table.entries = merged
    return table


def _kicad_config_bases() -> list[Path]:
    """Candidate KiCad config roots.

    An explicit ``KICAD_CONFIG_HOME`` (or a versioned ``KICAD<N>_CONFIG_HOME``)
    **replaces** the platform default, matching KiCad — so setting it to an empty
    directory hermetically disables global-table discovery in tests.
    """
    env_bases = [Path(v) for k, v in os.environ.items()
                 if v and (k == "KICAD_CONFIG_HOME"
                           or (k.startswith("KICAD") and k.endswith("_CONFIG_HOME")))]
    if env_bases:
        return env_bases
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Preferences" / "kicad"]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "kicad"] if appdata else []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return [(Path(xdg) if xdg else home / ".config") / "kicad"]


def _ver_key(name: str) -> tuple:
    out: list[int] = []
    for tok in name.split("."):
        try:
            out.append(int(tok))
        except ValueError:
            out.append(0)
    return tuple(out) or (0,)


def _find_global_table(kind: str, extra_base: Path | None = None) -> Path | None:
    """Locate the newest per-user global ``{sym,fp}-lib-table`` (or ``None``)."""
    fname = "sym-lib-table" if kind == "sym" else "fp-lib-table"
    bases = ([extra_base] if extra_base is not None else []) + _kicad_config_bases()
    for base in bases:
        if base is None or not base.exists():
            continue
        candidates: list[tuple[tuple, Path]] = []
        direct = base / fname
        if direct.exists():
            candidates.append(((10 ** 6,), direct))     # explicit dir wins
        for sub in base.glob("*/" + fname):
            candidates.append((_ver_key(sub.parent.name), sub))
        if candidates:
            candidates.sort(key=lambda c: c[0])
            return candidates[-1][1]
    return None


def discover(project: os.PathLike | str, *,
             kicad_config_home: os.PathLike | str | None = None) -> Workspace:
    """Build a :class:`Workspace` from a project directory (or ``.kicad_pro``).

    Also loads the per-user **global** sym/fp lib-tables (so standard KiCad
    library nicknames resolve). ``kicad_config_home`` overrides discovery of
    the global tables (used by tests and for a non-default config location).
    """
    p = Path(project)
    if p.is_file():
        project_dir = p.parent
        project_file = p if p.suffix == ".kicad_pro" else None
    else:
        project_dir = p
        project_file = next(iter(sorted(project_dir.glob("*.kicad_pro"))), None)
    ws = Workspace(project_dir=project_dir, project_file=project_file)
    sym = project_dir / "sym-lib-table"
    fp = project_dir / "fp-lib-table"
    if sym.exists():
        ws.sym_table = read_table(sym)
    if fp.exists():
        ws.fp_table = read_table(fp)

    extra = Path(kicad_config_home) if kicad_config_home is not None else None
    for kind, attr in (("sym", "global_sym_table"), ("fp", "global_fp_table")):
        gpath = _find_global_table(kind, extra)
        if gpath is not None:
            try:
                setattr(ws, attr, read_table(gpath))
            except Exception:
                pass                          # unreadable global table: smaller scope

    ws.schematics = sorted(project_dir.glob("*.kicad_sch"))
    ws.boards = sorted(project_dir.glob("*.kicad_pcb"))
    return ws


def nearest_project_table(start: os.PathLike | str,
                          kind: str = "fp") -> tuple[Path | None, Path | None]:
    """``(project_dir, table_path)`` for the KiCad project nearest ``start``.

    Walks up from ``start`` (which need not exist yet) to the first directory
    holding a ``{sym,fp}-lib-table`` or a ``*.kicad_pro``. KiCad reads the
    project table from the project directory only, so the walk stops at the
    first project it meets: a table further up belongs to some OTHER project.
    ``(dir, None)`` is a project with no table of that kind; ``(None, None)``
    means no project was found at all.
    """
    fname = "sym-lib-table" if kind == "sym" else "fp-lib-table"
    here = Path(start).resolve()
    for d in [here, *here.parents]:
        try:
            if (d / fname).is_file():
                return d, d / fname
            if any(d.glob("*.kicad_pro")):
                return d, None
        except OSError:                          # unreadable ancestor: keep walking
            continue
    return None, None


def nicknames_for_dir(table: LibTable, directory: os.PathLike | str) -> list[str]:
    """Nicknames of the ``table`` entries whose uri resolves to ``directory``.

    EXACT directory match (both sides ``resolve()``-d, so ``${KIPRJMOD}``,
    ``..`` and symlinked prefixes compare equal) — never an ancestor: a
    ``.pretty`` library is flat (KiCad and
    :func:`.readers.footprint_lib.read_pretty` list one directory, not its
    children), so an entry registering a PARENT of ``directory`` does not
    serve what is in it. Non-KiCad plugin entries and unresolvable ``${VAR}``
    uris are skipped. Table order is preserved.
    """
    if table.path is None:
        return []
    ws = Workspace(project_dir=table.path.parent)
    want = Path(directory).resolve()
    out: list[str] = []
    for e in table.entries:
        if (e.type or "KiCad").lower() != "kicad":
            continue
        path, unresolved = ws.resolve_uri(e.uri)
        if unresolved:
            continue
        try:
            if path.resolve() == want and e.name not in out:
                out.append(e.name)
        except OSError:                          # pragma: no cover - defensive
            continue
    return out


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #
def _split_lib_id(lib_id: str | None) -> tuple[str | None, str | None]:
    if not lib_id or ":" not in lib_id:
        return None, lib_id or None
    nick, name = lib_id.split(":", 1)
    return nick, name


def _load_sym_lib(path: Path) -> set[str] | None:
    try:
        from .readers import kicad_lib
        lib = kicad_lib.read(str(path))
    except Exception:
        return None
    return {s.name for s in lib.symbols}


def _resolve_model(mpath: str, mod_dir: Path, project_dir: Path) -> tuple[Path | None, bool]:
    """Resolve a footprint 3D ``model`` path; returns (path-or-None, portable).

    Only a BARE relative path is taken against ``mod_dir``. A path anchored
    by a variable is already complete once expanded — joining it to
    ``mod_dir`` again is wrong whenever the expansion is itself relative,
    which is exactly ``${KIPRJMOD}`` under ``akcli library audit .`` (the
    project directory is then ``.``, and ``./3d/x.step`` used to be looked up
    inside the ``.pretty``).
    """
    portable = not Path(mpath).is_absolute()
    anchored = mpath.lstrip().startswith("${")
    expanded = mpath
    for var, val in (("${KIPRJMOD}", str(project_dir)),):
        expanded = expanded.replace(var, val)
    expanded = re.sub(
        r"\$\{([^}]+)\}",
        lambda m: expand_var(m.group(1)) or m.group(0),
        expanded,
    )
    if "${" in expanded:
        return None, portable
    p = Path(expanded)
    if not p.is_absolute() and not anchored:
        p = mod_dir / p
    return p, portable


def resolve_model(mpath: str, base_dir: Path,
                  project_dir: Path) -> tuple[Path | None, bool]:
    """Resolve a footprint 3D ``model`` path the way :func:`audit` does.

    ``${KIPRJMOD}`` -> ``project_dir``, other ``${VAR}`` through
    :func:`expand_var`, a relative path against ``base_dir``. Returns
    ``(path, portable)``; ``path`` is ``None`` for an unresolvable variable.
    """
    return _resolve_model(mpath, base_dir, project_dir)


# --------------------------------------------------------------------------- #
# library loading (shared by audit, the library baseline and verify --library)
# --------------------------------------------------------------------------- #
@dataclass
class ModelRef:
    """One 3D ``(model ...)`` reference of a library footprint."""

    ref: str                  # the path exactly as the footprint writes it
    path: Path | None         # resolved file; None = an unresolvable ${VAR}
    portable: bool            # False when written as an absolute path


@dataclass
class LoadedFootprint:
    """One ``.kicad_mod`` of a registered ``.pretty`` library."""

    file: Path
    definition: model.FootprintDef
    models: list[ModelRef] = field(default_factory=list)


@dataclass
class LoadedLibrary:
    """One PROJECT lib-table entry, resolved and (when it exists) loaded.

    ``kind`` is the table slot (``"sym"`` | ``"fp"``). ``unresolved`` lists the
    ``${VAR}`` names the uri could not expand; ``exists`` is whether the
    expanded ``path`` is on disk — content is loaded only when the uri
    resolved AND exists. ``symbols`` is the symbol-name set of a ``.kicad_sym``
    (``None`` when not loaded or unreadable); ``footprints`` holds the
    readable ``.kicad_mod`` files in file-name order and ``unreadable`` the
    ones that did not parse.
    """

    kind: str
    table: LibTable
    entry: LibEntry
    path: Path
    unresolved: list[str] = field(default_factory=list)
    exists: bool = False
    symbols: set[str] | None = None
    footprints: list[LoadedFootprint] = field(default_factory=list)
    unreadable: list[Path] = field(default_factory=list)

    @property
    def loaded(self) -> bool:
        return not self.unresolved and self.exists

    def footprint_defs(self) -> dict[str, model.FootprintDef] | None:
        """``{footprint name: definition}``, or ``None`` when not loaded."""
        if not self.loaded:
            return None
        return {f.definition.name: f.definition for f in self.footprints}


def load_libraries(ws: Workspace,
                   kinds: tuple[str, ...] = ("sym", "fp")) -> list[LoadedLibrary]:
    """Resolve and load every PROJECT lib-table entry, in table order.

    The symbol table's entries come first, then the footprint table's (each
    only when ``kinds`` asks for it). Nothing is reported here: a uri that
    does not expand, a path that does not exist and an unreadable file are
    recorded on the :class:`LoadedLibrary` for the caller to judge — ``audit``
    turns them into findings, the library baseline into explicit "not
    resolved" records. The global tables are never loaded (their libraries
    are KiCad's own, and machine-specific).
    """
    out: list[LoadedLibrary] = []
    for table, kind in ((ws.sym_table, "sym"), (ws.fp_table, "fp")):
        if table is None or kind not in kinds:
            continue
        for e in table.entries:
            path, unresolved = ws.resolve_uri(e.uri)
            lib = LoadedLibrary(kind=kind, table=table, entry=e, path=path,
                                unresolved=unresolved)
            out.append(lib)
            if unresolved:
                continue
            lib.exists = path.exists()
            if not lib.exists:
                continue
            if kind == "sym":
                lib.symbols = _load_sym_lib(path)
                continue
            # Same walk as footprint_lib.read_pretty (file-name order, one
            # unreadable file never hides the rest), keeping WHICH file each
            # definition came from.
            for mod in sorted(path.glob("*.kicad_mod")):
                try:
                    one = footprint_lib.read_kicad_mod(mod)
                except Exception:
                    lib.unreadable.append(mod)
                    continue
                for fdef in one.footprints:
                    refs = []
                    for mpath in fdef.models:
                        resolved, portable = _resolve_model(
                            mpath, path, ws.project_dir)
                        refs.append(ModelRef(ref=mpath, path=resolved,
                                             portable=portable))
                    lib.footprints.append(LoadedFootprint(
                        file=mod, definition=fdef, models=refs))
    return out


def audit(ws: Workspace, sch_paths: list[Path] | None = None) -> list[Finding]:
    """Cross-check schematics <-> lib tables <-> libraries <-> 3D models.

    ``refs`` carry what a config ``[[waiver]]`` can scope on
    (:func:`.report.apply_waivers` matches ``refs``, never ``anchors``): the
    component designator for a per-component finding, the table nickname
    (``nick``, or ``nick:footprint`` for one footprint) for a library one.
    """
    return [f for f, _origin in audit_with_origins(ws, sch_paths)]


def audit_with_origins(
    ws: Workspace, sch_paths: list[Path] | None = None,
) -> list[tuple[Finding, Path | None]]:
    """:func:`audit`, paired with the file each finding is ABOUT.

    The origin is the schematic for a per-component finding, the library
    (``.kicad_sym`` file / ``.pretty`` directory) for one about its contents,
    the lib-table for a bad table entry, and ``None`` when there is no file to
    point at. ``library repair`` uses it to tell "the repair left an error in
    a file it rewrote" from "the project already had an unrelated error" —
    kept BESIDE the finding rather than on it, so the exported finding shape
    does not change.
    """
    pairs: list[tuple[Finding, Path | None]] = []

    def out(finding: Finding, origin: Path | None = None) -> None:
        pairs.append((finding, origin))

    if ws.sym_table is None:
        out(Finding("LIB_TABLE_MISSING", Severity.NOTE,
                    f"no sym-lib-table in {ws.project_dir} "
                    "(symbols resolve only through the global table)"))
    if ws.fp_table is None:
        out(Finding("LIB_TABLE_MISSING", Severity.NOTE,
                    f"no fp-lib-table in {ws.project_dir} "
                    "(footprints resolve only through the global table)"))

    # --- registered libraries: URI resolution + content -------------------- #
    sym_symbols: dict[str, set[str] | None] = {}
    fp_defs: dict[str, dict[str, model.FootprintDef] | None] = {}

    for lib in load_libraries(ws):
        table, e, path = lib.table, lib.entry, lib.path
        if lib.unresolved:
            out(Finding("LIB_URI_UNRESOLVED", Severity.ERROR,
                        f"{table.kind}-lib-table '{e.name}': cannot expand "
                        f"{', '.join('${' + v + '}' for v in lib.unresolved)} "
                        f"in uri {e.uri!r}", refs=[e.name]), table.path)
            (sym_symbols if lib.kind == "sym" else fp_defs)[e.name] = None
            continue
        if not lib.exists:
            out(Finding("LIB_PATH_MISSING", Severity.ERROR,
                        f"{table.kind}-lib-table '{e.name}': uri resolves to "
                        f"{path}, which does not exist", refs=[e.name]),
                table.path)
            (sym_symbols if lib.kind == "sym" else fp_defs)[e.name] = None
            continue
        if lib.kind == "sym":
            if lib.symbols is None:
                out(Finding("LIB_UNREADABLE", Severity.ERROR,
                            f"sym-lib-table '{e.name}': {path} is not a "
                            "readable .kicad_sym", refs=[e.name]), path)
            sym_symbols[e.name] = lib.symbols
            continue
        fp_defs[e.name] = lib.footprint_defs()
        for loaded in lib.footprints:
            f = loaded.definition
            fp_ref = f"{e.name}:{f.name}"
            for w in f.warnings:
                if w.startswith("LEGACY_FORMAT"):
                    out(Finding(
                        "FOOTPRINT_LEGACY_FORMAT", Severity.WARNING,
                        f"{e.name}:{f.name}: {w}", refs=[fp_ref],
                        anchors=[anchor("component", f.name)]), path)
            for m in loaded.models:
                if m.path is None:
                    out(Finding(
                        "MODEL_UNRESOLVED", Severity.WARNING,
                        f"{e.name}:{f.name}: 3D model {m.ref!r} uses an "
                        "unresolvable path variable", refs=[fp_ref]),
                        path)
                elif not m.path.exists():
                    out(Finding(
                        "MODEL_MISSING", Severity.WARNING,
                        f"{e.name}:{f.name}: 3D model {m.ref!r} -> "
                        f"{m.path} does not exist", refs=[fp_ref]),
                        path)
                elif not m.portable:
                    out(Finding(
                        "MODEL_NOT_PORTABLE", Severity.NOTE,
                        f"{e.name}:{f.name}: 3D model path is absolute — "
                        "usable on this machine, breaks on other "
                        "checkouts/machines", refs=[fp_ref]), path)

    # --- schematics: symbol + footprint references ------------------------- #
    for sch_path in (sch_paths if sch_paths is not None else ws.schematics):
        try:
            from .readers import kicad as kreader
            sch = kreader.read_sch(str(sch_path))
        except Exception as exc:
            out(Finding("SCH_UNREADABLE", Severity.ERROR,
                        f"{sch_path.name}: unreadable schematic ({exc})"),
                sch_path)
            continue
        for comp in sch.components:
            nick, _sym = _split_lib_id(comp.library_ref)
            if nick:
                where = ws.locate_sym_nick(nick)
                if where is None and ws.has_global_sym:
                    out(Finding(
                        "SYMBOL_LIB_UNREGISTERED", Severity.WARNING,
                        f"{sch_path.name} {comp.designator}: symbol library "
                        f"nickname '{nick}' is in neither the project nor the "
                        "global sym-lib-table (embedded copy still renders; "
                        "re-linking will fail)",
                        refs=[comp.designator],
                        anchors=[anchor("component", comp.designator)]),
                        sch_path)
                elif where is None:
                    out(Finding(
                        "SYMBOL_LIB_UNREGISTERED", Severity.NOTE,
                        f"{sch_path.name} {comp.designator}: symbol library "
                        f"nickname '{nick}' is not in the project sym-lib-table; "
                        "the global table could not be read to confirm it is a "
                        "standard KiCad library",
                        refs=[comp.designator],
                        anchors=[anchor("component", comp.designator)]),
                        sch_path)
                elif where == "project":
                    known = sym_symbols.get(nick)
                    if known is not None and _sym and _sym not in known:
                        out(Finding(
                            "SYMBOL_MISSING", Severity.WARNING,
                            f"{sch_path.name} {comp.designator}: symbol "
                            f"'{comp.library_ref}' not found in the registered "
                            "library",
                            refs=[comp.designator],
                            anchors=[anchor("component", comp.designator)]),
                            sch_path)
                # where == "global": resolved via a standard library (not loaded)
            fp_nick, fp_name = _split_lib_id(comp.footprint)
            if not comp.footprint:
                continue
            if fp_nick is None:
                out(Finding(
                    "FOOTPRINT_FIELD_MALFORMED", Severity.WARNING,
                    f"{sch_path.name} {comp.designator}: Footprint field "
                    f"{comp.footprint!r} has no library nickname",
                    refs=[comp.designator],
                    anchors=[anchor("component", comp.designator)]), sch_path)
                continue
            fp_where = ws.locate_fp_nick(fp_nick)
            if fp_where is None and ws.has_global_fp:
                out(Finding(
                    "FOOTPRINT_LIB_UNREGISTERED", Severity.ERROR,
                    f"{sch_path.name} {comp.designator}: footprint nickname "
                    f"'{fp_nick}' (from {comp.footprint!r}) is in neither the "
                    "project nor the global fp-lib-table — KiCad will not find "
                    "this package",
                    refs=[comp.designator],
                    anchors=[anchor("component", comp.designator)]), sch_path)
                continue
            if fp_where is None:
                out(Finding(
                    "FOOTPRINT_LIB_UNREGISTERED", Severity.WARNING,
                    f"{sch_path.name} {comp.designator}: footprint nickname "
                    f"'{fp_nick}' (from {comp.footprint!r}) is not in the "
                    "project fp-lib-table; the global table could not be read "
                    "to confirm it is a standard KiCad library",
                    refs=[comp.designator],
                    anchors=[anchor("component", comp.designator)]), sch_path)
                continue
            if fp_where == "global":
                continue        # resolved via a standard library (not loaded)
            defs = fp_defs.get(fp_nick)
            if defs is not None and fp_name and fp_name not in defs:
                out(Finding(
                    "FOOTPRINT_MISSING", Severity.ERROR,
                    f"{sch_path.name} {comp.designator}: footprint "
                    f"'{comp.footprint}' not found in the registered library",
                    refs=[comp.designator],
                    anchors=[anchor("component", comp.designator)]), sch_path)
    return pairs


# --------------------------------------------------------------------------- #
# repair plans (the two historically hand-sed'ed fixes)
# --------------------------------------------------------------------------- #
@dataclass
class RepairEdit:
    path: Path
    description: str
    new_text: str


def plan_rename(ws: Workspace, old: str, new: str) -> list[RepairEdit]:
    """Plan nickname rewrites ``old:`` -> ``new:`` in Footprint fields.

    Touches every registered symbol library's ``.kicad_sym`` and every project
    schematic — the exact scope of the historic
    ``sed 's/"footprint:/"proj_jlc:/'`` workaround, but via the lossless
    S-expression parser instead of raw text.
    """
    edits: list[RepairEdit] = []
    targets: list[Path] = []
    if ws.sym_table is not None:
        for e in ws.sym_table.entries:
            path, unresolved = ws.resolve_uri(e.uri)
            if not unresolved and path.exists() and path.suffix == ".kicad_sym":
                targets.append(path)
    targets.extend(ws.schematics)

    prefix = f"{old}:"
    for path in targets:
        # Lenient decode + swallow: this is a best-effort SCAN across every
        # registered library, not a read of the document under edit. One
        # unreadable third-party file must not abort the repair plan for the
        # rest (see read_table for the same rationale).
        try:
            root = sexpr.parse(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        count = 0

        def _walk(node: sexpr.SNode) -> None:
            nonlocal count
            if node.is_atom or not node.children:
                return
            if (node.tag == "property" and len(node.children) >= 3
                    and node.children[1].is_atom
                    and node.children[1].value == "Footprint"):
                val = node.children[2]
                if val.is_atom and (val.value or "").startswith(prefix):
                    new_val = new + ":" + (val.value or "")[len(prefix):]
                    val.text = _quote(new_val)
                    count += 1
            for child in node.children:
                _walk(child)

        _walk(root)
        if count:
            edits.append(RepairEdit(
                path=path,
                description=f"{path.name}: {count} Footprint field(s) "
                            f"'{old}:*' -> '{new}:*'",
                new_text=sexpr.dumps(root),
            ))
    return edits


def plan_model_paths(ws: Workspace, mode: str) -> list[RepairEdit]:
    """Plan 3D-model path rewrites in every registered footprint library.

    ``mode``: ``"absolute"`` (usable on this machine, not portable) or a
    ``${VAR}``-style prefix replacing the model's directory part.
    """
    if mode != "absolute" and not mode.startswith("$"):
        raise ValueError(
            f"bad 3d-path mode {mode!r}: expected 'absolute' or a '${{VAR}}' prefix")
    edits: list[RepairEdit] = []
    if ws.fp_table is None:
        return edits
    for e in ws.fp_table.entries:
        lib_dir, unresolved = ws.resolve_uri(e.uri)
        if unresolved or not lib_dir.is_dir():
            continue
        for mod in sorted(lib_dir.glob("*.kicad_mod")):
            # Lenient decode + swallow, same rationale as plan_footprint_lib:
            # a best-effort sweep of third-party footprint libraries.
            try:
                root = sexpr.parse(mod.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            count = 0
            for mnode in root.find_all("model"):
                if len(mnode.children) < 2 or not mnode.children[1].is_atom:
                    continue
                val = mnode.children[1].value or ""
                if val.startswith("$") or Path(val).is_absolute():
                    continue                      # already policy-managed
                if mode == "absolute":
                    new_val = (lib_dir / val).resolve().as_posix()
                else:
                    new_val = mode.rstrip("/") + "/" + Path(val).name
                mnode.children[1].text = _quote(new_val)
                count += 1
            if count:
                edits.append(RepairEdit(
                    path=mod,
                    description=f"{e.name}/{mod.name}: {count} 3D model "
                                f"path(s) -> {mode}",
                    new_text=sexpr.dumps(root),
                ))
    return edits
