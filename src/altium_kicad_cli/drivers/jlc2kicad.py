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
    force: bool = False,
) -> ConvertResult:
    """Convert LCSC part ``lcsc`` into a KiCad library under ``out_dir``.

    Layout (JLC2KiCadLib conventions): ``<out>/symbol/<lib_name>.kicad_sym``,
    ``<out>/footprint/<name>.kicad_mod`` and, with ``with_3d``,
    ``<out>/footprint/packages3d/<name>.step``.
    """
    import json

    res = ConvertResult(lcsc=lcsc, out_dir=out_dir)

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
            footprint_lib="footprint",
            output_dir=out_dir,
            model_base_variable="",
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
