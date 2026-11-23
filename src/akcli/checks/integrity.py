"""Tree-scoped document integrity — what ``akcli check --integrity`` runs.

:func:`akcli.writers.connectivity.verify` judges ONE parsed document. That is
the right scope for the write gate (it runs on the temp file of the sheet being
written) and the wrong one for a read-only check of a design: asked about the
root of a hierarchy it never opened a child, so a duplicate designator living
in — or split across — child sheets and a grandchild that is not on disk were
invisible, and a child that is not on disk killed the command before a single
finding was printed.

:func:`run_tree` walks the sheet tree DOWNWARD from the file it is given (no
root discovery: the scope is that file and everything it instantiates) and
returns one flat finding list:

* **Every file's single-document findings**, each file verified ONCE however
  many times it is instantiated. The given file's messages are unchanged (a
  flat schematic reports byte-for-byte what it always did) unless the caller
  asks for ``label_root``; a child's are prefixed with its path relative to
  the given file's directory (``amp.kicad_sch: ...``).
  ``SHEET_FILE_MISSING`` needs no logic of its own —
  the per-file verifier already emits it for a ``(sheet ...)`` whose child is
  not there, so a missing grandchild is simply the child's own finding; the
  walk carries on with the children that do exist.
* **``DUPLICATE_DESIGNATOR`` across the tree**, counted the way eeschema
  resolves references: per LIVE sheet instance, the reference the symbol
  answers to there (:func:`akcli.readers.kicad.sheet_instance_refs` — the
  reader's own rule, so this pass and ``akcli nets`` never resolve one symbol
  to two different designators). Counting what RESOLVES, rather than the
  ``(path ...)`` entries a file happens to carry, is what keeps stale entries
  out: an entry for another project's tree, or for a sheet instance that no
  longer exists, names no live instance and is never counted — while an
  instance a symbol has NO entry for inherits the symbol's first entry,
  exactly as in KiCad (``tests/fixtures/kicad/hier_partial/``), and that
  duplicate IS reported.
  A properly annotated sheet instantiated twice is clean: each instance
  resolves its own reference.

Two deliberate omissions from the tree-level count:

* ``?`` placeholders — an unannotated symbol is not a duplicate of the next
  unannotated symbol (the per-file verifier's rule).
* ``#``-prefixed virtual references (power ports, ``PWR_FLAG``). They never
  reach a netlist, BOM or board, and ``#PWR0<n>`` is allocated per FILE
  (``writers.instances.alloc_pwr_ref``), so every multi-sheet design akcli
  writes repeats them across sheets by construction; reporting that here would
  fail designs for something no op can fix. The per-file verifier still
  reports a repeat INSIDE one file.

A reference the per-file pass already reported is not reported again.

Unreadable input: the given file itself raises (the caller's reader reports
that with its own precise error); a CHILD that exists but cannot be parsed is
skipped for the same reason ``project.tree_files`` skips it — every caller
loads the tree through :func:`akcli.readers.kicad.read_sch` right after, and
that names the file and the fault.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from .. import sexpr
from ..errors import AkcliError
from ..readers import kicad as kreader
from ..report import Finding, Severity
from ..safety import read_text_guarded
from ..sexpr import query as _query
from ..writers import connectivity

__all__ = ["run_tree"]

# Same cap as the reader's sheet recursion and ``project.tree_files``.
_MAX_DEPTH = 16

# Placements spelled out in one tree-level message; the rest is a count.
_MAX_LISTED = 6


def _parse(path: Path) -> sexpr.SNode:
    """One file through the reader's own text guard (size cap, strict UTF-8)."""
    return sexpr.parse(read_text_guarded(path))


def _label(path: Path, base: Path) -> str:
    """``path`` relative to the given file's directory, POSIX-style."""
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:  # no relative path exists (another drive on Windows)
        return path.as_posix()


