"""Op-list executor → surgical ``.kicad_sch`` edits (SPEC §3.5).

This is the **executor**: it consumes a validated op-list (SPEC §2) and applies
its ops to a ``.kicad_sch`` document by *surgically* synthesizing the handful of
S-expression subtrees each op needs — never re-pretty-printing the rest of the
file (the byte-identity GATE in :mod:`..sexpr.parser` / :mod:`..sexpr.writer`
keeps untouched nodes verbatim).

Per-op behaviour (SPEC §2.2):

* ``place_component`` — :func:`lib_cache.ensure_cached` copies the symbol (any
  ``(extends)`` chain flattened, KiCad-save style) into ``(lib_symbols ...)``,
  then a placed ``(symbol ...)``
  instance is emitted with **per-pin** ``(pin "N" (uuid ...))`` nodes and a
  reference written into BOTH the ``Reference`` property and the ``(instances)``
  block (via :mod:`.instances`).
* ``set_component_transform`` — rotate/mirror an existing placed component.
* ``set_component_parameters`` — set reference/value/footprint/custom params.
* ``add_wire`` / ``add_bus`` — emit one orthogonal segment per consecutive vertex
  pair; a ``"REF.PIN"`` endpoint is **snapped to the pin's world coordinate** via
  :func:`geometry.pin_world`, a raw ``[x,y]`` mil point is snapped to grid.
* ``add_junction`` / ``add_no_connect`` / ``add_net_label`` / ``add_text`` /
  ``add_bus_entry`` — single-node primitives.
* ``add_sheet`` — a hierarchical ``(sheet ...)`` node (Sheetname/Sheetfile,
  stroke/fill, deterministic uuid, sheet pins at computed edge coordinates, the
  ``(instances)`` page block). The referenced child ``.kicad_sch`` is authored
  separately; wires attach to a sheet pin by its ``at`` + ``offset_mil``
  coordinate (a label anchor, so the endpoint does not dangle).
* ``place_power_port`` (+ sugar ``place_gnd`` / ``place_vcc``) — a power symbol with
  an auto-allocated ``#PWR0<n>`` reference.
* ``add_net_label`` / ``place_power_port`` ``at`` also accepts
  ``"mid(REF.PIN,REF.PIN)"`` — the midpoint of two axis-aligned pins, snapped to
  the 50-mil grid along the wire axis (labels auto-orient along that axis).
* ``delete_component`` with ``"cascade": true`` also removes wires ending on a
  deleted pin plus labels/no_connects/junctions anchored there;
  ``delete_object`` alternatively takes a ``match`` selector (exactly-one).
* ``rename_net`` — rewrite matching label texts + power-port net Values.

**Atomic write with backup + verify (SPEC §3.5, risk #13).** Nothing is written in
``--dry-run`` (the default). On ``apply=True`` the sequence is: snapshot the
original (mtime + sha256 optimistic lock) → write a temp file in the SAME directory
→ ``fsync`` → **re-parse the temp and run** :func:`connectivity.verify` on it →
``os.replace`` ONLY when verify is error-free → otherwise the temp is unlinked and
the original is untouched. An op-list whose ``protocol_version`` major exceeds ours
is rejected up front with ``PROTOCOL_MISMATCH``.

All geometry is integer **nanometres** (SPEC §1.2); millimetre strings are produced
only at serialize time through :func:`units.nm_to_mm_str`.

**Module layout (0.21 batch R3b).** This module is now a thin facade: the
executor was split into one module per concern, and every name below is
re-exported here so callers and tests keep their historical import site.

* :mod:`..sexpr.build` — SNode construction helpers (``_q``, ``_at``,
  ``_append_top_idempotent``, ...)
* :mod:`.context` — ``ExecCtx``, the claim registries, document lookups
* :mod:`.anchors` — ``"REF.PIN"`` / ``mid()`` / ``[x,y]`` -> world nm
* :mod:`.properties` — property writes + Reference/Value autoplacement
* :mod:`.placement` — placed ``(symbol ...)`` construction
* :mod:`.registry` — the ``@op`` decorator and the ``HANDLERS`` table
* :mod:`.handlers` — one module per op family (components, wires, labels,
  graphics, sheets, delete)
* :mod:`.executor` — ``apply`` / ``run_ops`` / the write + verify sequence
"""

