"""Render findings (+ metadata caveats) as text or JSON (SPEC §3.1).

A report ALWAYS prints a metadata header — passive-pin ratio, No-ERC suppressed
count, unnamed-net count and frac-coord presence — so that a vacuous "no findings"
pass is never mistaken for a clean board.
"""

from __future__ import annotations

import enum
import fnmatch
import hashlib
import json
from dataclasses import dataclass, field, replace


class Severity(enum.Enum):
    """Finding severity, ordered least -> most serious."""

    INFO = "info"
    NOTE = "note"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# rank for sorting (higher = more serious)
_SEV_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.NOTE: 1,
    Severity.WARNING: 2,
    Severity.ERROR: 3,
    Severity.CRITICAL: 4,
}


# Review evidence-envelope vocabularies (schemas/findings.schema.json
# mirrors these).
CONFIDENCE_LEVELS: tuple[str, ...] = (
    "deterministic", "heuristic", "datasheet_backed", "llm_reviewed",
)
FINDING_STATUSES: tuple[str, ...] = (
    "reported", "waived", "accepted_risk", "quarantined",
    "insufficient_evidence",
)


@dataclass
class Finding:
    """A single check result.

    ``pos`` is an optional ``(x_mil, y_mil)`` world coordinate the finding
    points at (top-left origin, +Y down — the canonical model frame). ``anchors``
    is an optional list of ``{kind, id, pos?}`` dicts naming the concrete
    entities involved (kind ∈ ``component|pin|net|label``); build them with
    :func:`anchor`. Both feed the JSON/SARIF exports and the web UI's markers;
    findings without positions render exactly as before.

    The remaining fields are the OPTIONAL review evidence envelope (all default
    to unset and are serialized only when present, so pre-review findings keep
    their historical JSON shape byte-for-byte):

    * ``detector`` — the detector module that produced the finding.
    * ``confidence`` — one of :data:`CONFIDENCE_LEVELS`.
    * ``evidence`` — dict carrying the *why*: ``source`` (datasheet / topology /
      heuristic_rule / symbol_footprint / bom / geometry / api_lookup / calc),
      a ``calc`` envelope, ``datasheet`` ``{sha256, page, quote?}`` and
      ``assumptions``.
    * ``rule_version`` — bumped (major) only when the rule's SEMANTICS change.
    * ``fingerprint`` — stable identity over (code, rule_version-major, anchors);
      deliberately message-free so rewording never churns CI alerts. Compute
      with :func:`compute_fingerprint`.
    * ``remediation`` — suggested fix, human-readable.
    * ``fix_params`` — structured fix metadata for ``review propose``.
    * ``status`` — one of :data:`FINDING_STATUSES`.
    """

    code: str
    severity: Severity
    message: str
    refs: list = field(default_factory=list)
    pos: tuple | None = None
    anchors: list = field(default_factory=list)
    detector: str | None = None
    confidence: str | None = None
    evidence: dict | None = None
    rule_version: str | None = None
    fingerprint: str | None = None
    remediation: str | None = None
    fix_params: dict | None = None
    status: str | None = None


def compute_fingerprint(code: str, rule_version: str | None,
                        anchors: list) -> str:
    """Stable finding identity: sha256(code | rule-major | sorted anchors)[:32].

    Message text is deliberately EXCLUDED — rewording a finding must never
    churn alert identity, so identity rests on the rule code, its semantic
    (major) version, and the anchored entities alone.
    """
    major = str(rule_version or "1").split(".", 1)[0]
    ids = sorted(f"{a.get('kind', '')}:{a.get('id', '')}" for a in anchors or [])
    basis = "|".join([code, major, *ids])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def anchor(kind: str, id: object, pos: tuple | None = None) -> dict:
    """Build one ``anchors`` entry: ``{kind, id[, pos]}`` (see :class:`Finding`)."""
    a: dict = {"kind": kind, "id": str(id)}
    if pos is not None:
        a["pos"] = (float(pos[0]), float(pos[1]))
    return a


def _match_waiver(f: Finding, waivers: list[dict]) -> dict | None:
    """First config ``[[waiver]]`` whose ``code`` (fnmatch) and, if given, ``refs``
    (any fnmatch pattern vs any of ``f.refs``) match ``f``; else ``None``."""
    for w in waivers or []:
        code = w.get("code")
        if code and not fnmatch.fnmatchcase(f.code, str(code)):
            continue
        pats = w.get("refs")
        if pats:
            pat_list = [pats] if isinstance(pats, str) else list(pats)
            refs = [str(r) for r in (f.refs or [])]
            if not any(fnmatch.fnmatchcase(r, str(p)) for r in refs for p in pat_list):
                continue
        return w
    return None


