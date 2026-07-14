"""LCSC → KiCad library conversion via the vendored JLC2KiCadLib core.

Runs **in-process** (no external binary, no pip dependency): the vendored MIT
converter (`.._vendor.jlc2kicadlib`, see its PROVENANCE.md) fetches the part's
EasyEDA CAD data over HTTP and writes a KiCad symbol library, footprint, and
optional 3D model. The GPLv3 ``KicadModTree`` upstream dependency is replaced by
the clean-room ``_kmt`` writer; ``requests`` by the stdlib ``_http`` shim.

The converter is networked (the only networked akcli feature besides
``jlc search``/``show``); a converted library is a CLAIM, not a fact — callers
must surface the verify-against-datasheet caveat.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .._vendor.jlc2kicadlib import _http, helper

EXE = "jlc2kicadlib (vendored)"  # display name in messages

# EasyEDA endpoint that maps an LCSC part number to its CAD document uuids.
_SVGS_URL = "https://easyeda.com/api/products/{lcsc}/svgs"


@dataclass
class ConvertResult:
    """Outcome of one conversion. ``error_code`` None means artifacts exist."""

    lcsc: str
    out_dir: str
    artifacts: list[str] = field(default_factory=list)
    error_code: str | None = None
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "lcsc": self.lcsc,
            "out_dir": self.out_dir,
            "artifacts": list(self.artifacts),
            "error_code": self.error_code,
            "message": self.message,
        }


def convert(
    lcsc: str,
    out_dir: str,
    *,
    with_3d: bool = False,
    lib_name: str = "akcli",
    footprint_lib: str = "footprint",
    model_path: str | None = None,
    force: bool = False,
) -> ConvertResult:
    """Convert LCSC part ``lcsc`` into a KiCad library under ``out_dir``.

    Layout (JLC2KiCadLib conventions): ``<out>/symbol/<lib_name>.kicad_sym``,
    ``<out>/<footprint_lib>/<name>.kicad_mod`` and, with ``with_3d``,
    ``<out>/<footprint_lib>/packages3d/<name>.step``.

    ``footprint_lib`` doubles as the **fp-lib-table nickname** written into the
    symbol's Footprint field (``<footprint_lib>:<name>``) — pass the nickname
    the target project actually registers, or KiCad will not find the package.

    ``model_path`` controls how the 3D model is referenced from the footprint:
    ``None``/``"relative"`` keeps the converter's bare relative path (portable
    but resolvable only next to the library), ``"absolute"`` rewrites it to the
    on-disk absolute path (always resolvable on this machine, not portable),
    and a ``${VAR}``-style prefix writes ``${VAR}/packages3d/<name>.step``.
    """
    import json

    res = ConvertResult(lcsc=lcsc, out_dir=out_dir)

    base_var = ""
    if model_path and model_path not in ("relative", "absolute"):
        if not model_path.startswith("$"):
            res.error_code = "CONVERT_FAILED"
            res.message = (f"bad --3d-path {model_path!r}: expected 'relative', "
                           "'absolute', or a '${VAR}'-style prefix")
            return res
        base_var = model_path

    resp = _http.get(_SVGS_URL.format(lcsc=lcsc),
                     headers={"User-Agent": helper.get_user_agent()})
    if resp.status_code != _http.codes.ok:
        res.error_code = "NETWORK"
        res.message = f"EasyEDA lookup failed (HTTP {resp.status_code})"
        return res
    try:
        data = json.loads(resp.content.decode("utf-8"))
    except ValueError:
        res.error_code = "NETWORK"
        res.message = "EasyEDA lookup returned malformed JSON"
        return res
    if not data.get("success") or not data.get("result"):
        res.error_code = "CONVERT_PART_NOT_FOUND"
        res.message = f"no EasyEDA CAD data for {lcsc!r} (typo, or part has no model)"
        return res

    footprint_uuid = data["result"][-1]["component_uuid"]
    symbol_uuids = [i["component_uuid"] for i in data["result"][:-1]]

    from .._vendor.jlc2kicadlib.footprint.footprint import create_footprint
    from .._vendor.jlc2kicadlib.symbol.symbol import create_symbol

    try:
        footprint_name, datasheet_link = create_footprint(
            footprint_component_uuid=footprint_uuid,
            component_id=lcsc,
            footprint_lib=footprint_lib,
            output_dir=out_dir,
            model_base_variable=base_var,
            model_dir="packages3d",
            skip_existing=not force,
            models=["STEP"] if with_3d else [],
        )
        if symbol_uuids:
            create_symbol(
                symbol_component_uuid=symbol_uuids,
                footprint_name=footprint_name,
                datasheet_link=datasheet_link,
                library_name=lib_name,
                symbol_path="symbol",
                output_dir=out_dir,
                component_id=lcsc,
                skip_existing=not force,
            )
    except Exception as exc:  # vendored code raises on malformed CAD payloads
        logging.debug("jlc2kicad conversion error", exc_info=True)
        res.error_code = "CONVERT_FAILED"
        res.message = f"conversion failed: {exc}"
        return res

    root = Path(out_dir)
    if root.exists():
        res.artifacts = sorted(
            str(p) for p in root.rglob("*")
            if p.is_file() and p.suffix in (".kicad_sym", ".kicad_mod", ".step", ".wrl")
        )
    if not res.artifacts:
        res.error_code = "CONVERT_NO_ARTIFACTS"
        res.message = "conversion produced no library artifacts"
        return res

    if model_path == "absolute":
        _absolutize_models(res.artifacts, root, footprint_lib)
    return res


def _absolutize_models(artifacts: list[str], root: Path, footprint_lib: str) -> None:
    """Rewrite bare-relative ``(model "packages3d/...")`` paths to absolute.

    Bare relative 3D paths only resolve when KiCad's CWD happens to be the
    library directory (footprint viewer/chooser: never). Absolute paths always
    resolve on this machine at the cost of portability — the caller surfaces
    that trade-off.
    """
    base = (root.resolve() / footprint_lib / "packages3d").as_posix()
    for art in artifacts:
        if not art.endswith(".kicad_mod"):
            continue
        p = Path(art)
        text = p.read_text(encoding="utf-8")
        patched = text.replace('(model "packages3d/', f'(model "{base}/')
        if patched != text:
            p.write_text(patched, encoding="utf-8")
