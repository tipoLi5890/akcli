"""Fab profiles: versioned manufacturing policy checked against a ``.kicad_pcb``.

A profile is TOML (stdlib ``tomllib``) capturing ONE vendor capability revision
with its sources — never a hardcoded global constant:

.. code-block:: toml

    id = "jlc-4l-1oz-free-vias-2026-07"
    vendor = "JLCPCB"

    [source]
    urls = ["https://jlcpcb.com/hk/help/article/pcb-via-covering"]
    retrieved_at = "2026-07-14"

    [stackup]
    layers = 4
    thickness_mm = 1.0

    [via]
    min_drill_mm = 0.30            # below this: paid small-via process
    min_pad_mm = 0.40
    preferred_annular_mm = 0.075
    max_tented_drill_mm = 0.40     # tented covering caps the drill
    forbid_via_in_pad = true
    forbid_blind_buried = true     # blind, buried AND micro

    [slot]                         # optional: oval pad drills
    min_plated_width_mm = 0.35
    min_non_plated_width_mm = 1.0  # (min_width_mm: either kind)

    [cost.warn_if]
    board_length_mm_gte = 600
    board_area_cm2_gt = 650
    drill_density_per_m2_gt = 150000
    trace_width_mm_lte = 0.0889

    [cost.error_if]                # optional: same keys, same finding code,
    board_length_mm_gte = 1200     # raised as an ERROR instead

    [rules]                        # optional: floors for the project's own
    min_clearance_mm = 0.1         # DRC constraints (.kicad_pro)

    [[exception]]
    type = "thermal_via"
    component = "U1"               # vias inside U1's pads are approved
    owner = "hw-lead"
    reason = "QFN exposed-pad thermal vias"
    expires = "2027-01-01"

Severity policy (mirrors the enhancement plan): direct profile violations
(paid via geometry, oversize tented via, via-in-pad, forbidden via types, a
slot under the minimum, stackup drift) are ERRORs; cost-threshold crossings
are WARNINGs with the actual value vs the threshold — ERRORs where the profile
repeats the key under ``[cost.error_if]``; boundary-exact geometry passes with
a NOTE ("minimum margin"); an approved exception passes as an explicit NOTE,
never silently. Missing evidence (no board outline) is itself a WARNING — the
check never pretends it measured something it could not.

**What is measured, and what is only declared.** Every finding stays advisory
policy; akcli never writes PCB content. Three inputs decide how far a rule
can be trusted, and each is reported rather than assumed:

* *Outline.* Board area and drill density use the true ``Edge.Cuts`` area the
  reader assembles (:mod:`akcli.geometry.outline`: arcs exact, cutouts
  subtracted). When the shapes do not close, the bounding box stands in and
  the finding says so — never a silently wrong area.
* *Tenting.* ``max_tented_drill_mm`` binds a via only where it IS tented:
  per the board file, or for every via once the order manifest declares
  ``via_covering = "tented"``. When neither says, ONE ``FAB_TENTING_UNKNOWN``
  NOTE replaces an error per via.
* *Project rules.* The board's own DRC minimums (``.kicad_pro``) are compared
  with the profile's; a project that allows less is one
  ``FAB_RULES_LOOSER_THAN_PROFILE`` WARNING naming each rule.

Not computed, by design: V-cut/panel features (not in the board file) and the
ENIG exposed-copper area (needs polygon booleans) stay
``ORDER_REVIEW_REQUIRED``.

The **order manifest** (:func:`check_order`) is the user's declared purchase
intent — delivery format, surface finish, via covering… — validated for
completeness; it is never guessed from the PCB.

**Bundled profiles.** A small set of profiles ships inside the wheel
(:mod:`akcli.fab_profiles`, a byte-for-byte mirror of ``examples/fab``), so
``--profile`` takes either a file or a bundled id — :func:`resolve_profile`.
A bundled profile is still one sourced, dated revision, not a builtin
constant: it ages inside an installed wheel while the vendor page moves on,
which is why :func:`check` raises ``FAB_PROFILE_STALE`` (a NOTE) once its
``retrieved_at`` is more than twelve months old. A profile FILE never gets
that note — its revision discipline is the owning repository's.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import math
import os
from pathlib import Path
from typing import NamedTuple

from . import fab_profiles
from .errors import fail
from .model import Pcb
from .readers import kicad_pro
from .report import Finding, Severity, anchor

__all__ = ["ProfileOrigin", "ProjectRules", "load_profile", "bundled_profiles",
           "resolve_profile", "retrieved_at", "find_project",
           "read_project_rules", "check", "evaluate", "load_order",
           "check_order", "explain"]

_EPS = 1e-6

# A bundled profile older than this is flagged (NOTE) by :func:`check`.
_STALE_AFTER_YEARS = 1


class ProfileOrigin(NamedTuple):
    """Where a resolved profile came from — what a release manifest records.

    ``sha256`` is over the exact bytes that were parsed (one read, so the
    hash can never describe a different file state than the rules that ran).
    """

    bundled: bool
    path: str | None      # the file as given; ``None`` for a bundled profile
    name: str             # file name (the packaged one when bundled)
    sha256: str


# One key set for BOTH cost tables: a threshold under [cost.warn_if] trips a
# WARNING, the same key under [cost.error_if] trips the same code at ERROR.
_COST_KEYS = ("board_length_mm_gte", "board_area_cm2_gt",
              "drill_density_per_m2_gt", "trace_width_mm_lte")
_SLOT_KEYS = ("min_width_mm", "min_plated_width_mm", "min_non_plated_width_mm")
# Profile [rules] key -> the KiCad ``board.design_settings.rules`` entry (in
# the project's .kicad_pro) it is a floor for.
_RULE_KEYS = {
    "min_clearance_mm": "min_clearance",
    "min_track_width_mm": "min_track_width",
    "min_via_diameter_mm": "min_via_diameter",
    "min_through_hole_mm": "min_through_hole_diameter",
    "min_via_annular_mm": "min_via_annular_width",
    "min_hole_to_hole_mm": "min_hole_to_hole",
    "min_hole_clearance_mm": "min_hole_clearance",
    "min_copper_edge_clearance_mm": "min_copper_edge_clearance",
}
# The two minimums every profile already states under [via]: they stand in
# for the [rules] key of the same meaning when that key is absent.
_RULE_FALLBACKS = {
    "min_via_diameter_mm": ("via", "min_pad_mm"),
    "min_through_hole_mm": ("via", "min_drill_mm"),
}


def _number_table(doc: dict, path: tuple[str, ...], allowed: tuple[str, ...],
                  label: str) -> None:
    """Refuse a mistyped optional table: every key known, every value a
    finite, non-negative number. An absent table is fine.

    A misspelt threshold would otherwise be the quietest failure a policy
    file can have — the rule simply never runs.
    """
    node: object = doc
    for part in path:
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return
    name = ".".join(path)
    if not isinstance(node, dict):
        fail("BAD_CONFIG", f"fab profile {label}: [{name}] must be a table")
    unknown = sorted(set(node) - set(allowed))
    if unknown:
        fail("BAD_CONFIG",
             f"fab profile {label}: unknown key(s) in [{name}]: "
             f"{', '.join(unknown)} — allowed: {', '.join(allowed)}")
    for key, value in node.items():
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            fail("BAD_CONFIG",
                 f"fab profile {label}: [{name}] {key} must be a "
                 f"non-negative number, got {value!r}")


def _parse_profile(data: bytes, label: str) -> dict:
    """Decode + structurally validate fab-profile TOML bytes."""
    import tomllib

    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        fail("BAD_CONFIG", f"fab profile {label}: {exc}")
    if not doc.get("id"):
        fail("BAD_CONFIG", f"fab profile {label}: missing 'id'")
    if not (doc.get("source") or {}).get("urls"):
        fail("BAD_CONFIG",
             f"fab profile {label}: missing [source] urls — a profile must "
             "carry its evidence (vendor page + retrieved_at)")
    for i, exc_rule in enumerate(doc.get("exception") or []):
        if not exc_rule.get("owner") or not exc_rule.get("reason"):
            fail("BAD_CONFIG",
                 f"fab profile {label}: exception #{i} needs owner AND reason")
    _number_table(doc, ("cost", "error_if"), _COST_KEYS, label)
    _number_table(doc, ("slot",), _SLOT_KEYS, label)
    _number_table(doc, ("rules",), tuple(_RULE_KEYS), label)
    return doc


def _load_profile_file(path: os.PathLike | str) -> tuple[dict, str]:
    """A profile file -> (validated doc, sha256 of its bytes)."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        fail("BAD_CONFIG", f"fab profile {path}: {exc}")
    return _parse_profile(data, os.fspath(path)), hashlib.sha256(data).hexdigest()


