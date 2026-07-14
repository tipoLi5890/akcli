"""akcli — read Altium .SchDoc and KiCad .kicad_sch, run checks, draw KiCad.

Zero-runtime-dependency Python package (stdlib only). This top-level module exposes
the package version and the analysis/protocol version constants used across the CLI.

Name cascade (LOCKED): PyPI dist = ``akcli``; import package =
``akcli``; CLI = ``akcli``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version


def _resolve_version() -> str:
    """Return the installed dist version; fall back to ``pyproject.toml`` when the
    package is run from a source checkout (not installed), so ``akcli --version`` is
    correct in both cases. Last-resort ``"0.0.0"`` only if neither source is readable.
    """
    try:
        return _pkg_version("akcli")
    except PackageNotFoundError:
        try:
            import tomllib
            from pathlib import Path

            pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
            with pyproject.open("rb") as fh:
                return tomllib.load(fh)["project"]["version"]
        except Exception:
            return "0.0.0"


__version__ = _resolve_version()

__all__ = ["__version__"]
