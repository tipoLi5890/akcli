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

Project tables only: the global table lives in a per-user KiCad config this
tool does not own; an unresolvable nickname is reported as "not in the PROJECT
tables" so a globally-registered library reads as a NOTE, not a false ERROR.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import model
from .readers import footprint_lib, sexpr
from .report import Finding, Severity, anchor

__all__ = [
    "LibEntry", "LibTable", "Workspace", "read_table", "discover", "audit",
    "plan_rename", "plan_model_paths", "RepairEdit",
]


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
    schematics: list[Path] = field(default_factory=list)
    boards: list[Path] = field(default_factory=list)

    def resolve_uri(self, uri: str) -> tuple[Path, list[str]]:
        """Expand ``${KIPRJMOD}``/env vars in a table URI; returns (path, unresolved)."""
        unresolved: list[str] = []

        def _sub(m: re.Match) -> str:
            var = m.group(1)
            if var == "KIPRJMOD":
                return str(self.project_dir)
            val = os.environ.get(var)
            if val is None:
                unresolved.append(var)
                return m.group(0)
            return val

        expanded = re.sub(r"\$\{([^}]+)\}", _sub, uri)
        p = Path(expanded)
        if not p.is_absolute():
            p = self.project_dir / p
        return p, unresolved


def read_table(path: os.PathLike | str) -> LibTable:
    """Parse a ``sym-lib-table`` / ``fp-lib-table`` file."""
    p = Path(path)
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
    return table


def discover(project: os.PathLike | str) -> Workspace:
    """Build a :class:`Workspace` from a project directory (or ``.kicad_pro``)."""
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
        lambda m: os.environ.get(m.group(1), m.group(0)),
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
            if nick and ws.sym_table is not None:
                known = sym_symbols.get(nick)
                if ws.sym_table.get(nick) is None:
                    out(Finding(
                        "SYMBOL_LIB_UNREGISTERED", Severity.WARNING,
                        f"{sch_path.name} {comp.designator}: symbol library "
                        f"nickname '{nick}' is not in the project sym-lib-table "
                        "(embedded copy still renders; re-linking will fail)",
                        anchors=[anchor("component", comp.designator)]))
                elif known is not None and _sym and _sym not in known:
                    out(Finding(
                        "SYMBOL_MISSING", Severity.WARNING,
                        f"{sch_path.name} {comp.designator}: symbol "
                        f"'{comp.library_ref}' not found in the registered "
                        "library",
                        anchors=[anchor("component", comp.designator)]))
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
            if ws.fp_table is not None and ws.fp_table.get(fp_nick) is None:
                out(Finding(
                    "FOOTPRINT_LIB_UNREGISTERED", Severity.ERROR,
                    f"{sch_path.name} {comp.designator}: footprint nickname "
                    f"'{fp_nick}' (from {comp.footprint!r}) is not in the "
                    "project fp-lib-table — KiCad will not find this package",
                    anchors=[anchor("component", comp.designator)]))
                continue
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
                    val.text = '"' + new_val.replace('"', '\\"') + '"'
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
                mnode.children[1].text = '"' + new_val.replace('"', '\\"') + '"'
                count += 1
            if count:
                edits.append(RepairEdit(
                    path=mod,
                    description=f"{e.name}/{mod.name}: {count} 3D model "
                                f"path(s) -> {mode}",
                    new_text=sexpr.dumps(root),
                ))
    return edits
