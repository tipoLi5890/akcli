"""Node identity: the ONE home of uuid seed formats (SPEC §3.5).

Every node akcli writes gets a deterministic UUIDv5 derived from the sheet's
root uuid plus a CONTENT seed — never an op index — so replaying or editing
an op-list re-identifies the same nodes instead of duplicating the design.
The seed grammar used to be scattered across ten call sites in the writer
monolith; each format now has exactly one builder here.

RED LINE — the seed strings and the ``:0`` suffix are the identity of every
node in every existing ``.kicad_sch``.  Changing ANY seed format
re-identifies those nodes, so the next re-apply duplicates the whole
document.  ``tests/test_instances.py`` pins actual uuid values;
``docs/pin-identity.md`` records the audited migration boundaries (a future
per-pin identity changes the *derived-output* encoding, not these seeds).

The trailing ``:0`` is the fossil of a removed ``op_index`` parameter (the
index was pinned to 0 everywhere once content seeding landed, because an
index in the seed re-identified — and so duplicated — every node downstream
of an op-list edit).  It stays in the seed string forever; only the
PARAMETER was deleted.
"""

from __future__ import annotations

import uuid as _uuid

__all__ = [
    "node_uuid",
    "seed_symbol", "seed_symbol_pin", "seed_wire", "seed_junction",
    "seed_no_connect", "seed_bus_entry", "seed_label", "seed_sheet",
    "seed_sheet_file", "seed_sheet_pin", "seed_annotation_key",
    "seed_annotation_coords",
]

_SEED_SUFFIX = ":0"     # historical; removing it re-identifies every node


def node_uuid(sheet_uuid: str | None, seed: str) -> str:
    """UUIDv5 of ``seed`` in the sheet's namespace (DNS-hash fallback)."""
    try:
        ns = _uuid.UUID(str(sheet_uuid))
    except (ValueError, AttributeError):
        ns = _uuid.uuid5(_uuid.NAMESPACE_DNS, str(sheet_uuid))
    return str(_uuid.uuid5(ns, seed + _SEED_SUFFIX))


def seed_symbol(designator: str, unit: int = 1) -> str:
    """``R3`` / ``R3#u2`` — the SCALAR designator (per-instance refs on a
    reused sheet never re-identify the node)."""
    return designator if unit == 1 else f"{designator}#u{unit}"


def seed_symbol_pin(designator: str, unit: int, pin_number: str,
                    dup_index: int = 0) -> str:
    """``R3.pin1`` (+``#k`` when a pin NUMBER legitimately repeats within one
    symbol — shared pads on multi-unit parts)."""
    base = (f"{designator}.pin{pin_number}" if unit == 1
            else f"{designator}#u{unit}.pin{pin_number}")
    return base if dup_index == 0 else f"{base}#{dup_index + 1}"


def seed_wire(tag: str, a: tuple[int, int], b: tuple[int, int]) -> str:
    return f"{tag}:{a[0]}:{a[1]}:{b[0]}:{b[1]}"


def seed_junction(p: tuple[int, int]) -> str:
    return f"junction:{p[0]}:{p[1]}"


def seed_no_connect(anchor_seed: str) -> str:
    return f"no_connect:{anchor_seed}"


def seed_bus_entry(p: tuple[int, int]) -> str:
    return f"bus_entry:{p[0]}:{p[1]}"


def seed_label(tag: str, name: str, anchor_seed: str) -> str:
    """``label:NAME:<anchor>`` — the written anchor string itself is the seed
    (two labels on one coordinate stay distinct nodes)."""
    return f"{tag}:{name}:{anchor_seed}"


def seed_sheet(file_name: str, name: str) -> str:
    return f"sheet:{file_name}:{name}"


def seed_sheet_file(file_name: str) -> str:
    """``sheetfile:<child.kicad_sch>`` — the ROOT uuid of a child sheet file
    that ``add_sheet {"create": true}`` writes for the first time.

    The only seed here that identifies a FILE rather than a node inside one,
    and the same red line applies: it is derived from the PARENT document's
    root uuid plus the Sheetfile string exactly as the op wrote it, never from
    randomness or an op index. That is what makes ``draw`` (plan) and
    ``draw --apply`` agree on the child's identity, and what keeps a
    re-created child re-identifying every node inside it.
    """
    return f"sheetfile:{file_name}"


def seed_sheet_pin(file_name: str, name: str, pin_name: str) -> str:
    return f"sheet:{file_name}:{name}.pin.{pin_name}"


def seed_annotation_key(tag: str, key: str) -> str:
    """Stable, coordinate-free identity for keyed annotations (frames)."""
    return f"{tag}:key:{key}"


def seed_annotation_coords(tag: str, coord_seed: str) -> str:
    return f"{tag}:{coord_seed}"