def load_profile(path: os.PathLike | str) -> dict:
    """Load + structurally validate a fab-profile TOML file."""
    return _load_profile_file(path)[0]


def bundled_profiles() -> list[tuple[dict, ProfileOrigin]]:
    """Every profile shipped in THIS install, in file-name order.

    Read from the packaged mirror (:mod:`akcli.fab_profiles`), validated by
    the same rules as a profile file — a bundled profile gets no exemption.
    """
    out: list[tuple[dict, ProfileOrigin]] = []
    for name in fab_profiles.names():
        data = fab_profiles.read_bytes(name)
        out.append((_parse_profile(data, f"<bundled>/{name}"),
                    ProfileOrigin(True, None, name,
                                  hashlib.sha256(data).hexdigest())))
    return out


def resolve_profile(arg: os.PathLike | str) -> tuple[dict, ProfileOrigin]:
    """Turn a ``--profile`` argument into ``(profile, origin)``.

    Precedence, in order:

    1. an existing FILE at that path — always wins, so a profile checked
       into the project is never shadowed by a bundled one of the same name;
    2. a bundled profile whose ``id`` — or packaged file stem — equals the
       argument exactly;
    3. otherwise ``BAD_CONFIG``, listing the bundled ids.
    """
    text = os.fspath(arg)
    path = Path(text)
    if path.is_file():
        doc, digest = _load_profile_file(path)
        return doc, ProfileOrigin(False, text, path.name, digest)
    bundled = bundled_profiles()
    for doc, origin in bundled:
        if text in (doc.get("id"), origin.name.removesuffix(".toml")):
            return doc, origin
    ids = ", ".join(str(doc.get("id")) for doc, _ in bundled)
    fail("BAD_CONFIG",
         f"fab profile {text!r}: no such file, and not a bundled profile id "
         f"— bundled: {ids or '(none in this install)'} "
         "(`akcli fab profiles` lists them)")


def retrieved_at(profile: dict) -> _dt.date | None:
    """The profile's ``[source] retrieved_at`` as a date, or ``None``.

    Accepts both spellings TOML allows — a quoted ISO string and a native
    date — and treats anything else (absent, mistyped) as unknown.
    """
    raw = (profile.get("source") or {}).get("retrieved_at")
    if isinstance(raw, _dt.datetime):
        return raw.date()
    if isinstance(raw, _dt.date):
        return raw
    try:
        return _dt.date.fromisoformat(str(raw))
    except ValueError:
        return None


