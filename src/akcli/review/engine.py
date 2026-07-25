"""Review analysis driver: model in, stamped + deterministically-ordered
findings out.

Per-detector containment mirrors the op executor's contract: a detector bug
surfaces as ONE ``REVIEW_DETECTOR_ERROR`` finding, never a traceback aborting
the run. Every finding leaves stamped with its detector name and a
wording-immune fingerprint, and the metadata block always reports what ran
and what was skipped — a vacuous "no findings" is never mistaken for a
reviewed board.
"""

from __future__ import annotations

from ..model import Schematic
from ..report import Finding, Severity, compute_fingerprint
from . import DETECTORS, PROFILES, topo


def analyze(sch: Schematic, *, pcb: object | None = None,
            profile: str = "standard",
            detectors: list[str] | None = None,
            facts: object | None = None,
            gerbers: object | None = None) -> tuple[list[Finding], dict]:
    """Run the selected detectors; return ``(findings, metadata)``.

    ``detectors`` (names) overrides the profile's family selection; ``facts``
    is an optional :class:`~.facts.FactsStore` that lets detectors upgrade to
    ``datasheet_backed`` judgements. Findings are sorted deterministically
    (severity desc, code, refs) so repeat runs and cross-platform runs are
    byte-stable.
    """
    from . import detectors as _reg  # noqa: F401 — registration side effect

    families = PROFILES.get(profile)
    if families is None:
        raise KeyError(profile)
    if detectors is not None:
        wanted = set(detectors)
        selected = [d for name, d in sorted(DETECTORS.items())
                    if name in wanted]
        unknown = wanted - set(DETECTORS)
        if unknown:
            raise KeyError(", ".join(sorted(unknown)))
    else:
        selected = [d for _name, d in sorted(DETECTORS.items())
                    if d.family in families
                    # input-bound families skip without their input; they
                    # land in detectors_skipped instead of running vacuously
                    and not (d.family in ("pcb", "emc") and pcb is None)
                    and not (d.family == "gerber" and gerbers is None)]

    ctx = topo.build_ctx(sch, pcb, facts, gerbers)
    findings: list[Finding] = []
    ran: list[str] = []
    for det in selected:
        ran.append(det.name)
        try:
            batch = det.run(ctx) or []
        except Exception as exc:  # noqa: BLE001 — per-detector containment
            findings.append(Finding(
                code="REVIEW_DETECTOR_ERROR", severity=Severity.WARNING,
                message=f"{det.name}: {type(exc).__name__}: {exc}",
                detector=det.name, confidence="deterministic",
                status="quarantined"))
            continue
        for f in batch:
            if f.detector is None:
                f.detector = det.name
            if f.rule_version is None:
                f.rule_version = next(
                    (r.version for r in det.rules if r.code == f.code), "1")
            if f.fingerprint is None:
                f.fingerprint = compute_fingerprint(
                    f.code, f.rule_version, f.anchors)
        findings.extend(batch)

    # Facts linkage audit: a facts file that matches NO component MPN would
    # otherwise fail silently (findings stay heuristic, nothing upgrades to
    # datasheet_backed, nobody notices). Engine-level like
    # REVIEW_DETECTOR_ERROR — this is bookkeeping, not a detector rule.
    unmatched_mpns: list[str] = []
    if facts is not None and getattr(facts, "by_mpn", None):
        from .facts import component_mpn
        comp_mpns = {m.upper() for m in
                     (component_mpn(c) for c in sch.components) if m}
        unmatched_mpns = sorted(k for k in facts.by_mpn
                                if k not in comp_mpns)
        if unmatched_mpns:
            findings.append(Finding(
                code="REVIEW_FACTS_UNMATCHED", severity=Severity.NOTE,
                message=(f"{len(unmatched_mpns)} audited facts file(s) match "
                         f"no component MPN: {', '.join(unmatched_mpns)} — "
                         "their findings stay heuristic instead of "
                         "datasheet_backed; give the component an MPN "
                         "parameter (or its exact part-number Value) to "
                         "attach them"),
                refs=list(unmatched_mpns),
                detector="engine.facts", confidence="deterministic"))

    findings.sort(key=lambda f: (
        -{"info": 0, "note": 1, "warning": 2, "error": 3, "critical": 4}
        .get(f.severity.value, 0),
        f.code, tuple(map(str, f.refs))))

    trust: dict[str, int] = {}
    for f in findings:
        key = f.confidence or "unspecified"
        trust[key] = trust.get(key, 0) + 1
    meta = {
        "review_profile": profile,
        "facts_mpns": (sorted(facts.by_mpn) if facts is not None
                       and getattr(facts, "by_mpn", None) else []),
        "facts_unmatched": unmatched_mpns,
        "source_format": sch.source_format,
        "components": len(sch.components),
        "nets": len(sch.nets),
        "detectors_run": ran,
        "detectors_skipped": sorted(set(DETECTORS) - set(ran)),
        "trust_summary": dict(sorted(trust.items())),
    }
    # EMC aggregation: an ADVISORY risk score + near-field probe points,
    # emitted whenever the emc family actually ran (0 findings → score 0,
    # so "reviewed and quiet" is distinguishable from "never reviewed").
    if any(DETECTORS[name].family == "emc" for name in ran):
        from .tables import EMC_DISCLAIMER, EMC_RISK_WEIGHTS
        emc = [f for f in findings if f.code.startswith("REVIEW_EMC_")]
        score = min(100, sum(EMC_RISK_WEIGHTS.get(f.severity.value, 0)
                             for f in emc))
        probes = sorted({str(r) for f in emc
                         if f.severity.value in ("warning", "error",
                                                 "critical")
                         for r in f.refs})
        meta["emc"] = {"risk_score": score, "findings": len(emc),
                       "probe_points": probes, "note": EMC_DISCLAIMER}
    return findings, meta
