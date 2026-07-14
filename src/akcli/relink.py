"""Re-embed stale ``lib_symbols`` cache entries from fresh ``.kicad_sym`` files.

A ``.kicad_sch`` is self-contained: every placed symbol's electrical truth lives
in the inline ``(lib_symbols ...)`` cache, frozen at the moment the symbol was
first placed. When the source library moves on (pins renamed, graphics fixed,
format modernized) the cache silently drifts — KiCad only refreshes it when a
*human* re-places the part. The manual fix is surgery: extract the balanced-paren
``(symbol "Name" ...)`` block from ``<libdir>/<Nick>.kicad_sym``, rename it to
``"Nick:Name"``, and splice it over the embedded block. This module automates
exactly that procedure (the extraction is done by the lossless
:mod:`.readers.sexpr` parser instead of hand-counted parens):

* :func:`plan` — compare every embedded ``(symbol "Nick:Name" ...)`` against the
  fresh definition from ``<libdir>/<Nick>.kicad_sym`` (``(extends ...)`` derived
  symbols flattened the way KiCad itself caches them, via
  :mod:`.writers.lib_cache`) and return one action per cache entry:
  ``up-to-date`` (token-identical, whitespace ignored), ``replace`` (differs;
  the action carries the renamed fresh block as ``new_sexpr``), or
  ``missing-lib`` (no source found). Read-only.
* :func:`apply` — splice the ``replace`` actions into the document and write it
  atomically (``<name>.bak`` first). **Safety gate (non-negotiable):** after
  splicing, BOTH versions are re-read through :mod:`.readers.kicad` +
  :mod:`.netbuild` and their net membership sets must be identical — a relink
  must refresh definitions, never rewire the board. Any difference refuses the
  write with ``VERIFY_FAILED``.

Whitespace: a replaced block keeps the library file's internal formatting,
deepened one tab to sit at the cache's nesting level; untouched nodes round-trip
byte-for-byte (the :class:`~.readers.sexpr.SNode` contract).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from . import model
from .errors import fail
from .readers import kicad, kicad_lib, sexpr
from .readers.kicad_lib import _read_text
from .readers.sexpr import SNode
from .writers import lib_cache

__all__ = ["DEFAULT_LIB_DIR", "plan", "apply", "resolve_lib_dirs", "lib_file_for"]

# Where the macOS KiCad app ships its standard symbol libraries; used when the
# caller supplies no library dirs (and only if it actually exists).
DEFAULT_LIB_DIR = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols")


# --------------------------------------------------------------------------- #
# library-source resolution
# --------------------------------------------------------------------------- #
def resolve_lib_dirs(lib_dirs: object = None) -> list[Path]:
    """Normalize the caller's library sources; default to the KiCad.app dir."""
    if lib_dirs:
        return [Path(os.fspath(d)) for d in lib_dirs]
    return [DEFAULT_LIB_DIR] if DEFAULT_LIB_DIR.is_dir() else []