def _today() -> _dt.date:
    """The clock :func:`check` reads when no ``today`` is injected."""
    return _dt.date.today()


def _stale_finding(profile: dict, today: _dt.date) -> Finding | None:
    """The one staleness NOTE a BUNDLED profile can raise, or ``None``."""
    pid = profile.get("id", "?")
    got = retrieved_at(profile)
    if got is None:
        return Finding(
            "FAB_PROFILE_STALE", Severity.NOTE,
            f"bundled profile {pid} carries no parseable [source] "
            "retrieved_at — its age cannot be checked; re-verify it against "
            "its source urls")
    # Calendar comparison, not a day count: "older than 12 months" must not
    # drift by a day across a leap year (and 29 Feb needs no special case).
    if (today.year, today.month, today.day) <= (
            got.year + _STALE_AFTER_YEARS, got.month, got.day):
        return None
    return Finding(
        "FAB_PROFILE_STALE", Severity.NOTE,
        f"bundled profile {pid} was retrieved {got.isoformat()} — more than "
        f"{12 * _STALE_AFTER_YEARS} months ago; vendor capability and pricing "
        "pages change, so re-verify it against its source urls (or use a "
        "newer akcli / your own profile file)")


def _exception_for(profile: dict, *, etype: str, component: str | None,
                   today: _dt.date) -> tuple[dict | None, bool]:
    """Find a matching approved exception; returns (rule, expired)."""
    for rule in profile.get("exception") or []:
        if rule.get("type") != etype:
            continue
        want = rule.get("component")
        if want and component and want != component:
            continue
        expires = rule.get("expires")
        if expires:
            try:
                if _dt.date.fromisoformat(str(expires)) < today:
                    return rule, True
            except ValueError:
                return rule, True
        return rule, False
    return None, False


def _point_in_pad(px: float, py: float, pad: dict) -> bool:
    """Is board point (px,py) inside the pad's (rotated) rectangle?"""
    ax, ay = pad["at"]
    rot = float(pad.get("rotation") or 0.0)
    dx, dy = px - ax, py - ay
    if rot:
        r = math.radians(rot)
        c, s = math.cos(r), math.sin(r)
        # inverse of the reader's CCW-on-screen rotation
        dx, dy = dx * c - dy * s, dx * s + dy * c
    sx, sy = pad["size"]
    return abs(dx) <= sx / 2 + _EPS and abs(dy) <= sy / 2 + _EPS


# --------------------------------------------------------------------------- #
# the project's own design rules (.kicad_pro)
# --------------------------------------------------------------------------- #
class ProjectRules(NamedTuple):
    """The board's own DRC constraints, as read for the rule-drift check.

    ``rules`` is ``None`` whenever there is nothing to compare — no project
    file (``path`` is ``None`` too) or one that could not be read (``error``
    says why). A readable project that states no rules yields ``{}``.
    """

    path: str | None      # the .kicad_pro consulted, posix-style
    rules: dict | None    # KiCad rule name -> mm
    error: str | None


def find_project(board_path: os.PathLike | str) -> Path | None:
    """The ``.kicad_pro`` a board belongs to, or ``None``.

    KiCad pairs the two by stem, so ``<board>.kicad_pro`` beside the board
    wins; failing that, the directory's project file as
    :func:`akcli.readers.kicad_pro.find` picks it.
    """
    board = Path(board_path)
    same_stem = board.with_suffix(".kicad_pro")
    if same_stem.is_file():
        return same_stem
    return kicad_pro.find(board.parent)


def read_project_rules(pro_path: os.PathLike | str | None) -> ProjectRules:
    """``board.design_settings.rules`` of a project file. Never raises: the
    reader underneath is degrade-only, and a project that cannot be read is
    reported through ``error`` for the caller to state or to refuse."""
    if pro_path is None:
        return ProjectRules(None, None, None)
    pro = kicad_pro.read(pro_path)
    shown = Path(pro_path).as_posix()
    if not pro.ok:
        return ProjectRules(shown, None, pro.error)
    return ProjectRules(shown, dict(pro.design_rules), None)


def _rule_comparison(profile: dict, rules: dict) -> list[dict]:
    """One row per project DRC rule the profile states a floor for.

    A rule the project file does not state compares as ``project_mm: None``
    and is never "looser": KiCad then applies a built-in default this tool
    does not guess.
    """
    table = profile.get("rules") or {}
    rows: list[dict] = []
    for key, kicad_rule in _RULE_KEYS.items():
        source, minimum = f"rules.{key}", table.get(key)
        if minimum is None and key in _RULE_FALLBACKS:
            section, name = _RULE_FALLBACKS[key]
            source = f"{section}.{name}"
            minimum = (profile.get(section) or {}).get(name)
        if minimum is None:
            continue
        have = rules.get(kicad_rule)
        rows.append({
            "rule": kicad_rule,
            "project_mm": have,
            "profile_key": source,
            "profile_min_mm": float(minimum),
            "looser": (have is not None
                       and float(have) < float(minimum) - _EPS),
        })
    return rows


# --------------------------------------------------------------------------- #
# what the board file establishes (facts the findings are derived from)
# --------------------------------------------------------------------------- #
def _tenting_state(via: dict) -> str:
    """``tented`` | ``untented`` | ``unknown`` | ``not_exposed`` — from the
    board file alone.

    Only a side the via actually reaches counts: a blind via from F.Cu has no
    back opening to tent, and a buried via has none at all (``not_exposed`` —
    the tenting cap cannot apply to it). Solder mask over the hole on EITHER
    reached side is a tent the vendor has to bridge, so one tented side is
    enough; ``untented`` needs every reached side known open.
    """
    layers = via.get("layers") or []
    sides = [side for side, copper in (("front", "F.Cu"), ("back", "B.Cu"))
             if not layers or copper in layers]
    if not sides:
        return "not_exposed"
    tent = via.get("tenting") or {}
    states = [tent.get(side) for side in sides]
    if any(state is True for state in states):
        return "tented"
    if all(state is False for state in states):
        return "untented"
    return "unknown"