# ``[[waiver]].severity`` token -> the Severity a matched finding is demoted to
# (``off`` drops the finding entirely and has no entry here).
_WAIVE_TARGET: dict[str, Severity] = {
    "note": Severity.NOTE,
    "info": Severity.INFO,
}


def apply_waivers(
    findings: list[Finding], waivers: list[dict],
    detail_out: list[dict] | None = None,
) -> tuple[list[Finding], int, int]:
    """Apply config ``[[waiver]]`` entries uniformly to every finding.

    Returns ``(kept, waived, demoted)`` where ``waived`` counts findings a waiver
    touched (dropped or demoted) and ``demoted`` counts the subset that were
    downgraded (``severity = note|info``) rather than removed (``severity =
    off``). A finding's ``code``/``message``/``refs`` are never mutated, so SARIF
    ``partialFingerprints`` stay stable across a waiver's application.

    ``detail_out``, when a list is supplied, receives one record per waived
    finding (code, refs, action, reason) so reports can SHOW what a waiver
    silenced — "config-waived: 1" alone reads as clean when it isn't.
    """
    if not waivers:
        return list(findings), 0, 0
    kept: list[Finding] = []
    waived = demoted = 0
    for f in findings:
        w = _match_waiver(f, waivers)
        if w is None:
            kept.append(f)
            continue
        target = _WAIVE_TARGET.get(str(w.get("severity", "off")).lower())
        waived += 1
        if detail_out is not None:
            detail_out.append({
                "code": f.code,
                "refs": list(f.refs or []),
                "action": ("dropped" if target is None
                           else f"demoted to {target.value}"),
                "reason": str(w.get("reason") or ""),
            })
        if target is None:            # severity: off -> drop
            continue
        demoted += 1
        kept.append(replace(f, severity=target))
    return kept, waived, demoted


# Metadata keys always surfaced in the header (with friendly labels + defaults).
_META_FIELDS: list[tuple[str, str, str]] = [
    ("passive_pin_ratio", "passive-pin ratio", "n/a"),
    ("no_erc_suppressed", "No-ERC suppressed", "0"),
    ("unnamed_net_count", "unnamed nets", "0"),
    ("frac_present", "frac coords present", "false"),
    ("config_waived", "config-waived", "0 (0 demoted)"),
]


def _fmt_pos(pos: tuple) -> str:
    """`(x,y)` with integer coordinates rendered without a trailing `.0`."""
    def r(v: float) -> str:
        iv = round(v)
        return str(int(iv)) if abs(v - iv) < 0.01 else f"{v:.1f}"
    return f"({r(pos[0])},{r(pos[1])})"


def _meta_dict(meta: dict | None) -> dict:
    meta = meta or {}
    out = {}
    for key, _label, default in _META_FIELDS:
        out[key] = meta.get(key, default)
    # preserve any extra metadata the caller passed
    for k, v in meta.items():
        out.setdefault(k, v)
    return out


def _render_text(findings: list[Finding], meta: dict) -> str:
    lines: list[str] = []
    lines.append("# metadata")
    for key, label, _default in _META_FIELDS:
        lines.append(f"  {label}: {meta[key]}")
    lines.append(f"# findings ({len(findings)})")
    if not findings:
        lines.append("  (none)")
    ordered = sorted(findings, key=lambda f: -_SEV_RANK.get(f.severity, 0))
    for f in ordered:
        sev = f.severity.value.upper()
        ref = f" {list(f.refs)}" if f.refs else ""
        at = f" @ {_fmt_pos(f.pos)}" if f.pos is not None else ""
        lines.append(f"  {sev} [{f.code}] {f.message}{ref}{at}")
    return "\n".join(lines)


