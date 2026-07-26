"""`render_art` must survive the optional sub-nodes real symbol libraries use.

Every ``(offset)``/``(radius)``/``(length)``/``(name)``/``(number)``/``(type)``
inside a KiCad symbol body is OPTIONAL. `SymbolArt` used to hand `SNode.find`
results straight to `_atom_value` (which dereferences `.children`), so the very
common ``(pin_names hide)`` — no ``(offset)`` child — turned `akcli render`
into an `AttributeError` / exit 3 INTERNAL: it hit 13436 of the 22776 symbols
in the standard KiCad 10 libraries, and the project's own
`tests/fixtures/corpus/groups_board.kicad_sch` (jlc-imported symbols have the
same shape).
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest

from akcli.cli import main
from akcli.errors import EXIT
from akcli.model import Component
from akcli.readers import kicad_lib
from akcli.render_art import SymbolArt

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "corpus"

# Every optional node omitted at once: `(pin_names hide)` with no `(offset)`,
# a `(fill)` with no `(type)`, a `(circle)` with no `(radius)`, and pins with
# no `(length)` / `(name)` / `(number)`.
_SPARSE_LIB = """\
(kicad_symbol_lib
  (version 20231120)
  (generator "test")
  (symbol "Sparse"
    (pin_names hide)
    (in_bom yes)
    (on_board yes)
    (property "Reference" "U" (at 0 0 0))
    (property "Value" "Sparse" (at 0 0 0))
    (symbol "Sparse_0_1"
      (rectangle (start -2.54 2.54) (end 2.54 -2.54) (stroke (width 0)) (fill))
      (circle (center 0 0) (stroke (width 0)) (fill (type)))
      (polyline (pts (xy -2.54 0) (xy 2.54 0)) (stroke (width 0)) (fill none))
    )
    (symbol "Sparse_1_1"
      (pin passive line (at -5.08 0 0))
      (pin passive line (at 5.08 0 180) (length 2.54))
    )
  )
)
"""


def _sparse_art(tmp_path: Path) -> tuple[SymbolArt, Component]:
    path = tmp_path / "sparse.kicad_sym"
    path.write_text(_SPARSE_LIB, encoding="utf-8", newline="\n")
    art = SymbolArt(kicad_lib.read(str(path)))
    comp = Component(designator="U1", library_ref="Sparse", x_mil=1000,
                     y_mil=1000)
    return art, comp


def _fmt(v: float) -> str:
    return f"{v:.2f}"


def test_symbol_with_no_optional_nodes_emits_artwork(tmp_path: Path):
    art, comp = _sparse_art(tmp_path)
    frags = art.emit(comp, _fmt, _fmt, _fmt)
    assert frags, "a body with only optional nodes omitted must still draw"
    joined = "".join(frags)
    assert "<rect" in joined and "<circle" in joined and "<polyline" in joined
    # `(fill)` / `(fill (type))` carry no "background" token: plain outline
    assert 'class="sym sym-fill"' not in joined
    # `(pin_names hide)` suppresses names; a pin with no `(number)` draws none
    assert 'class="pinname"' not in joined
    assert 'class="pinnum"' not in joined
    # only the pin that has a `(length)` gets a stub
    assert joined.count('class="pinstub"') == 1


def test_sparse_symbol_world_bbox_is_computable(tmp_path: Path):
    art, comp = _sparse_art(tmp_path)
    assert art.world_bbox(comp) is not None


@pytest.mark.parametrize("name", sorted(p.name for p in CORPUS.glob("*.kicad_sch")))
def test_corpus_sheets_render(name: str, tmp_path: Path, capsys):
    """Regression: `groups_board` (and the rest of the corpus) crashed here.

    Rendered from a tmp copy — `akcli render` without `-o` writes the SVG
    beside its input, and fixtures must stay pristine.
    """
    src = CORPUS / name
    target = tmp_path / name
    target.write_bytes(src.read_bytes())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["render", str(target), "-o", "-"]) == EXIT["OK"]
    svg = buf.getvalue()
    assert svg.startswith("<?xml") or svg.lstrip().startswith("<svg")
    assert 'class="sym"' in svg           # faithful artwork, not the fallback box
    capsys.readouterr()