def lib_file_for(nick: str, lib_dirs: list[Path]) -> Path | None:
    """The ``.kicad_sym`` providing library ``nick`` (first hit wins).

    Each entry may be a directory (searched for ``<nick>.kicad_sym``) or a
    ``.kicad_sym`` file (matched by its stem), so ``--symbols``-style file
    arguments work as sources too.
    """
    for d in lib_dirs:
        if d.is_dir():
            cand = d / f"{nick}.kicad_sym"
            if cand.is_file():
                return cand
        elif d.suffix.lower() == ".kicad_sym" and d.stem == nick and d.is_file():
            return d
    return None


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def plan(sch_path: os.PathLike | str, lib_dirs: object = None,
         only: object = None) -> list[dict]:
    """Compare embedded cache entries against fresh library definitions.

    Returns one action dict per considered cache entry::

        {"lib_id": "Device:R", "status": "up-to-date" | "replace" | "missing-lib",
         "source": "<.kicad_sym path or None>", "detail": "<reason or None>",
         "new_sexpr": "<renamed fresh block>"}   # replace actions only

    ``only`` restricts the pass to the given library nicknames (or full
    lib_ids); it accepts an iterable or a comma-separated string. Read-only:
    feed the result to :func:`apply` to perform the splice.
    """
    p = Path(os.fspath(sch_path))
    doc = sexpr.parse(_read_text(p))
    if doc.tag != "kicad_sch":
        fail("ALTIUM_MALFORMED", f"not a kicad_sch: root tag {doc.tag!r}")
    libsyms = doc.find("lib_symbols")
    dirs = resolve_lib_dirs(lib_dirs)
    only_set = _normalize_only(only)

    libs: dict[Path, model.Library] = {}
    actions: list[dict] = []
    for sym in (libsyms.find_all("symbol") if libsyms is not None else []):
        lib_id = _symbol_name(sym) or ""
        nick, colon, name = lib_id.partition(":")
        if only_set is not None and nick not in only_set and lib_id not in only_set:
            continue
        if not colon or not name:
            actions.append(_action(
                lib_id, "missing-lib", None,
                "cache entry is not library-qualified (expected \"Nick:Name\")"))
            continue
        lib_file = lib_file_for(nick, dirs)
        if lib_file is None:
            where = ", ".join(map(str, dirs)) or "(no library dirs)"
            actions.append(_action(
                lib_id, "missing-lib", None, f"no {nick}.kicad_sym under {where}"))
            continue
        lib = libs.get(lib_file)
        if lib is None:
            lib = libs[lib_file] = kicad_lib.read(lib_file)
        srcdef = next((s for s in lib.symbols if s.name == name), None)
        if srcdef is None:
            actions.append(_action(
                lib_id, "missing-lib", str(lib_file),
                f"symbol {name!r} not in {lib_file.name}"))
            continue

        # The fresh block, exactly as the manual procedure produced it: the
        # library body ((extends ...) flattened KiCad-save style) renamed to
        # the qualified id, child units left unqualified.
        fresh = lib_cache._flattened_body(srcdef, [lib])
        lib_cache._set_symbol_name(fresh, lib_id)
        if _canon(fresh) == _canon(sym):
            actions.append(_action(lib_id, "up-to-date", str(lib_file), None))
        else:
            _deepen(fresh, "\t")  # lib depth 1 -> cache depth 2
            act = _action(lib_id, "replace", str(lib_file), None)
            act["new_sexpr"] = sexpr.dumps(fresh)
            actions.append(act)
    return actions


def _action(lib_id: str, status: str, source: str | None,
            detail: str | None) -> dict:
    return {"lib_id": lib_id, "status": status, "source": source, "detail": detail}