def _outline_facts(board: dict) -> dict:
    """How the board's area was established: ``outline`` | ``bbox`` | ``none``.

    ``outline`` is the true area — Edge.Cuts chained into closed loops,
    cutouts subtracted. When the shapes do not close the area falls back to
    the bounding box and ``problems`` says why; it is never presented as the
    real thing.
    """
    bbox = board.get("outline_bbox")
    if bbox is None:
        return {"method": "none", "closed": False, "area_mm2": None,
                "bbox": None, "cutouts": 0, "problems": []}
    outline = board.get("outline") or {}
    if outline.get("closed") and outline.get("area_mm2") is not None:
        return {"method": "outline", "closed": True,
                "area_mm2": float(outline["area_mm2"]), "bbox": bbox,
                "cutouts": sum(1 for lp in outline.get("loops") or []
                               if lp.get("cutout")),
                "problems": []}
    (x0, y0), (x1, y1) = bbox
    return {"method": "bbox", "closed": False,
            "area_mm2": round((x1 - x0) * (y1 - y0), 6), "bbox": bbox,
            "cutouts": 0,
            "problems": (list(outline.get("problems") or [])
                         or ["the Edge.Cuts shapes do not form a closed loop"])}


def check(pcb: Pcb, profile: dict, order: dict | None = None, *,
          today: _dt.date | None = None, origin: ProfileOrigin | None = None,
          rules: dict | None = None) -> list[Finding]:
    """Check a deep-read KiCad board against a fab profile.

    ``order`` is the declared order manifest (:func:`load_order`): its
    ``via_covering`` decides which vias the tenting cap applies to when the
    board file does not say. ``rules`` is the project's own DRC minimums
    (:func:`read_project_rules`), compared against the profile's.
    ``origin`` (from :func:`resolve_profile`) is what tells a BUNDLED profile
    from a file: only the former can raise ``FAB_PROFILE_STALE``. ``today``
    is injectable so expiry/staleness are testable without a wall clock.

    :func:`evaluate` is the same check returning the facts as well.
    """
    return evaluate(pcb, profile, order, today=today, origin=origin,
                    rules=rules)[0]