def run_tree(root: os.PathLike[str] | str, *,
             label_root: bool = False) -> list[Finding]:
    """Integrity findings for ``root`` and every sheet file it instantiates.

    Read-only and KiCad-free. See the module docstring for the scope and the
    counting rule; raises whatever reading ``root`` itself raises
    (``FileNotFoundError`` for a missing file, ``AkcliError`` for one that does
    not parse). ``label_root`` prefixes the given file's own messages with its
    name too — for a caller reporting on a PROJECT (``project check``), where
    "this file" would otherwise be anyone's guess.
    """
    start = Path(root).resolve()
    base = start.parent
    root_doc = _parse(start)
    root_uuid = _query.root_uuid(root_doc) or ""

    findings: list[Finding] = []
    docs: dict[Path, sexpr.SNode] = {start: root_doc}
    verified: set[Path] = set()
    unreadable: set[Path] = set()
    # References a per-file DUPLICATE_DESIGNATOR already names (its refs are
    # ``[reference]``): the tree pass must not say the same thing twice.
    flagged: set[str] = set()
    # (reference, unit) -> where it resolves, in walk order.
    placements: dict[tuple[str, int], list[str]] = {}

    # Depth-first in document order, like the reader's own sheet walk, on an
    # explicit stack. One frame per sheet INSTANCE: (file, instance path below
    # the root uuid, human-readable sheet path, resolved ancestor files).
    stack: list[tuple[Path, str, str, tuple[Path, ...]]] = [
        (start, "", "/", (start,))]
    while stack:
        path, inst_path, name_path, ancestors = stack.pop()
        if path in unreadable:
            continue
        doc = docs[path]
        label = _label(path, base)
        prefix = label if (label_root or path != start) else ""
        # The reader's ``want_path`` for this instance, verbatim.
        want_path = f"/{root_uuid}{inst_path}" if root_uuid else None

        try:
            own = ([] if path in verified
                   else connectivity.verify(doc, path=str(path)))
            refs = kreader.sheet_instance_refs(doc, want_path)
        except AkcliError:
            # Parsed as an S-expression but not as a schematic (a mangled
            # number, a broken lib_symbols entry): the module docstring's
            # unreadable-input rule — the given file raises, a child is left
            # to the reader.
            if path == start:
                raise
            unreadable.add(path)
            continue
        verified.add(path)

        for f in own:
            if f.code == connectivity.DUPLICATE_DESIGNATOR:
                flagged.update(str(r) for r in f.refs)
            findings.append(
                replace(f, message=f"{prefix}: {f.message}") if prefix else f)

        where = f"{name_path} ({label})"
        for ref, unit in refs:
            if "?" in ref or ref.startswith("#"):
                continue
            placements.setdefault((ref, unit), []).append(where)

        children: list[tuple[Path, str, str, tuple[Path, ...]]] = []
        for suuid, sname, sfile in kreader.doc_sheet_refs(doc):
            if not sfile:
                continue
            child = (path.parent / sfile).resolve()
            # A cycle or a runaway depth is refused by the reader on load;
            # here it only has to not loop.
            if child in ancestors or len(ancestors) >= _MAX_DEPTH:
                continue
            if child not in docs:
                # Missing: this file's own verify reported SHEET_FILE_MISSING.
                if not child.is_file():
                    continue
                try:
                    child_doc = _parse(child)
                except (AkcliError, OSError):
                    continue
                if child_doc.tag != "kicad_sch":
                    continue
                docs[child] = child_doc
            children.append((child, f"{inst_path}/{suuid}",
                             f"{name_path}{sname}/", (*ancestors, child)))
        stack.extend(reversed(children))

    for (ref, unit), places in placements.items():
        if len(places) < 2 or ref in flagged:
            continue
        listed = ", ".join(places[:_MAX_LISTED])
        if len(places) > _MAX_LISTED:
            listed += f", +{len(places) - _MAX_LISTED} more"
        findings.append(
            Finding(
                connectivity.DUPLICATE_DESIGNATOR,
                Severity.ERROR,
                f"designator {ref!r} unit {unit} resolves on {len(places)} "
                f"placements across the sheet tree: {listed} — each part+unit "
                f"must be annotated exactly once in the design; a sheet "
                f"instance a symbol has no (path ...) entry for inherits that "
                f"symbol's first entry, as in eeschema",
                refs=[ref],
            )
        )
    return findings
