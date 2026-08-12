"""``review propose`` — findings into declarative candidate changes (M7).

A proposal never touches a design file. Value fixes are recomputed here
(never copied blindly from the finding) and snapped to the E-series via
``akcli calc eseries``; the resulting op-list draft goes through the normal
``plan → draw --apply`` pipeline and inherits every safety rail. The
structural guarantee, enforced both here and in the shipped schema: **a
proposal with open ``requires_confirmation`` items carries no op-list
draft** — a fix that depends on unobserved conditions (loads, rails, layout)
cannot be auto-applied. PCB-side fixes are ``layout`` proposals: akcli
writes schematics only.

Contract drafts close the sedimentation chain: a datasheet-backed fix
carries its fact's sha256+page straight into the contract's ``evidence``
line, so the future gate cites the same document the finding did.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import contract
from ..calc.si import fmt_eng


def _eng(value: float) -> str:
    return fmt_eng(value, "").replace(" ", "")


def _snap(value: float, series: str) -> tuple[float, str]:
    """E-series nearest value via ``calc eseries``: ``(value, compact_str)``."""
    from ..calc import compute
    env = compute("eseries", {"value": value, "series": series})
    nearest = env["results"]["nearest"]["value"]
    return nearest, _eng(nearest)


def _oplist(*ops: dict) -> dict:
    return {"protocol_version": contract.PROTOCOL_VERSIONS["oplist"],
            "target_format": "kicad",
            "target_file": "<board.kicad_sch>", "ops": list(ops)}


def _set_value(ref: str, value: str) -> dict:
    return {"op": "set_component_parameters", "designator": ref,
            "value": value}


def _contract_draft(fid: str, evidence: list[str],
                    values: list[tuple[str, str]]) -> str:
    lines = []
    for ref, value in values:
        lines += [
            "[[contract]]",
            f'id = "review-{fid[:8]}-{ref}"',
            "evidence = [" + ", ".join(json.dumps(e) for e in evidence) + "]",
            f'component = "{ref}"',
            f'value = "{value}"',
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def _ds_evidence_lines(finding: dict) -> list[str]:
    ds = (finding.get("evidence") or {}).get("datasheet")
    if not ds:
        return []
    quote = f' "{ds["quote"]}"' if ds.get("quote") else ""
    return [f"datasheet p{ds['page']} sha256:{ds['sha256'][:16]}…{quote}"]


# --------------------------------------------------------------------------- #
# per-kind builders: (finding) -> partial proposal dict or None
# --------------------------------------------------------------------------- #
def _fb_divider_retune(f: dict) -> dict | None:
    fp = f.get("fix_params") or {}
    inputs = ((f.get("evidence") or {}).get("calc") or {}).get("inputs") or {}
    rb = inputs.get("r_bottom")
    vout = inputs.get("vout_from_rail")
    vref = fp.get("vref_spec")
    r_top_ref = fp.get("r_top")
    if not all(isinstance(v, (int, float)) and v > 0
               for v in (rb, vout, vref)) or vout <= vref or not r_top_ref:
        return None
    ideal = rb * (vout / vref - 1.0)
    snapped, text = _snap(ideal, "E96")
    vout_real = vref * (snapped + rb) / rb
    return {
        "kind": "set_value",
        "summary": (f"retune {r_top_ref} to {text} so the divider hits the "
                    f"datasheet Vref {vref:g} V (rail lands at "
                    f"{vout_real:.4g} V)"),
        "rationale": (f"R_top = R_b·(Vout/Vref − 1) = {ideal:,.0f} Ω, "
                      f"snapped to E96 {text}"),
        "requires_confirmation": [],
        "oplist_draft": _oplist(_set_value(r_top_ref, text)),
        "contract_draft": _contract_draft(
            f.get("fingerprint", ""), _ds_evidence_lines(f),
            [(r_top_ref, text)]),
        "sim_draft": {
            "analyses": {"op": ""},
            "_draft_note": "wire the measurement to the real output net",
            "assertions": [{"name": "vout", "signal": "v(<vout-net>)",
                            "approx": round(vout, 4), "tol": 0.05}],
        },
    }


def _xtal_load_retune(f: dict) -> dict | None:
    fp = f.get("fix_params") or {}
    c1, c2 = fp.get("c1"), fp.get("c2")
    c_pf = fp.get("c_suggested_pf")
    if not (c1 and c2 and isinstance(c_pf, (int, float)) and c_pf > 0):
        return None
    snapped, text = _snap(c_pf * 1e-12, "E24")
    return {
        "kind": "set_value",
        "summary": f"refit {c1}/{c2} to {text} to hit the datasheet CL",
        "rationale": (f"C = 2·(CL − C_stray) = {c_pf:g} pF, snapped to "
                      f"E24 {text}"),
        "requires_confirmation": [],
        "oplist_draft": _oplist(_set_value(c1, text), _set_value(c2, text)),
        "contract_draft": _contract_draft(
            f.get("fingerprint", ""), _ds_evidence_lines(f),
            [(c1, text), (c2, text)]),
        "sim_draft": None,
    }


def _retune_pullup(f: dict) -> dict | None:
    fp = f.get("fix_params") or {}
    ref = fp.get("ref")
    results = ((f.get("evidence") or {}).get("calc") or {}).get("results") or {}
    suggested = (results.get("suggested") or {}).get("value")
    if not ref or not isinstance(suggested, (int, float)) or suggested <= 0:
        return None
    text = _eng(float(suggested))
    return {
        "kind": "set_value",
        "summary": f"raise pull-up {ref} to {text} (inside the I²C window)",
        "rationale": ("suggested value from `calc i2c-pullup` "
                      "(NXP UM10204 window); C_b assumption rides in the "
                      "finding's evidence"),
        "requires_confirmation": [],
        "oplist_draft": _oplist(_set_value(ref, text)),
        "contract_draft": None,
        "sim_draft": None,
    }


def _confirm(summary: str, needs: list[str]):
    def build(f: dict) -> dict:
        return {"kind": "confirm", "summary": summary.format(**{
                    **(f.get("fix_params") or {}), "refs": ", ".join(
                        map(str, f.get("refs") or []))}),
                "rationale": f.get("remediation") or "",
                "requires_confirmation": list(needs),
                "oplist_draft": None, "contract_draft": None,
                "sim_draft": None}
    return build


def _layout(summary: str):
    def build(f: dict) -> dict:
        return {"kind": "layout", "summary": summary.format(**{
                    **(f.get("fix_params") or {}), "refs": ", ".join(
                        map(str, f.get("refs") or []))}),
                "rationale": f.get("remediation") or "",
                "requires_confirmation": [
                    "PCB layout edit — akcli writes schematics only"],
                "oplist_draft": None, "contract_draft": None,
                "sim_draft": None}
    return build


_BUILDERS = {
    "fb_divider_retune": _fb_divider_retune,
    "xtal_load_retune": _xtal_load_retune,
    "retune_pullup": _retune_pullup,
    "fb_divider": _confirm(
        "confirm the regulator's Vref, then retune the {r_top}/{r_bottom} "
        "divider", ["regulator datasheet Vref (add a facts file)"]),
    "divider_tap": _confirm(
        "decide whether {r_top}/{r_bottom} values or the tap net's name is "
        "wrong", ["which side is wrong: resistor values or the net name"]),
    "add_esd": _confirm(
        "clamp connector {connector} signals ({refs})",
        ["TVS part selection", "board placement"]),
    "add_pullup": _confirm(
        "fit a pull-up on {net}",
        ["bus rail", "target value (run `akcli calc i2c-pullup`)"]),
    "tie_enable": _confirm(
        "tie enable pin {pin} to its rail or controller",
        ["target rail / control line", "datasheet default-state row"]),
    "move_decap": _layout("move {cap} next to {target_pad} "
                          "(now {distance_mm} mm away)"),
    "move_tvs": _layout("move {tvs} to the connector pins ({target_pad})"),
    "diffpair_match": _layout("length-match the pair: add {add_mm} mm to "
                              "{short_side}"),
    "add_thermal_vias": _layout("stitch {want} vias through {pad} "
                                "(has {have})"),
}


def build_proposals(doc: dict, source: str = "<findings>") -> dict:
    """Findings envelope → proposals document (pure; never touches files)."""
    findings = [f for f in doc.get("findings", []) if isinstance(f, dict)]
    proposals: list[dict] = []
    for f in sorted(findings, key=lambda f: (f.get("fingerprint") or "",
                                             f.get("code") or "")):
        kind = (f.get("fix_params") or {}).get("kind")
        builder = _BUILDERS.get(kind)
        if builder is None:
            continue
        body = builder(f)
        if body is None:
            continue
        # structural guarantee (also enforced by the shipped schema)
        assert not (body["requires_confirmation"] and body["oplist_draft"]), \
            "unconfirmed proposal must not carry an op-list draft"
        proposals.append({
            "id": f"P{len(proposals) + 1}",
            "finding_fingerprint": f.get("fingerprint") or "0" * 32,
            "finding_code": f.get("code") or "",
            **body,
            "status": "proposed",
        })
    return {"proposals_version": contract.SCHEMA_VERSIONS["proposals"],
            "source": {"findings": str(source), "count": len(findings)},
            "proposals": proposals}


def load_findings(path: Path) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
        raise ValueError("not a findings envelope "
                         "(schema_version/metadata/findings)")
    return doc