def evaluate(pcb: Pcb, profile: dict, order: dict | None = None, *,
             today: _dt.date | None = None,
             origin: ProfileOrigin | None = None,
             rules: dict | None = None) -> tuple[list[Finding], dict]:
    """:func:`check`, plus the facts the findings were derived from.

    The second element is JSON-ready: ``outline`` (area, whether Edge.Cuts
    closed, and the ``method`` — ``outline`` / ``bbox`` / ``none``),
    ``via_types`` (count per type), ``tenting`` (what the board file says,
    per-via state counts, what the order declared and which vias the cap was
    applied to) and ``project_rules`` (one row per compared rule, or ``None``
    when no project rules were given).
    """
    findings: list[Finding] = []
    out = findings.append
    today = today or _today()
    pid = profile.get("id", "?")
    facts: dict = {"outline": None, "via_types": None, "tenting": None,
                   "project_rules": None}

    # About the profile, not the board — so it precedes every board rule,
    # including the unsupported-source early return below.
    if origin is not None and origin.bundled:
        stale = _stale_finding(profile, today)
        if stale is not None:
            out(stale)

    if pcb.source_format != "kicad":
        out(Finding("FAB_UNSUPPORTED_SOURCE", Severity.WARNING,
                    "fab check currently understands KiCad boards only "
                    f"(got {pcb.source_format})"))
        return findings, facts

    # --- project design rules vs the profile's minimums ----------------------- #
    if rules is not None:
        rows = _rule_comparison(profile, rules)
        facts["project_rules"] = rows
        loose = [r for r in rows if r["looser"]]
        if loose:
            out(Finding(
                "FAB_RULES_LOOSER_THAN_PROFILE", Severity.WARNING,
                "the project's own design rules allow less than profile "
                f"{pid} does: "
                + "; ".join(f"{r['rule']} {r['project_mm']:g}mm < "
                            f"{r['profile_min_mm']:g}mm ({r['profile_key']})"
                            for r in loose)
                + " — KiCad's DRC will pass geometry this profile flags; "
                "raise the Board Setup constraints to the profile's values"))

    # --- stackup drift ------------------------------------------------------ #
    stackup = profile.get("stackup") or {}
    want_layers = stackup.get("layers")
    have_layers = len(pcb.board.get("copper_layers") or [])
    if want_layers is not None and have_layers and have_layers != want_layers:
        out(Finding("FAB_STACKUP_MISMATCH", Severity.ERROR,
                    f"profile {pid} is a {want_layers}-layer profile; board has "
                    f"{have_layers} copper layers"))
    want_thick = stackup.get("thickness_mm")
    have_thick = pcb.board.get("thickness")
    if (want_thick is not None and have_thick
            and abs(float(have_thick) - float(want_thick)) > 0.011):
        out(Finding("FAB_STACKUP_MISMATCH", Severity.ERROR,
                    f"profile thickness {want_thick}mm, board setup says "
                    f"{have_thick}mm"))

    # --- vias ---------------------------------------------------------------- #
    via_rules = profile.get("via") or {}
    min_drill = via_rules.get("min_drill_mm")
    min_pad = via_rules.get("min_pad_mm")
    pref_annular = via_rules.get("preferred_annular_mm")
    max_tented = via_rules.get("max_tented_drill_mm")
    smd_pads = [p for p in pcb.pads if p.get("pad_type") == "smd"]

    # The tenting cap binds a via only where it is TENTED. The board file is
    # the evidence (KiCad >= 9 setup + per-via override; the plot option
    # before that); the order manifest's via_covering is the declaration that
    # settles it for every via when the file is silent.
    board_tenting = pcb.board.get("tenting") or {}
    order_covering = (order or {}).get("via_covering")
    order_tented = order_covering == "tented"
    via_types = {"through": 0, "blind": 0, "buried": 0, "micro": 0}
    tent_counts = {"tented": 0, "untented": 0, "unknown": 0,
                   "not_exposed": 0}
    untentable_unknown: list[dict] = []

    for via in pcb.vias:
        at = via.get("at") or (0.0, 0.0)
        where = f"via@({at[0]:g},{at[1]:g})"
        drill = float(via.get("drill") or 0.0)
        size = float(via.get("size") or 0.0)
        kind = via.get("type") or "through"
        via_types[kind] = via_types.get(kind, 0) + 1
        tent = _tenting_state(via)
        tent_counts[tent] += 1

        if kind in ("blind", "buried", "micro") and via_rules.get("forbid_blind_buried"):
            span = "-".join(via.get("layers") or []) or "layers unknown"
            out(Finding("FAB_VIA_TYPE_FORBIDDEN", Severity.ERROR,
                        f"{where}: {kind} via ({span}) — profile {pid} "
                        "forbids blind/buried/micro vias",
                        pos=at, anchors=[anchor("net", via.get("net") or "?", at)]))

        if min_drill is not None and drill and drill < float(min_drill) - _EPS:
            out(Finding("FAB_VIA_PAID_PROCESS", Severity.ERROR,
                        f"{where}: drill {drill:g}mm < free minimum "
                        f"{min_drill}mm — paid small-via process",
                        pos=at))
        elif min_drill is not None and drill and abs(drill - float(min_drill)) <= _EPS:
            out(Finding("FAB_VIA_MIN_MARGIN", Severity.NOTE,
                        f"{where}: drill {drill:g}mm sits exactly on the free "
                        f"minimum ({min_drill}mm) — zero manufacturing margin",
                        pos=at))
        if min_pad is not None and size and size < float(min_pad) - _EPS:
            out(Finding("FAB_VIA_PAID_PROCESS", Severity.ERROR,
                        f"{where}: pad {size:g}mm < free minimum {min_pad}mm",
                        pos=at))
        if (pref_annular is not None and drill and size
                and (size - drill) / 2 < float(pref_annular) - _EPS):
            out(Finding("FAB_VIA_ANNULAR_BELOW_PREFERRED", Severity.NOTE,
                        f"{where}: annular {(size - drill) / 2:.3f}mm < preferred "
                        f"{pref_annular}mm — passes, but below the recommended "
                        "margin",
                        pos=at))
        if (max_tented is not None and drill and tent != "not_exposed"
                and drill > float(max_tented) + _EPS):
            if tent == "tented" or order_tented:
                basis = ("the board file tents this via"
                         if tent == "tented" else
                         "the order declares via_covering \"tented\"")
                out(Finding("FAB_VIA_TENTED_TOO_BIG", Severity.ERROR,
                            f"{where}: drill {drill:g}mm > tenting cap "
                            f"{max_tented}mm — cannot be tented under {pid} "
                            f"({basis})",
                            pos=at))
            elif tent == "unknown" and order_covering is None:
                untentable_unknown.append(via)

        if via_rules.get("forbid_via_in_pad"):
            host = next((p for p in smd_pads
                         if _point_in_pad(at[0], at[1], p)), None)
            if host is not None:
                comp = host.get("component") or "?"
                rule, expired = _exception_for(
                    profile, etype="thermal_via", component=comp, today=today)
                label = f"{comp}.{host.get('number')}"
                if rule is not None and not expired:
                    out(Finding("FAB_VIA_IN_PAD_EXCEPTION", Severity.NOTE,
                                f"{where}: inside pad {label} — allowed by "
                                f"exception ({rule.get('reason')}, owner "
                                f"{rule.get('owner')})",
                                pos=at, anchors=[anchor("pin", label, at)]))
                elif rule is not None and expired:
                    out(Finding("FAB_EXCEPTION_EXPIRED", Severity.ERROR,
                                f"{where}: inside pad {label} — matching "
                                f"exception EXPIRED ({rule.get('expires')}); "
                                f"owner {rule.get('owner')} must re-review",
                                pos=at, anchors=[anchor("pin", label, at)]))
                else:
                    out(Finding("FAB_VIA_IN_PAD", Severity.ERROR,
                                f"{where}: via inside SMD pad {label} — "
                                f"forbidden by {pid} (register a thermal_via "
                                "exception if intentional)",
                                pos=at, anchors=[anchor("pin", label, at)]))

    if untentable_unknown:
        worst = max(untentable_unknown, key=lambda v: float(v.get("drill") or 0.0))
        at = worst.get("at") or (0.0, 0.0)
        out(Finding(
            "FAB_TENTING_UNKNOWN", Severity.NOTE,
            f"{len(untentable_unknown)} via(s) have a drill over the tenting "
            f"cap {max_tented}mm (largest {float(worst.get('drill') or 0.0):g}mm "
            f"at ({at[0]:g},{at[1]:g})), but the board file does not say "
            "whether they are tented and no order manifest declares "
            "via_covering — the cap was NOT applied to them. Pass --order, "
            "or save the board with KiCad 9 or newer (Board Setup > Solder "
            "Mask/Paste tenting)",
            pos=at))
    facts["via_types"] = via_types
    facts["tenting"] = {
        "board": {"front": board_tenting.get("front"),
                  "back": board_tenting.get("back"),
                  "source": board_tenting.get("source")},
        "vias": tent_counts,
        "order_via_covering": order_covering,
        "cap_applies_to": ("every via reaching an outer layer (the order "
                           "declares tented)" if order_tented else
                           "vias the board file marks tented"),
    }

    # --- slots (oval drills) ---------------------------------------------------- #
    slot_rules = profile.get("slot") or {}
    if slot_rules:
        for pad in pcb.pads:
            sizes = pad.get("drill_size")
            if pad.get("drill_shape") != "oval" or not sizes:
                continue
            width, length = min(sizes), max(sizes)
            if length - width <= _EPS:      # equal sizes: a round hole
                continue
            plated = pad.get("pad_type") != "np_thru_hole"
            key = "min_plated_width_mm" if plated else "min_non_plated_width_mm"
            if slot_rules.get(key) is None:
                key = "min_width_mm"
            floor = slot_rules.get(key)
            if floor is None or width >= float(floor) - _EPS:
                continue
            at = pad.get("at") or (0.0, 0.0)
            comp = pad.get("component") or "?"
            label = (f"{comp}.{pad.get('number')}" if pad.get("number")
                     else f"{comp} (unnumbered pad)")
            out(Finding("FAB_SLOT_BELOW_MIN", Severity.ERROR,
                        f"{label}: {'plated' if plated else 'non-plated'} slot "
                        f"{width:g}x{length:g}mm is narrower than the "
                        f"profile minimum {floor}mm ([slot] {key})",
                        pos=at, anchors=[anchor("pin", label, at)]))

    # --- cost thresholds ------------------------------------------------------ #
    cost = profile.get("cost") or {}
    warn_if = cost.get("warn_if") or {}
    error_if = cost.get("error_if") or {}

    def tripped(key: str, value: float, over) -> tuple[Severity, object, str] | None:
        """The stricter table first: [cost.error_if] promotes the same code."""
        for table, severity, why in (
                (error_if, Severity.ERROR,
                 "profile error threshold ([cost.error_if])"),
                (warn_if, Severity.WARNING, "vendor surcharge threshold")):
            thr = table.get(key)
            if thr is not None and over(value, float(thr)):
                return severity, thr, why
        return None

    outline = _outline_facts(pcb.board)
    facts["outline"] = outline
    needs_outline = any(table.get(key) is not None
                        for table in (warn_if, error_if)
                        for key in ("board_length_mm_gte", "board_area_cm2_gt",
                                    "drill_density_per_m2_gt"))
    if outline["method"] == "none":
        if needs_outline:
            out(Finding("FAB_NO_OUTLINE", Severity.WARNING,
                        "board has no Edge.Cuts outline — size/area/drill-density "
                        "cost rules were NOT evaluated"))
    else:
        (x0, y0), (x1, y1) = outline["bbox"]
        length = max(x1 - x0, y1 - y0)
        area_cm2 = outline["area_mm2"] / 100.0
        if outline["method"] == "outline":
            area_what = "board area"
            basis = ("Edge.Cuts outline, "
                     f"{outline['cutouts']} cutout(s) subtracted"
                     if outline["cutouts"] else "Edge.Cuts outline")
        else:
            # Never a silently wrong area: say it is the bounding box, and why.
            area_what = "board bbox area"
            basis = ("bounding-box approximation — the Edge.Cuts outline did "
                     "not close: " + "; ".join(outline["problems"]))
        hit = tripped("board_length_mm_gte", length, lambda v, t: v >= t)
        if hit is not None:
            out(Finding("FAB_COST_BOARD_LENGTH", hit[0],
                        f"board length {length:g}mm >= {hit[1]}mm — {hit[2]}"))
        hit = tripped("board_area_cm2_gt", area_cm2, lambda v, t: v > t)
        if hit is not None:
            out(Finding("FAB_COST_BOARD_AREA", hit[0],
                        f"{area_what} {area_cm2:.1f}cm² > {hit[1]}cm² — "
                        f"{hit[2]} ({basis})"))
        if area_cm2 > 0:
            holes = len(pcb.vias) + sum(
                1 for p in pcb.pads if p.get("drill"))
            density = holes / (area_cm2 / 1e4)
            hit = tripped("drill_density_per_m2_gt", density, lambda v, t: v > t)
            if hit is not None:
                out(Finding("FAB_COST_DRILL_DENSITY", hit[0],
                            f"drill density {density:,.0f}/m² > {hit[1]:,}/m² "
                            f"— {hit[2]} ({holes} holes over "
                            f"{area_cm2:.1f}cm²; {basis})"))
    for table, severity, why in (
            (error_if, Severity.ERROR,
             "profile error threshold ([cost.error_if])"),
            (warn_if, Severity.WARNING, "multilayer fine-line surcharge")):
        thr = table.get("trace_width_mm_lte")
        if thr is None:
            continue
        thin = [t for t in pcb.tracks
                if t.get("width") and float(t["width"]) <= float(thr) + _EPS]
        if thin:
            wmin = min(float(t["width"]) for t in thin)
            out(Finding("FAB_COST_TRACE_WIDTH", severity,
                        f"{len(thin)} track segment(s) at width <= {thr}mm "
                        f"(min {wmin:g}mm) — {why}"))
            break
    return findings, facts


