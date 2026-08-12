"""Project library workspace: ``sym-lib-table`` / ``fp-lib-table`` + audit.

KiCad resolves symbol/footprint nicknames through the project's (and global)
library tables; a nickname mismatch — a symbol whose Footprint field says
``footprint:X`` while the table registers ``proj_jlc`` — is invisible to
ERC and every netlist check, and surfaces only as "footprint not found" in the
GUI. This module makes the project tables a first-class, auditable object:

* :func:`read_table` — parse a ``sym-lib-table`` / ``fp-lib-table``;
* :func:`discover` — build a :class:`Workspace` from a project directory;
* :func:`audit` — findings across schematic <-> tables <-> libraries <-> 3D;
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
    "plan_rename", "plan_model_paths", "RepairEdit", "expand_var",
    "kicad_data_roots",
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
        """Expand ``${KIPRJMOD}``/env vars in a table URI; returns (path, unresolved)."""
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
        if not p.is_absolute():
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
    """Resolve a footprint 3D ``model`` path; returns (path-or-None, portable)."""
    portable = not Path(mpath).is_absolute()
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
    if not p.is_absolute():
        p = mod_dir / p
    return p, portable


def audit(ws: Workspace, sch_paths: list[Path] | None = None) -> list[Finding]:
    """Cross-check schematics <-> lib tables <-> libraries <-> 3D models."""
    findings: list[Finding] = []
    out = findings.append

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

    for table, store in ((ws.sym_table, "sym"), (ws.fp_table, "fp")):
        if table is None:
            continue
        for e in table.entries:
            path, unresolved = ws.resolve_uri(e.uri)
            if unresolved:
                out(Finding("LIB_URI_UNRESOLVED", Severity.ERROR,
                            f"{table.kind}-lib-table '{e.name}': cannot expand "
                            f"{', '.join('${' + v + '}' for v in unresolved)} "
                            f"in uri {e.uri!r}"))
                (sym_symbols if store == "sym" else fp_defs)[e.name] = None
                continue
            if not path.exists():
                out(Finding("LIB_PATH_MISSING", Severity.ERROR,
                            f"{table.kind}-lib-table '{e.name}': uri resolves to "
                            f"{path}, which does not exist"))
                (sym_symbols if store == "sym" else fp_defs)[e.name] = None
                continue
            if store == "sym":
                syms = _load_sym_lib(path)
                if syms is None:
                    out(Finding("LIB_UNREADABLE", Severity.ERROR,
                                f"sym-lib-table '{e.name}': {path} is not a "
                                "readable .kicad_sym"))
                sym_symbols[e.name] = syms
            else:
                lib = footprint_lib.read_pretty(path)
                defs = {f.name: f for f in lib.footprints}
                fp_defs[e.name] = defs
                for f in lib.footprints:
                    for w in f.warnings:
                        if w.startswith("LEGACY_FORMAT"):
                            out(Finding(
                                "FOOTPRINT_LEGACY_FORMAT", Severity.WARNING,
                                f"{e.name}:{f.name}: {w}",
                                anchors=[anchor("component", f.name)]))
                    for mpath in f.models:
                        resolved, portable = _resolve_model(
                            mpath, path, ws.project_dir)
                        if resolved is None:
                            out(Finding(
                                "MODEL_UNRESOLVED", Severity.WARNING,
                                f"{e.name}:{f.name}: 3D model {mpath!r} uses an "
                                "unresolvable path variable"))
                        elif not resolved.exists():
                            out(Finding(
                                "MODEL_MISSING", Severity.WARNING,
                                f"{e.name}:{f.name}: 3D model {mpath!r} -> "
                                f"{resolved} does not exist"))
                        elif not portable:
                            out(Finding(
                                "MODEL_NOT_PORTABLE", Severity.NOTE,
                                f"{e.name}:{f.name}: 3D model path is absolute — "
                                "usable on this machine, breaks on other "
                                "checkouts/machines"))

    # --- schematics: symbol + footprint references ------------------------- #
    for sch_path in (sch_paths if sch_paths is not None else ws.schematics):
        try:
            from .readers import kicad as kreader
            sch = kreader.read_sch(str(sch_path))
        except Exception as exc:
            out(Finding("SCH_UNREADABLE", Severity.ERROR,
                        f"{sch_path.name}: unreadable schematic ({exc})"))
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
                        anchors=[anchor("component", comp.designator)]))
                elif where is None:
                    out(Finding(
                        "SYMBOL_LIB_UNREGISTERED", Severity.NOTE,
                        f"{sch_path.name} {comp.designator}: symbol library "
                        f"nickname '{nick}' is not in the project sym-lib-table; "
                        "the global table could not be read to confirm it is a "
                        "standard KiCad library",
                        anchors=[anchor("component", comp.designator)]))
                elif where == "project":
                    known = sym_symbols.get(nick)
                    if known is not None and _sym and _sym not in known:
                        out(Finding(
                            "SYMBOL_MISSING", Severity.WARNING,
                            f"{sch_path.name} {comp.designator}: symbol "
                            f"'{comp.library_ref}' not found in the registered "
                            "library",
                            anchors=[anchor("component", comp.designator)]))
                # where == "global": resolved via a standard library (not loaded)
            fp_nick, fp_name = _split_lib_id(comp.footprint)
            if not comp.footprint:
                continue
            if fp_nick is None:
                out(Finding(
                    "FOOTPRINT_FIELD_MALFORMED", Severity.WARNING,
                    f"{sch_path.name} {comp.designator}: Footprint field "
                    f"{comp.footprint!r} has no library nickname",
                    anchors=[anchor("component", comp.designator)]))
                continue
            fp_where = ws.locate_fp_nick(fp_nick)
            if fp_where is None and ws.has_global_fp:
                out(Finding(
                    "FOOTPRINT_LIB_UNREGISTERED", Severity.ERROR,
                    f"{sch_path.name} {comp.designator}: footprint nickname "
                    f"'{fp_nick}' (from {comp.footprint!r}) is in neither the "
                    "project nor the global fp-lib-table — KiCad will not find "
                    "this package",
                    anchors=[anchor("component", comp.designator)]))
                continue
            if fp_where is None:
                out(Finding(
                    "FOOTPRINT_LIB_UNREGISTERED", Severity.WARNING,
                    f"{sch_path.name} {comp.designator}: footprint nickname "
                    f"'{fp_nick}' (from {comp.footprint!r}) is not in the "
                    "project fp-lib-table; the global table could not be read "
                    "to confirm it is a standard KiCad library",
                    anchors=[anchor("component", comp.designator)]))
                continue
            if fp_where == "global":
                continue        # resolved via a standard library (not loaded)
            defs = fp_defs.get(fp_nick)
            if defs is not None and fp_name and fp_name not in defs:
                out(Finding(
                    "FOOTPRINT_MISSING", Severity.ERROR,
                    f"{sch_path.name} {comp.designator}: footprint "
                    f"'{comp.footprint}' not found in the registered library",
                    anchors=[anchor("component", comp.designator)]))
    return findings


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