def _render_json(findings: list[Finding], meta: dict) -> str:
    from .model import SCHEMA_VERSION  # local import avoids any import cycle
    payload = {
        "schema_version": SCHEMA_VERSION,
        "metadata": meta,
        "findings": [_finding_json(f) for f in findings],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def _finding_json(f: Finding) -> dict:
    # Optional keys are emitted only when present so findings without them
    # keep their historical shape (json turns tuples into arrays).
    d: dict = {
        "code": f.code,
        "severity": f.severity.value,
        "message": f.message,
        "refs": list(f.refs),
    }
    if f.pos is not None:
        d["pos"] = list(f.pos)
    if f.anchors:
        d["anchors"] = list(f.anchors)
    for key in ("detector", "confidence", "rule_version", "fingerprint",
                "remediation", "status"):
        v = getattr(f, key)
        if v is not None:
            d[key] = v
    if f.evidence:
        d["evidence"] = dict(f.evidence)
    if f.fix_params:
        d["fix_params"] = dict(f.fix_params)
    return d


def finding_from_json(d: dict) -> Finding:
    """Rebuild a :class:`Finding` from its :func:`_finding_json` dict.

    Round-trip partner for the findings JSON export (``review report`` re-reads
    a findings file to re-render it in another format). Unknown keys are
    ignored (consumers must tolerate additive schema growth).
    """
    return Finding(
        code=str(d.get("code", "")),
        severity=Severity(d.get("severity", "info")),
        message=str(d.get("message", "")),
        refs=list(d.get("refs", [])),
        pos=tuple(d["pos"]) if d.get("pos") is not None else None,
        anchors=list(d.get("anchors", [])),
        detector=d.get("detector"),
        confidence=d.get("confidence"),
        evidence=d.get("evidence"),
        rule_version=d.get("rule_version"),
        fingerprint=d.get("fingerprint"),
        remediation=d.get("remediation"),
        fix_params=d.get("fix_params"),
        status=d.get("status"),
    )


# SARIF severity levels (GitHub code scanning): error / warning / note.
_SARIF_LEVEL = {
    Severity.CRITICAL: "error",
    Severity.ERROR: "error",
    Severity.WARNING: "warning",
    Severity.NOTE: "note",
    Severity.INFO: "note",
}


def _render_sarif(findings: list[Finding], meta: dict, source: str | None) -> str:
    """SARIF 2.1.0 for GitHub code scanning / other SARIF consumers.

    Schematic findings have no line numbers, so locations carry only the
    artifact URI; ``partialFingerprints`` keeps alert identity stable across
    runs for the same (code, message, refs) triple.
    """
    import hashlib

    from . import __version__
    from .model import SCHEMA_VERSION

    rules: dict[str, dict] = {}
    results = []
    for f in sorted(findings, key=lambda f: (-_SEV_RANK.get(f.severity, 0), f.code)):
        rules.setdefault(f.code, {
            "id": f.code,
            "shortDescription": {"text": f.code.replace("_", " ").title()},
        })
        fp = hashlib.sha256(
            "|".join([f.code, f.message, *map(str, f.refs)]).encode("utf-8")
        ).hexdigest()[:32]
        text = f.message
        if f.refs:
            text += " (refs: " + ", ".join(map(str, f.refs)) + ")"
        # v1 (message-based) stays for alert continuity; v2 is the wording-
        # immune review fingerprint and wins in consumers that know it.
        fps = {"akcliFinding/v1": fp}
        if f.fingerprint:
            fps["akcliFinding/v2"] = f.fingerprint
        result: dict = {
            "ruleId": f.code,
            "level": _SARIF_LEVEL.get(f.severity, "note"),
            "message": {"text": text},
            "partialFingerprints": fps,
        }
        if source:
            result["locations"] = [{
                "physicalLocation": {
                    "artifactLocation": {"uri": str(source).replace("\\", "/")}
                }
            }]
        # Position data rides in logicalLocations (the named entities) and a
        # tool-specific properties.akcli bag; it is deliberately kept OUT of the
        # partialFingerprints above so alert identity never churns when a check
        # starts (or stops) emitting coordinates.
        if f.anchors:
            result.setdefault("locations", [{}])
            result["locations"][0]["logicalLocations"] = [
                {"name": a.get("id", ""), "kind": a.get("kind", "")}
                for a in f.anchors
            ]
        if f.pos is not None or f.anchors or f.confidence or f.detector:
            akcli: dict = {}
            if f.pos is not None:
                akcli["pos"] = list(f.pos)
            if f.anchors:
                akcli["anchors"] = list(f.anchors)
            if f.confidence:
                akcli["confidence"] = f.confidence
            if f.detector:
                akcli["detector"] = f.detector
            result["properties"] = {"akcli": akcli}
        results.append(result)

    payload = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "akcli",
                "informationUri": "https://github.com/tipoLi5890/akcli",
                "version": __version__,
                "rules": list(rules.values()),
            }},
            "properties": {"schema_version": SCHEMA_VERSION, "metadata": meta},
            "results": results,
        }],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _xml_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _render_junit(findings: list[Finding], meta: dict, source: str | None) -> str:
    """JUnit XML for CI test reporters.

    Each WARNING+ finding is a failed testcase; NOTE/INFO become passed
    testcases carrying their text in ``system-out``; a clean run emits one
    passed "no findings" case so the suite is never empty.
    """
    cases: list[str] = []
    failures = 0
    ordered = sorted(findings, key=lambda f: (-_SEV_RANK.get(f.severity, 0), f.code))
    for f in ordered:
        name = _xml_escape(f.message[:200])
        cls = f"akcli.{_xml_escape(f.code)}"
        body = _xml_escape(f.message)
        if f.refs:
            body += "\nrefs: " + _xml_escape(", ".join(map(str, f.refs)))
        if _SEV_RANK.get(f.severity, 0) >= _SEV_RANK[Severity.WARNING]:
            failures += 1
            cases.append(
                f'    <testcase classname="{cls}" name="{name}">\n'
                f'      <failure message="{_xml_escape(f.severity.value)}">{body}</failure>\n'
                f"    </testcase>"
            )
        else:
            cases.append(
                f'    <testcase classname="{cls}" name="{name}">\n'
                f"      <system-out>{body}</system-out>\n"
                f"    </testcase>"
            )
    if not cases:
        cases.append('    <testcase classname="akcli" name="no findings"/>')

    suite_name = _xml_escape(source or "akcli-check")
    total = max(len(findings), 1)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuites tests="{total}" failures="{failures}">\n'
        f'  <testsuite name="{suite_name}" tests="{total}" failures="{failures}">\n'
        + "\n".join(cases)
        + "\n  </testsuite>\n</testsuites>\n"
    )


