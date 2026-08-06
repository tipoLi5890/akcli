"""``review validate`` — the deterministic gate for LLM deep-review output (M8).

An agent may propose findings; it may not assert them. Every candidate passes
four gates, and a failure lands in ``quarantined[]`` with its reasons —
nothing is silently dropped, nothing is silently accepted:

* **G1 schema** — required fields, legal severity; the candidate may not
  claim any confidence but ``llm_reviewed`` (that vocabulary belongs to the
  deterministic layer) and may not pre-set a status.
* **G2 anchors** — every anchor must resolve against the schematic model
  (component / net / pin actually exist); an unanchored claim is not a
  finding.
* **G3 datasheet evidence** — a cited sha256+page must match the facts
  store, and a quote must be found on that page when ``pdftotext`` can look.
* **G4 masquerade** — the candidate's code may not collide with a registered
  deterministic rule (an LLM restating ``REVIEW_XTAL_LOAD`` would inherit
  its credibility).

Accepted candidates become ``llm_reviewed`` observations with a computed
fingerprint. They can never block a release, never override a deterministic
finding, and never auto-create a contract — the caller gets observations,
full stop.
"""

from __future__ import annotations

from ..model import Schematic
from ..report import Finding, Severity, compute_fingerprint
from . import rules_index, topo

_LEGAL_SEVERITIES = {"info", "note", "warning", "error", "critical"}


def _g1_schema(c: dict, reasons: list[str]) -> None:
    for req in ("code", "severity", "message"):
        if not isinstance(c.get(req), str) or not c[req]:
            reasons.append(f"schema: missing/empty {req!r}")
    if c.get("severity") not in _LEGAL_SEVERITIES:
        reasons.append(f"schema: illegal severity {c.get('severity')!r}")
    code = c.get("code") or ""
    if code and not code.startswith("REVIEW_"):
        reasons.append("schema: code must start with REVIEW_")
    conf = c.get("confidence")
    if conf not in (None, "llm_reviewed"):
        reasons.append(
            f"confidence: candidate claims {conf!r} — only the deterministic "
            "layer assigns that")
    if c.get("status") not in (None, "reported"):
        reasons.append(f"status: candidate pre-sets {c.get('status')!r}")
    if not c.get("anchors"):
        reasons.append("anchors: none — an unanchored claim is not a finding")


def _g2_anchors(c: dict, ctx: topo.ReviewCtx, reasons: list[str]) -> None:
    net_names: set[str] = set()
    for net in ctx.sch.nets:
        from ..checks.power import net_candidate_names
        net_names.update(n for n in net_candidate_names(net) if n)
    for a in c.get("anchors") or []:
        if not isinstance(a, dict):
            reasons.append(f"anchor: malformed {a!r}")
            continue
        kind, aid = a.get("kind"), str(a.get("id") or "")
        if kind == "component":
            if aid not in ctx.comps:
                reasons.append(f"anchor: component {aid!r} not on the sheet")
        elif kind == "net":
            if aid not in net_names:
                reasons.append(f"anchor: net {aid!r} not on the sheet")
        elif kind == "pin":
            ref, _, pin = aid.partition(".")
            comp = ctx.comps.get(ref)
            if comp is None:
                reasons.append(f"anchor: pin {aid!r}: no component {ref!r}")
            elif comp.pins and not any(str(p.number) == pin
                                       for p in comp.pins):
                reasons.append(f"anchor: pin {aid!r} not on {ref}")
        elif kind == "label":
            pass                                   # labels aren't indexed
        else:
            reasons.append(f"anchor: unknown kind {kind!r}")


def _g3_datasheet(c: dict, facts_store, root, reasons: list[str]) -> None:
    ds = (c.get("evidence") or {}).get("datasheet")
    if not ds:
        return
    if facts_store is None:
        reasons.append("datasheet: cited but no facts store to verify "
                       "against")
        return
    sha = str(ds.get("sha256") or "")
    page = ds.get("page")
    known = {f.sha256: f for f in facts_store.by_mpn.values()}
    facts = known.get(sha)
    if facts is None:
        reasons.append("datasheet: sha256 matches no PDF in the facts store")
        return
    if not isinstance(page, int) or page < 1:
        reasons.append("datasheet: page must be an integer >= 1")
        return
    quote = ds.get("quote")
    if quote and root is not None and facts.pdf:
        from ..drivers import pdftotext
        pdf_path = root / facts.pdf
        if pdftotext.available() and pdf_path.is_file():
            hit = pdftotext.quote_present(str(pdf_path), page, str(quote))
            if hit is False:
                reasons.append(
                    f"datasheet: quote not found on page {page} of "
                    f"{facts.pdf}")


def _g4_masquerade(c: dict, deterministic_codes: set[str],
                   reasons: list[str]) -> None:
    if (c.get("code") or "") in deterministic_codes:
        reasons.append(
            f"masquerade: {c['code']} is a registered deterministic rule — "
            "an LLM claim may not borrow its identity")


def validate_candidates(doc: dict, sch: Schematic, *,
                        facts=None, facts_root=None) -> tuple[list, list]:
    """``(accepted_findings, quarantined)`` for a candidates document.

    ``doc`` carries ``candidates`` (or ``findings``) — a list of
    finding-shaped dicts. Accepted candidates come back as
    :class:`~..report.Finding` objects stamped ``llm_reviewed`` /
    ``reported`` with a computed fingerprint; each quarantined entry is
    ``{"candidate": <original>, "reasons": [...]}``.
    """
    cands = doc.get("candidates")
    if cands is None:
        cands = doc.get("findings")
    if not isinstance(cands, list):
        raise ValueError("candidates document needs a 'candidates' (or "
                         "'findings') array")
    ctx = topo.build_ctx(sch)
    deterministic_codes = set(rules_index())
    accepted: list[Finding] = []
    quarantined: list[dict] = []
    for c in cands:
        if not isinstance(c, dict):
            quarantined.append({"candidate": c,
                                "reasons": ["schema: not an object"]})
            continue
        reasons: list[str] = []
        _g1_schema(c, reasons)
        if not reasons:
            _g2_anchors(c, ctx, reasons)
            _g3_datasheet(c, facts, facts_root, reasons)
            _g4_masquerade(c, deterministic_codes, reasons)
        if reasons:
            quarantined.append({"candidate": c, "reasons": reasons})
            continue
        anchors = list(c.get("anchors") or [])
        accepted.append(Finding(
            code=c["code"], severity=Severity(c["severity"]),
            message=c["message"], refs=list(c.get("refs") or []),
            anchors=anchors, detector="review.validate",
            confidence="llm_reviewed",
            evidence=c.get("evidence"),
            rule_version=str(c.get("rule_version") or "1"),
            fingerprint=compute_fingerprint(
                c["code"], str(c.get("rule_version") or "1"), anchors),
            remediation=c.get("remediation"),
            status="reported"))
    accepted.sort(key=lambda f: (f.code, f.fingerprint or ""))
    return accepted, quarantined