# --------------------------------------------------------------------------- #
# order manifest — declared purchase intent, never guessed from the PCB
# --------------------------------------------------------------------------- #
_ORDER_REQUIRED = (
    "delivery_format",     # single | panel
    "design_count",
    "rush",
    "surface_finish",      # HASL_LF | ENIG | ...
    "via_covering",        # tented | untented | plugged | ...
    "board_material",
    "copper_weight_oz",
)


def load_order(path: os.PathLike | str) -> dict:
    import tomllib

    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        fail("BAD_CONFIG", f"order manifest {path}: {exc}")


def check_order(order: dict, profile: dict | None = None) -> list[Finding]:
    """Validate an order manifest's completeness + profile consistency."""
    findings: list[Finding] = []
    out = findings.append
    missing = [k for k in _ORDER_REQUIRED if k not in order]
    if missing:
        out(Finding("ORDER_INCOMPLETE", Severity.ERROR,
                    "order manifest is missing required field(s): "
                    + ", ".join(missing) + " — these are pricing inputs the "
                    "tool must not guess"))
    if order.get("surface_finish") == "ENIG":
        out(Finding("ORDER_REVIEW_REQUIRED", Severity.WARNING,
                    "surface_finish ENIG: exposed-copper-area surcharge rules "
                    "apply. NOT computed: akcli does not measure the "
                    "mask-open copper area (that needs polygon booleans over "
                    "copper and mask) — review against the vendor calculator"))
    if order.get("delivery_format") == "panel":
        out(Finding("ORDER_REVIEW_REQUIRED", Severity.WARNING,
                    "delivery_format panel: V-Cut/mouse-bite and per-design "
                    "surcharge rules apply. NOT computed: V-cut lines and "
                    "panel rails/tabs are not in the board file, so akcli "
                    "checks none of them — review the panel drawing against "
                    "the vendor calculator"))
    if int(order.get("design_count") or 1) > 1:
        out(Finding("ORDER_REVIEW_REQUIRED", Severity.WARNING,
                    f"design_count {order.get('design_count')}: multi-design "
                    "gerbers are surcharged. NOT computed: akcli reads one "
                    "board and cannot count the designs in a shared gerber — "
                    "declare per vendor rules"))
    if profile is not None:
        want = (profile.get("via") or {}).get("covering")
        got = order.get("via_covering")
        if want and got and want != got:
            out(Finding("ORDER_PROFILE_CONFLICT", Severity.ERROR,
                        f"order declares via_covering {got!r} but profile "
                        f"{profile.get('id')} is built for {want!r}"))
        want_thick = (profile.get("stackup") or {}).get("thickness_mm")
        got_thick = order.get("thickness_mm")
        if want_thick is not None and got_thick is not None \
                and abs(float(got_thick) - float(want_thick)) > 0.011:
            out(Finding("ORDER_PROFILE_CONFLICT", Severity.ERROR,
                        f"order thickness {got_thick}mm != profile "
                        f"{want_thick}mm"))
    return findings