def _normalize_only(only: object) -> set[str] | None:
    if only is None:
        return None
    items = only.split(",") if isinstance(only, str) else list(only)  # type: ignore[arg-type]
    out = {str(s).strip() for s in items if str(s).strip()}
    return out or None


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def apply(sch_path: os.PathLike | str, actions: list[dict],
          backup: bool = True) -> dict:
    """Splice the ``replace`` actions into ``sch_path`` (atomic, gated).

    Non-``replace`` actions are ignored, so :func:`plan`'s full output can be
    passed straight in. With nothing to replace the file is left untouched.
    Otherwise the new document must pass the net-membership equivalence gate
    (see module docstring) before ``<name>.bak`` is written (when ``backup``)
    and the file is atomically replaced.

    Returns ``{"path", "replaced": [lib_ids], "backup": path | None,
    "written": bool}``. Raises ``VERIFY_FAILED`` when the gate refuses,
    ``SYMBOL_NOT_FOUND`` when a replace targets a lib_id no longer embedded.
    """
    p = Path(os.fspath(sch_path))
    original = _read_text(p)
    doc = sexpr.parse(original)
    if doc.tag != "kicad_sch":
        fail("ALTIUM_MALFORMED", f"not a kicad_sch: root tag {doc.tag!r}")
    libsyms = doc.find("lib_symbols")

    replaced: list[str] = []
    for act in actions or []:
        if act.get("status") != "replace":
            continue
        lib_id = str(act.get("lib_id") or "")
        new_sexpr = act.get("new_sexpr")
        if not isinstance(new_sexpr, str) or not new_sexpr:
            fail("BAD_CONFIG",
                 f"replace action for {lib_id!r} carries no new_sexpr "
                 "(build actions with relink.plan())")
        idx = _cache_index(libsyms, lib_id)
        if idx is None:
            fail("SYMBOL_NOT_FOUND",
                 f"{lib_id!r} is not embedded in {p.name}'s lib_symbols "
                 "(file changed since plan?)")
        node = sexpr.parse(new_sexpr)
        if node.is_atom or node.tag != "symbol":
            fail("BAD_CONFIG",
                 f"replace action for {lib_id!r}: new_sexpr is not a (symbol ...) block")
        node.prefix = node.suffix = ""   # parent ws slot supplies the indentation
        libsyms.children[idx] = node
        replaced.append(lib_id)

    result = {"path": str(p), "replaced": replaced, "backup": None, "written": False}
    if not replaced:
        return result

    new_text = sexpr.dumps(doc)
    directory = p.parent if str(p.parent) else Path(".")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=p.name + ".relink.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(new_text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        # --- SAFETY GATE: identical net membership, or no write at all ------ #
        # The temp lives next to the original so hierarchical sheet references
        # resolve identically for both reads.
        before = _net_membership(p)
        after = _net_membership(tmp)
        if before != after:
            lost, gained = before - after, after - before
            fail("VERIFY_FAILED",
                 f"relink would change connectivity of {p.name}: "
                 f"{len(lost)} net(s) lost [{_fmt_nets(lost)}], "
                 f"{len(gained)} gained [{_fmt_nets(gained)}] — refusing to write")
        if backup:
            bak = p.with_name(p.name + ".bak")
            shutil.copy2(p, bak)
            result["backup"] = str(bak)
        os.replace(tmp, p)
        result["written"] = True
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return result


def _net_membership(path: os.PathLike | str) -> set[frozenset]:
    """Net membership sets — the connectivity invariant the gate compares."""
    sch = kicad.read_sch(path)
    return {frozenset(net.members) for net in sch.nets}


def _fmt_nets(nets: set[frozenset]) -> str:
    """A few nets as ``R1.2+R2.1`` samples for the refusal message."""
    shown = sorted((sorted(n) for n in nets))[:3]
    text = "; ".join("+".join(f"{d}.{n}" for d, n in net) for net in shown)
    return text + (" ..." if len(nets) > 3 else "")


# --------------------------------------------------------------------------- #
# node helpers (iterative — never native recursion on parsed trees)
# --------------------------------------------------------------------------- #
def _symbol_name(sym: SNode) -> str | None:
    return lib_cache._symbol_name(sym)


def _cache_index(libsyms: SNode | None, lib_id: str) -> int | None:
    """Index of the ``(symbol "lib_id" ...)`` child inside ``lib_symbols``."""
    kids = (libsyms.children or []) if libsyms is not None else []
    for i, ch in enumerate(kids):
        if ch.is_list and ch.tag == "symbol" and _symbol_name(ch) == lib_id:
            return i
    return None


_CLOSE = object()  # sentinel marking "emit the closing paren" on the canon stack


def _canon(node: SNode) -> tuple:
    """Whitespace-insensitive token stream of a node (decoded atom values).

    Two blocks that differ only in formatting (indentation, quoting of the
    same value) canonicalize identically; any structural or value change —
    including pure graphics — does not.
    """
    out: list[str] = []
    stack: list[object] = [node]
    while stack:
        nd = stack.pop()
        if nd is _CLOSE:
            out.append(")")
        elif nd.is_atom:  # type: ignore[union-attr]
            out.append(nd.value or "")  # type: ignore[union-attr]
        else:
            out.append("(")
            stack.append(_CLOSE)
            stack.extend(reversed(nd.children or []))  # type: ignore[union-attr]
    return tuple(out)


def _deepen(node: SNode, extra: str) -> None:
    """Deepen every newline-bearing whitespace run by ``extra`` (in place)."""
    stack: list[SNode] = [node]
    while stack:
        nd = stack.pop()
        if nd.is_atom:
            continue
        nd.ws = [w + extra if "\n" in w else w for w in (nd.ws or [])]
        stack.extend(c for c in (nd.children or []) if c.is_list)
