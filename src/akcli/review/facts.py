"""Datasheet facts store (review M4): audited, PDF-pinned component facts.

Layout (per project)::

    datasheets/
      C123456_TPS61023.pdf          # fetched by `akcli jlc datasheet`
      extracted/<MPN>.json          # ONE audited facts file per MPN

Every fact is pinned to its source PDF by **sha256 + page** (optionally a
verbatim quote), so a ``datasheet_backed`` finding always traces to the exact
document that justified it. The store is audit-first: ``manual`` entry is a
first-class extraction method — the discipline lives in :func:`verify`
(schema conformance, PDF presence, sha256 staleness, page bounds, quote
presence via the optional ``pdftotext`` driver), not in how the numbers were
obtained. No fact, no ``datasheet_backed`` claim; the reader returns ``None``
rather than guess.

Standard fact keys consumed by detectors today: ``vref`` (regulator feedback
reference, V), ``load_capacitance`` (crystal CL, F), ``abs_max_io``
(pin absolute maximum, V), ``i_load`` (fuse continuous current, A), and the
junction trio ``theta_ja``/``power_dissipation``/``t_j_max`` (pcb.thermal).
Any ``[a-z][a-z0-9_]*`` key is schema-legal so a
facts file can carry more than the detectors consume yet.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..model import Component
from ..report import Finding, Severity

FACTS_VERSION = "1.0"
EXTRACTION_METHODS = ("manual", "pdftotext", "llm")

# Component parameters that carry the exact MPN a facts file is keyed by.
_MPN_PARAMS: tuple[str, ...] = (
    "MPN", "Mpn", "mpn", "Manufacturer Part", "Manufacturer_Part_Number",
    "Part Number", "PartNumber",
)


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FactValue:
    """One audited number with its provenance (see ``datasheet-facts`` schema)."""

    key: str
    unit: str
    page: int
    value: float | None = None
    min: float | None = None
    typ: float | None = None
    max: float | None = None
    quote: str | None = None
    conditions: str | None = None
    sha256: str = ""
    pdf: str | None = None

    def best(self) -> float | None:
        """The single number a comparison should use: value → typ → mid → bound."""
        if self.value is not None:
            return self.value
        if self.typ is not None:
            return self.typ
        if self.min is not None and self.max is not None:
            return (self.min + self.max) / 2.0
        return self.min if self.min is not None else self.max

    def evidence(self) -> dict:
        """The ``evidence.datasheet`` block this fact justifies."""
        d: dict = {"sha256": self.sha256, "page": self.page}
        if self.quote:
            d["quote"] = self.quote
        return d


@dataclass
class Facts:
    """All audited facts for one MPN."""

    mpn: str
    sha256: str
    pdf: str | None = None
    extraction_method: str = "manual"
    quality: str = "unverified"
    package: str | None = None
    values: dict[str, FactValue] = field(default_factory=dict)
    path: Path | None = None

    def get(self, key: str) -> FactValue | None:
        return self.values.get(key)


@dataclass
class FactsStore:
    """Every ``extracted/*.json`` under one datasheets dir, indexed by MPN."""

    root: Path | None = None
    by_mpn: dict[str, Facts] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def lookup(self, mpn: str | None) -> Facts | None:
        if not mpn:
            return None
        return self.by_mpn.get(mpn.strip().upper())

    def lookup_component(self, comp: Component) -> Facts | None:
        return self.lookup(component_mpn(comp))


def component_mpn(comp: Component) -> str | None:
    """The component's exact MPN: a parameter first, else a part-number Value.

    The Value fallback accepts only digit-bearing strings (``TPS61023`` yes,
    ``100n`` maps to no facts file anyway) — a wrong guess costs nothing
    because matching only succeeds when a facts file exists for it.
    """
    params = comp.parameters or {}
    for key in _MPN_PARAMS:
        v = (params.get(key) or "").strip()
        if v:
            return v
    v = (comp.value or "").strip()
    if v and any(ch.isdigit() for ch in v) and len(v) >= 4:
        return v
    return None


# --------------------------------------------------------------------------- #
# load / save
# --------------------------------------------------------------------------- #
def sanitize_mpn(mpn: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", mpn.strip())


def extracted_dir(root: Path) -> Path:
    return Path(root) / "extracted"


def facts_path(root: Path, mpn: str) -> Path:
    return extracted_dir(root) / f"{sanitize_mpn(mpn)}.json"


def _facts_from_doc(doc: dict, path: Path | None) -> Facts:
    src = doc.get("source") or {}
    f = Facts(
        mpn=str(doc.get("mpn", "")),
        sha256=str(src.get("sha256", "")),
        pdf=src.get("pdf"),
        extraction_method=str(src.get("extraction_method", "manual")),
        quality=str(src.get("quality", "unverified")),
        package=doc.get("package"),
        path=path,
    )
    for key, raw in (doc.get("facts") or {}).items():
        if not isinstance(raw, dict):
            continue
        f.values[key] = FactValue(
            key=key, unit=str(raw.get("unit", "")),
            page=int(raw.get("page", 0) or 0),
            value=raw.get("value"), min=raw.get("min"),
            typ=raw.get("typ"), max=raw.get("max"),
            quote=raw.get("quote"), conditions=raw.get("conditions"),
            sha256=f.sha256, pdf=f.pdf)
    return f


def load_store(root: Path | str | None) -> FactsStore:
    """Read ``<root>/extracted/*.json``; unreadable files land in ``errors``."""
    store = FactsStore(root=Path(root) if root else None)
    if root is None:
        return store
    exdir = extracted_dir(Path(root))
    if not exdir.is_dir():
        return store
    for p in sorted(exdir.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            facts = _facts_from_doc(doc, p)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            store.errors.append(f"{p.name}: {exc}")
            continue
        if facts.mpn:
            store.by_mpn[facts.mpn.upper()] = facts
        else:
            store.errors.append(f"{p.name}: missing mpn")
    return store


def facts_to_doc(f: Facts) -> dict:
    doc: dict = {
        "facts_version": FACTS_VERSION,
        "mpn": f.mpn,
        "variant": None,
        "package": f.package,
        "source": {
            **({"pdf": f.pdf} if f.pdf else {}),
            "sha256": f.sha256,
            "extraction_method": f.extraction_method,
            "quality": f.quality,
        },
        "facts": {},
    }
    for key in sorted(f.values):
        v = f.values[key]
        entry: dict = {}
        for slot in ("value", "min", "typ", "max"):
            num = getattr(v, slot)
            if num is not None:
                entry[slot] = num
        entry["unit"] = v.unit
        entry["page"] = v.page
        if v.quote:
            entry["quote"] = v.quote
        if v.conditions:
            entry["conditions"] = v.conditions
        doc["facts"][key] = entry
    return doc


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# CLI value syntax: KEY=VALUE[UNIT]@PAGE  (vref=0.6V@5, load_capacitance=12pF@3)
# --------------------------------------------------------------------------- #
_KEY_RX = re.compile(r"^[a-z][a-z0-9_]*$")
_SET_RX = re.compile(
    r"^([a-z][a-z0-9_]*)=([0-9]+(?:\.[0-9]+)?)([a-zA-ZµΩ%]*)@p?([0-9]+)$")
_ENG = {"G": 1e9, "M": 1e6, "k": 1e3, "m": 1e-3, "u": 1e-6, "µ": 1e-6,
        "n": 1e-9, "p": 1e-12, "f": 1e-15}


def parse_set(expr: str) -> tuple[str, float, str, int]:
    """``(key, base_value, unit, page)`` from ``KEY=VALUE[UNIT]@PAGE``.

    ``12pF@3`` → 12e-12 F; ``0.6V@5`` → 0.6 V; ``6V@2`` → 6.0 V. Raises
    ``ValueError`` with the expected shape on any mismatch — never guesses.
    """
    m = _SET_RX.match(expr.strip())
    if not m:
        raise ValueError(
            f"cannot parse {expr!r} (expected KEY=VALUE[UNIT]@PAGE, "
            "e.g. vref=0.6V@5 or load_capacitance=12pF@3)")
    key, head, suffix, page = m.groups()
    base = float(head)
    if suffix and suffix[0] in _ENG and len(suffix) > 1:
        base *= _ENG[suffix[0]]
        unit = suffix[1:]
    elif suffix in _ENG:
        base *= _ENG[suffix]
        unit = ""
    else:
        unit = suffix
    return key, base, unit, int(page)


# --------------------------------------------------------------------------- #
# verification (runtime, zero-dep — jsonschema mirrors this in tests)
# --------------------------------------------------------------------------- #
_SHA_RX = re.compile(r"^[0-9a-f]{64}$")


def verify_facts(facts: Facts, root: Path) -> list[Finding]:
    """Audit one facts file: structure, PDF binding, staleness, quotes.

    Quote checks run through the optional ``pdftotext`` driver; when the tool
    is absent every quoted fact yields ONE aggregated NOTE (the check did not
    run — saying so beats pretending).
    """
    from ..drivers import pdftotext

    out: list[Finding] = []
    name = facts.path.name if facts.path else facts.mpn

    def _f(code: str, sev: Severity, msg: str) -> None:
        out.append(Finding(code=code, severity=sev, message=f"{name}: {msg}",
                           refs=[facts.mpn] if facts.mpn else [],
                           detector="review.facts",
                           confidence="deterministic"))

    if not facts.mpn:
        _f("FACTS_SCHEMA_INVALID", Severity.ERROR, "missing mpn")
    if not _SHA_RX.match(facts.sha256 or ""):
        _f("FACTS_SCHEMA_INVALID", Severity.ERROR,
           "source.sha256 is not a 64-hex digest")
    if facts.extraction_method not in EXTRACTION_METHODS:
        _f("FACTS_SCHEMA_INVALID", Severity.ERROR,
           f"extraction_method {facts.extraction_method!r} not in "
           f"{EXTRACTION_METHODS}")
    if not facts.values:
        _f("FACTS_EMPTY", Severity.NOTE, "no facts recorded yet")
    for key, v in sorted(facts.values.items()):
        if not _KEY_RX.match(key):
            _f("FACTS_SCHEMA_INVALID", Severity.ERROR,
               f"fact key {key!r} is not lower_snake_case")
        if v.page < 1:
            _f("FACTS_SCHEMA_INVALID", Severity.ERROR,
               f"fact {key!r}: page must be >= 1")
        if v.best() is None:
            _f("FACTS_SCHEMA_INVALID", Severity.ERROR,
               f"fact {key!r}: needs one of value/min/typ/max")

    pdf_path = Path(root) / facts.pdf if facts.pdf else None
    if pdf_path is None:
        _f("FACTS_PDF_MISSING", Severity.WARNING,
           "no source.pdf recorded — staleness and quotes are unverifiable")
    elif not pdf_path.is_file():
        _f("FACTS_PDF_MISSING", Severity.WARNING,
           f"source.pdf {facts.pdf!r} not found under {root}")
    else:
        actual = sha256_file(pdf_path)
        if _SHA_RX.match(facts.sha256 or "") and actual != facts.sha256:
            _f("FACTS_STALE", Severity.ERROR,
               f"PDF {facts.pdf!r} changed since extraction "
               f"(sha256 {actual[:12]}… != recorded {facts.sha256[:12]}…) — "
               "re-verify every fact against the new document")
        else:
            quoted = [(k, v) for k, v in sorted(facts.values.items()) if v.quote]
            if quoted and not pdftotext.available():
                _f("FACTS_QUOTE_UNVERIFIED", Severity.NOTE,
                   f"{len(quoted)} quoted fact(s) not checked — install "
                   "pdftotext (poppler) to verify quotes against the PDF")
            else:
                for key, v in quoted:
                    hit = pdftotext.quote_present(str(pdf_path), v.page, v.quote)
                    if hit is None:
                        _f("FACTS_QUOTE_UNVERIFIED", Severity.NOTE,
                           f"fact {key!r}: quote check could not run")
                    elif not hit:
                        _f("FACTS_QUOTE_MISMATCH", Severity.WARNING,
                           f"fact {key!r}: quote not found on page {v.page} "
                           "of the recorded PDF")
    return out