def _render_markdown(findings: list[Finding], meta: dict,
                     source: str | None) -> str:
    """GitHub-flavoured Markdown report: trust summary + one table row each.

    The trust summary counts findings by confidence: deterministic findings
    carry weight, heuristics are flagged for manual review — a reader should
    never mistake one for the other.
    """
    lines: list[str] = [f"# akcli findings — {source or 'report'}", ""]
    by_conf: dict[str, int] = {}
    for f in findings:
        by_conf[f.confidence or "unspecified"] = (
            by_conf.get(f.confidence or "unspecified", 0) + 1)
    lines.append(f"**{len(findings)} finding(s)**"
                 + (" — " + ", ".join(f"{k}: {v}" for k, v in
                                      sorted(by_conf.items()))
                    if findings else ""))
    lines.append("")
    if findings:
        lines.append("| severity | code | confidence | message | refs |")
        lines.append("|---|---|---|---|---|")
        ordered = sorted(findings,
                         key=lambda f: (-_SEV_RANK.get(f.severity, 0), f.code))
        for f in ordered:
            msg = f.message.replace("|", "\\|")
            refs = ", ".join(map(str, f.refs)).replace("|", "\\|")
            lines.append(f"| {f.severity.value.upper()} | `{f.code}` | "
                         f"{f.confidence or '-'} | {msg} | {refs} |")
        remediations = [(f.code, f.remediation) for f in ordered if f.remediation]
        if remediations:
            lines += ["", "## Remediation", ""]
            for code, rem in remediations:
                lines.append(f"- `{code}` — {rem}")
    else:
        lines.append("(none)")
    lines += ["", "## Metadata", ""]
    for key, label, _default in _META_FIELDS:
        lines.append(f"- {label}: {meta[key]}")
    return "\n".join(lines) + "\n"


def render(
    findings: list[Finding],
    fmt: str = "text",
    meta: dict | None = None,
    source: str | None = None,
) -> str:
    """Render findings as ``text``/``json`` (metadata header always present),
    ``sarif`` (GitHub code scanning), ``junit`` (CI test reporters) or
    ``markdown`` (human review summary with a trust rollup)."""
    m = _meta_dict(meta)
    if fmt == "json":
        return _render_json(findings, m)
    if fmt == "sarif":
        return _render_sarif(findings, m, source)
    if fmt == "junit":
        return _render_junit(findings, m, source)
    if fmt == "markdown":
        return _render_markdown(findings, m, source)
    return _render_text(findings, m)
