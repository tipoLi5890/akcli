"""``.PcbDoc`` -> :class:`model.Pcb` (ASCII sections only, v1) (SPEC §3.2).

An Altium PCB document is an OLE2/CFBF container whose payload is split into many
named storages -- some **ASCII** (``|KEY=VALUE|`` records: ``Nets6``,
``Components6``, ``Classes6``, ``Rules6``, ...) and some **binary** (packed structs:
``Pads6``, ``Vias6``, ``Tracks6``, ``Arcs6``, ``Fills6``, ``Regions6``).

v1 scope (LOCKED): parse **only** the four ASCII sections needed for net / part /
class / rule analysis. The binary sections require per-section struct decoders and
are **explicitly deferred** -- :func:`parse_ascii_section` *refuses loudly*
(``ALTIUM_UNSUPPORTED`` -> exit 5, unsupported) if asked to ASCII-parse one, rather
than feeding binary bytes through the text tokenizer and emitting garbage.
"""

from __future__ import annotations

import os

from ..errors import fail
from ..model import Footprint, Pcb
from . import _cfbf
from .altium_records import fields, parse_records

# ASCII (text-record) sections this reader understands.
ASCII_SECTIONS: frozenset[str] = frozenset(
    {"Nets6", "Components6", "Classes6", "Rules6"}
)

# Binary sections (packed structs) -- decoding deferred; refuse to ASCII-parse.
BINARY_SECTIONS: frozenset[str] = frozenset(
    {"Pads6", "Vias6", "Tracks6", "Arcs6", "Fills6", "Regions6", "Texts6", "Polygons6"}
)


def _section_buf(streams: dict[str, bytes], section: str) -> bytes | None:
    """Return a section's payload bytes (``"<section>/Data"`` or bare ``"<section>"``)."""
    if f"{section}/Data" in streams:
        return streams[f"{section}/Data"]
    if section in streams:
        return streams[section]
    return None


def _section_records(buf: bytes) -> list[dict]:
    """Frame an ASCII PCB section payload into ``|KEY=VALUE|`` field-dicts.

    PCB ASCII sections do not carry a schematic ``HEADER``; ``drop_header=False``
    keeps every record. Some sections store all rows as one ``\\x00``-delimited
    blob rather than length-prefixed frames, so if length-framing yields nothing
    usable we fall back to splitting on NUL.
    """
    recs = parse_records(buf, drop_header=False)
    if any(r for r in recs):  # at least one non-empty field-dict
        return [r for r in recs if r]
    # fallback: NUL-delimited |KEY=VALUE| rows
    out: list[dict] = []
    for chunk in buf.split(b"\x00"):
        if b"|" not in chunk:
            continue
        d = fields(chunk.decode("latin-1", "replace"))
        if d:
            out.append(d)
    return out


def parse_ascii_section(streams: dict[str, bytes], section: str) -> list[dict]:
    """Parse one ASCII section into records; refuse binary sections loudly."""
    if section in BINARY_SECTIONS:
        fail(
            "ALTIUM_UNSUPPORTED",
            f"section {section!r} is a binary PCB section (decoding deferred)",
        )
    buf = _section_buf(streams, section)
    if buf is None:
        return []
    return _section_records(buf)


def _nets(records: list[dict]) -> list[str]:
    out: list[str] = []
    for r in records:
        name = r.get("NAME") or r.get("Name")
        if name:
            out.append(name)
    return out


def _footprints(records: list[dict]) -> list[Footprint]:
    out: list[Footprint] = []
    for r in records:
        des = r.get("SOURCEDESIGNATOR") or r.get("NAME") or r.get("Name")
        if not des:
            continue
        rot = r.get("ROTATION") or r.get("Rotation")
        try:
            rotation = float(rot) if rot is not None else 0.0
        except ValueError:
            rotation = 0.0
        out.append(
            Footprint(
                designator=des,
                footprint_name=r.get("PATTERN") or r.get("FOOTPRINT") or r.get("Pattern"),
                layer=r.get("LAYER") or r.get("Layer"),
                rotation=rotation,
                value=r.get("COMMENT") or r.get("Comment"),
            )
        )
    return out


def read(path: os.PathLike | str | bytes | bytearray) -> Pcb:
    """Read a ``.PcbDoc`` into a normalized :class:`model.Pcb`.

    ASCII sections (``Nets6`` / ``Components6`` / ``Classes6`` / ``Rules6``)
    provide nets/footprints/classes/rules; the binary copper sections
    (``Tracks6`` / ``Vias6`` / ``Arcs6`` / ``Pads6``) are decoded by
    :mod:`.altium_pcb_bin` into ``Pcb.tracks/vias/arcs/pads`` (Altium frame,
    mils, +Y up; net indices resolved to names). Other binary sections
    (fills/regions/texts/polygons) remain out of scope and untouched;
    :func:`parse_ascii_section` still refuses them loudly.
    """
    from . import altium_pcb_bin as _bin

    streams = _cfbf.read_streams_qualified(path)

    nets = _nets(parse_ascii_section(streams, "Nets6"))
    footprints = _footprints(parse_ascii_section(streams, "Components6"))
    classes = parse_ascii_section(streams, "Classes6")
    rules = parse_ascii_section(streams, "Rules6")

    def _bin_section(section: str, parser):
        buf = _section_buf(streams, section)
        return parser(buf, nets) if buf else []

    src = path if isinstance(path, (str, os.PathLike)) else "<bytes>"
    return Pcb(
        source_path=str(src),
        source_format="altium",
        nets=nets,
        footprints=footprints,
        classes=classes,
        rules=rules,
        tracks=_bin_section("Tracks6", _bin.parse_tracks),
        vias=_bin_section("Vias6", _bin.parse_vias),
        arcs=_bin_section("Arcs6", _bin.parse_arcs),
        pads=_bin_section("Pads6", _bin.parse_pads),
    )


__all__ = ["read", "parse_ascii_section", "ASCII_SECTIONS", "BINARY_SECTIONS"]