# --------------------------------------------------------------------------- #
# explain — finding code -> profile rule + evidence
# --------------------------------------------------------------------------- #
# One row per code this module emits — no more, no fewer: the gate in
# tests/test_fab.py AST-scans the Finding(...) calls above against these keys.
_EXPLAIN = {
    "FAB_PROFILE_STALE": (
        "source", "A profile bundled with akcli is one dated vendor revision; "
        "its [source] retrieved_at is more than 12 months old (or missing), "
        "and it keeps ageing inside an installed wheel while the vendor's "
        "capability and pricing pages change. A note, never a failure: the "
        "rules still ran. Fix: re-verify the numbers against the source "
        "urls, upgrade akcli, or check against your own profile file."),
    "FAB_UNSUPPORTED_SOURCE": (
        "input", "fab check reads KiCad boards only; any other source stops "
        "it before a single profile rule runs — nothing was checked. Fix: "
        "run it on the .kicad_pcb."),
    "FAB_VIA_PAID_PROCESS": (
        "via", "Vias below the profile's min_drill_mm/min_pad_mm fall into the "
        "vendor's paid small-via process. Fix: enlarge to at least the free "
        "minimum (preferably min_drill + 2*preferred_annular)."),
    "FAB_VIA_MIN_MARGIN": (
        "via", "The drill sits exactly on min_drill_mm: it passes the free "
        "process with zero manufacturing margin. Fix (optional): enlarge the "
        "drill where the routing allows."),
    "FAB_VIA_ANNULAR_BELOW_PREFERRED": (
        "via", "The annular ring, (pad - drill) / 2, is under "
        "preferred_annular_mm. It passes, with less registration margin than "
        "the vendor recommends. Fix: grow the pad or shrink the drill."),
    "FAB_VIA_TENTED_TOO_BIG": (
        "via", "The vendor only tents drills up to max_tented_drill_mm, and "
        "this via IS tented: either the board file says so (KiCad 9+ board "
        "setup or a per-via override; the plot option on older boards) or "
        "the order manifest declares via_covering \"tented\", which applies "
        "the cap to every via. A via the board marks untented is not "
        "flagged unless the order says tented. Fix: shrink the drill, "
        "untent the via, or change the covering option (which changes "
        "pricing)."),
    "FAB_TENTING_UNKNOWN": (
        "via", "Some vias have a drill over max_tented_drill_mm, but nothing "
        "says whether they are tented: the board file carries no tenting "
        "(KiCad 8 and older keep it out of the board, apart from a saved "
        "plot option) and no order manifest declares via_covering. The cap "
        "was NOT applied to them — one note, not an error per via, and not "
        "a pass. Fix: pass --order with via_covering, or save the board "
        "with KiCad 9 or newer so it records tenting."),
    "FAB_SLOT_BELOW_MIN": (
        "slot", "A pad's oval drill (a slot) is narrower than the profile "
        "allows: min_plated_width_mm for a plated slot, "
        "min_non_plated_width_mm for an NPTH one, min_width_mm when the "
        "specific key is absent. The width is the smaller of the two drill "
        "sizes. Slots routed on Edge.Cuts are not measured. Fix: widen the "
        "slot, or use the footprint variant with a round hole."),
    "FAB_RULES_LOOSER_THAN_PROFILE": (
        "rules", "The project's own DRC constraints (.kicad_pro, "
        "board.design_settings.rules) allow less than the profile's minimum "
        "for the rules named — so KiCad's DRC passes geometry this profile "
        "flags. Compared: every [rules] key the profile states, plus "
        "via.min_pad_mm against min_via_diameter and via.min_drill_mm "
        "against min_through_hole_diameter. A rule the project does not "
        "state is not compared. Fix: raise the Board Setup constraints to "
        "the profile's values (or pick the profile that matches the "
        "process you are paying for)."),
    "FAB_VIA_IN_PAD": (
        "via", "Via-in-pad requires plugged/capped vias (a paid process) or "
        "causes solder wicking. Fix: move the via out of the pad, or register "
        "a thermal_via exception with owner+reason for exposed pads."),
    "FAB_VIA_IN_PAD_EXCEPTION": (
        "exception", "A via inside an SMD pad (banned by forbid_via_in_pad) "
        "is covered by an unexpired thermal_via exception. Reported so the "
        "approval stays visible with its owner and reason; nothing to fix."),
    "FAB_EXCEPTION_EXPIRED": (
        "exception", "The thermal_via exception covering this via-in-pad is "
        "past its expires date (or the date is not ISO YYYY-MM-DD), so it no "
        "longer excuses the via. Fix: have the owner re-review and renew "
        "expires, or move the via out of the pad."),
    "FAB_VIA_TYPE_FORBIDDEN": (
        "via", "Blind/buried/micro vias are excluded by this profile "
        "(forbid_blind_buried). They require a different, costlier stackup. "
        "A via is buried when KiCad 10 marks it so, or when an older "
        "board's blind via spans a layer pair touching neither F.Cu nor "
        "B.Cu."),
    "FAB_STACKUP_MISMATCH": (
        "stackup", "The board's layer count/thickness drifted from the "
        "profile. Either the board or the chosen profile revision is wrong."),
    "FAB_NO_OUTLINE": (
        "cost.warn_if", "The board has nothing on Edge.Cuts, so the board "
        "length/area/drill-density thresholds were NOT evaluated — their "
        "silence is missing evidence, not a pass. Fix: draw the outline on "
        "Edge.Cuts."),
    "FAB_COST_BOARD_LENGTH": (
        "cost.warn_if", "Long boards are surcharged: the longer side of the "
        "Edge.Cuts bounding box reached board_length_mm_gte. A warning "
        "under [cost.warn_if]; the same key under [cost.error_if] makes it "
        "an error."),
    "FAB_COST_BOARD_AREA": (
        "cost.warn_if", "Large boards are surcharged: the board area "
        "exceeded board_area_cm2_gt. The area is the Edge.Cuts outline "
        "(arcs exact, cutouts subtracted) when it closes; otherwise the "
        "bounding box, and the message says so and why. A warning under "
        "[cost.warn_if], an error under [cost.error_if]."),
    "FAB_COST_DRILL_DENSITY": (
        "cost.warn_if", "High drill density is surcharged: vias plus "
        "drilled pads per square metre of board exceeded "
        "drill_density_per_m2_gt. Uses the same area as the board-area "
        "rule (outline, or bounding box when Edge.Cuts does not close — "
        "stated in the message). A warning under [cost.warn_if], an error "
        "under [cost.error_if]."),
    "FAB_COST_TRACE_WIDTH": (
        "cost.warn_if", "Fine multilayer traces are surcharged: track "
        "segments at or under trace_width_mm_lte. A warning under "
        "[cost.warn_if], an error under [cost.error_if]."),
    "ORDER_INCOMPLETE": (
        "order", "The order manifest is purchase intent — akcli never derives "
        "delivery format, finish, or covering from the PCB. Declare them."),
    "ORDER_REVIEW_REQUIRED": (
        "order", "The order declares an option whose surcharge akcli does NOT "
        "compute: ENIG finish (the exposed-copper area needs polygon "
        "booleans over copper and mask), panel delivery (V-cut lines and "
        "panel rails/tabs are not in the board file) or design_count > 1. "
        "Nothing was measured for these. Fix: price it in the vendor's "
        "calculator before ordering."),
    "ORDER_PROFILE_CONFLICT": (
        "order", "The order's via_covering or thickness_mm differs from what "
        "the profile is built for ([via] covering, [stackup] thickness_mm), "
        "so the profile's rules do not describe the board being bought. Fix: "
        "align the order, or check against the profile that matches it."),
}


def explain(code: str, profile: dict | None = None) -> str | None:
    entry = _EXPLAIN.get(code)
    if entry is None:
        return None
    section, text = entry
    lines = [f"{code}", f"  profile section: [{section}]", f"  {text}"]
    if profile is not None:
        src = profile.get("source") or {}
        for url in src.get("urls") or []:
            lines.append(f"  source: {url} (retrieved {src.get('retrieved_at', '?')})")
        lines.append(f"  profile: {profile.get('id')}")
    return "\n".join(lines)
