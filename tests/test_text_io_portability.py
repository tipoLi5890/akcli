"""Text-I/O portability gate — the recurring Windows-CI failure class, frozen.

Every release that broke on windows-latest broke on the same physics: CPython
text mode defaults to the LOCALE encoding (cp1252 on Windows runners, not
UTF-8) and to universal-newline translation (``\\n`` -> ``\\r\\n`` on write).
Code developed on macOS/Linux never sees either until CI does:

* 0.8.0 — review tests crashed decoding UTF-8 content read without
  ``encoding=`` (cp1252);
* 0.10.0 — ``--render`` previews wrote SVG via ``write_text`` without
  ``newline=``, so the on-disk file was CRLF-inflated and byte-count
  assertions (and the renderer's "same input bytes, same SVG bytes"
  determinism promise) failed on Windows only.

This test makes the fix structural instead of whack-a-mole: an AST scan over
``src/akcli`` plus the dev/CI-executed trees ``tools/`` and ``hooks/``
(vendored code excluded) requires every ``Path.read_text`` to pin
``encoding=`` and every ``Path.write_text`` / text-mode builtin ``open``
write to pin BOTH ``encoding=`` and ``newline=`` (readers may pin newline
too, but translation on read is harmless). A dynamic ``**kwargs`` splat
counts as providing them (the caller decided).

``tools/`` and ``hooks/`` are in scope because they run on the Windows CI
matrix too: the hook is spawned by the harness on every ``draw --apply``, and
the tools write goldens/baselines/manifests whose bytes are asserted
elsewhere — a CRLF-inflated golden is exactly the 0.10.0 failure again.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (ROOT / "src" / "akcli", ROOT / "tools", ROOT / "hooks")


def _py_files() -> list[Path]:
    out: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.is_dir():
            continue
        out += [p for p in sorted(root.rglob("*.py"))
                if "_vendor" not in p.parts
                and "__pycache__" not in p.parts]
    return out


def _kwarg_names(call: ast.Call) -> set[str]:
    names = {k.arg for k in call.keywords if k.arg is not None}
    if any(k.arg is None for k in call.keywords):   # **kwargs splat
        names |= {"encoding", "newline"}
    return names


def _open_mode(call: ast.Call) -> str:
    for k in call.keywords:
        if k.arg == "mode" and isinstance(k.value, ast.Constant):
            return str(k.value.value)
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant) \
            and isinstance(call.args[1].value, str):
        return call.args[1].value
    return "r"


def _violations() -> list[str]:
    out: list[str] = []
    for path in _py_files():
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "write_text":
                kw = _kwarg_names(node)
                missing = {"encoding", "newline"} - kw
                if missing:
                    out.append(f"{rel}:{node.lineno}: write_text without "
                               f"{'/'.join(sorted(missing))}=")
            elif isinstance(fn, ast.Attribute) and fn.attr == "read_text":
                if "encoding" not in _kwarg_names(node):
                    out.append(f"{rel}:{node.lineno}: read_text without encoding=")
            elif isinstance(fn, ast.Name) and fn.id == "open":
                mode = _open_mode(node)
                if "b" in mode:
                    continue
                kw = _kwarg_names(node)
                need = {"encoding"}
                if any(c in mode for c in "wax+"):
                    need.add("newline")
                missing = need - kw
                if missing:
                    out.append(f"{rel}:{node.lineno}: text-mode open({mode!r}) "
                               f"without {'/'.join(sorted(missing))}=")
    return out


def test_text_io_pins_encoding_and_newline() -> None:
    problems = _violations()
    assert not problems, (
        "platform-dependent text I/O (Windows: cp1252 locale encoding + "
        "\\n -> \\r\\n translation); pin encoding='utf-8' and, on writes, "
        "newline='\\n':\n  " + "\n  ".join(problems))


# ------------------------------------------------ vendored patch sentinel ----
# The AST gate above skips ``_vendor/`` — vendored code is not ours to
# restyle. But three vendored writers DO carry the fix by hand (PROVENANCE.md
# "Local modifications" 5): they emit .kicad_sym / .kicad_mod / .step wrappers
# whose bytes matter, and their text contains Ω/µ. Re-vendoring from upstream
# silently reverts them — the exact 0.8.0 + 0.10.0 failure class, only this
# time invisible to the gate. So pin the known-patched call sites by name:
# this is a sentinel over three lines, NOT a general rule for _vendor.

VENDOR = ROOT / "src" / "akcli" / "_vendor" / "jlc2kicadlib"

PATCHED_WRITES = {
    # module (relative to _vendor/jlc2kicadlib) -> what it writes
    "footprint/model3d.py": ".step/.wrl 3D-model wrapper",
    "symbol/symbol.py": ".kicad_sym symbol library",
    "_kmt.py": ".kicad_mod footprint",
}


def _text_write_opens(path: Path) -> list[tuple[int, set[str]]]:
    """(lineno, kwargs) for every text-mode writing builtin ``open`` call."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "open":
            mode = _open_mode(node)
            if "b" not in mode and any(c in mode for c in "wax+"):
                out.append((node.lineno, _kwarg_names(node)))
    return out


def test_vendored_writers_keep_the_windows_patch() -> None:
    problems: list[str] = []
    for rel, what in sorted(PATCHED_WRITES.items()):
        path = VENDOR / rel
        if not path.is_file():
            problems.append(f"{rel}: vendored file is gone (writes {what})")
            continue
        writes = _text_write_opens(path)
        if not writes:
            problems.append(f"{rel}: no text-mode write open() left — the "
                            f"{what} writer moved; re-pin this sentinel")
            continue
        for lineno, kw in writes:
            missing = {"encoding", "newline"} - kw
            if missing:
                problems.append(
                    f"{rel}:{lineno}: text write without "
                    f"{'/'.join(sorted(missing))}= ({what})")
    assert not problems, (
        "a hand-applied Windows text-I/O patch was lost from the vendored "
        "JLC2KiCadLib (re-vendoring reverts it silently). Re-apply "
        "encoding='utf-8', newline='\\n' and see "
        "src/akcli/_vendor/jlc2kicadlib/PROVENANCE.md, 'Local modifications' "
        "item 5:\n  " + "\n  ".join(problems))