from __future__ import annotations

from ..sexpr.build import (  # noqa: F401 - re-export (historical import site)
    _append_top,
    _append_top_idempotent,
    _at,
    _at_nm,
    _atom,
    _doc_child_indent,
    _effects,
    _fill,
    _label_justify,
    _list,
    _mil_pair,
    _mm,
    _pts,
    _q,
    _stroke,
    _unescape_atom,
    _uuid_node,
    _xy,
)
from . import registry as _registry
from .anchors import (  # noqa: F401 - re-export (historical import site)
    _GRID_NM,
    _anchored_pos_nm,
    _pin_at_point,
    _point_nm,
    _resolve_anchor_origin,
    _resolve_endpoint,
    _resolve_mid_anchor,
    _resolve_pin_inst,
    _resolve_pin_world,
    _snap_within_nm,
)
from .context import (  # noqa: F401 - re-export (historical import site)
    ExecCtx,
    _claim_uid,
    _ctx_new,
    _ctx_symdef,
    _inline_library,
    _inst_lib_id,
    _instance_component,
    _placed_symbols,
    _symbol_answers_to,
    _symbol_by_ref,
    _symbol_by_uuid,
    _symbol_reference,
    _symbol_unit,
    _symbols_by_ref,
    _symdef_from_body,
    root_uuid,
)
from .executor import (  # noqa: F401 - re-export (historical import site)
    OpResult,
    _coerce_source_list,
    _DEFAULT_BACKUP_DEPTH,
    _verify_temp,
    apply,
    backup_name,
    envelope_gate,
    gui_lock_path,
    run_ops,
)
from .handlers.components import (  # noqa: F401 - re-export
    _CARRY_LABEL_TAGS,
    _fnum_at,
    _instance_pin_points_nm,
    _translate_at,
    _translate_wire_pins,
)
from .handlers.delete import (  # noqa: F401 - re-export
    _delete_object_match,
    _delete_top_nodes,
    _node_uuid,
    _pt_on_seg_nm,
    _xy_points_nm,
)
from .handlers.graphics import (  # noqa: F401 - re-export
    _TITLE_BLOCK_SIMPLE,
    _annotation_uuid,
    _set_tb_field,
)
from .handlers.labels import (  # noqa: F401 - re-export
    _SUGAR_POWER,
    _anchor_seed,
    _emit_net_label,
    _existing_pwr_ref_for_op,
    _existing_pwr_sym_for_op,
    _is_pwr_flag,
)
from .handlers.sheets import (  # noqa: F401 - re-export
    _SHEET_PIN_ANGLE,
    _sheet_pin_pos,
)
from .handlers.wires import (  # noqa: F401 - re-export
    _BUS_ENTRY_NM,
    _emit_wire_segments,
    _pin_tip_set,
    _route_points,
)
from .placement import _make_symbol, _place_symbol  # noqa: F401 - re-export
from .properties import (  # noqa: F401 - re-export
    _PROP_MARGIN_NM,
    _VISIBLE_PROPERTIES,
    _autoplace_ref_value,
    _body_box_world,
    _free_anchor,
    _place_prop,
    _set_property,
)
from .registry import OpCall, _Note  # noqa: F401 - re-export

__all__ = ["OpResult", "apply", "backup_name"]

# The ONE handler table (``registry.HANDLERS``) under the name 25 test modules
# and the op-census gate have always imported. Same dict object, so a test that
# monkeypatches one entry still changes what the executor dispatches.
_HANDLERS = _registry.HANDLERS
