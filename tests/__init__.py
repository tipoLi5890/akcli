"""Test package for akcli.

Ensures the in-repo ``src/`` layout is importable when the package has not been
installed (editable). Harmless when it has been installed via ``pip install -e .``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
