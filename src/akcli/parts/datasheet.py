"""BOM → datasheet resolution/download bridge (``akcli jlc datasheet``).

The JLCPCB mirror catalog (jlcsearch) never carries datasheet URLs, and
lcsc.com gates plain-HTTP downloads behind a browser check — but the EasyEDA
``/components`` record for an LCSC part usually embeds the szlcsc-hosted PDF
link in a ``head.c_para.link`` field (on the symbol OR the footprint side;
:func:`~.easyeda.lookup` checks both). Those ``atta.szlcsc.com`` PDFs download
cleanly with browser-like headers, so the pipeline here is:

    C-number ── easyeda.lookup ──> DatasheetInfo.url ── fetch_pdf ──> .pdf

``fetch_pdf`` validates the ``%PDF`` magic before keeping anything: several
hosts answer a challenge/viewer HTML page with status 200, and saving that as
``.pdf`` is exactly the silent failure this module exists to prevent. Files
land in :func:`default_dir` (``AKCLI_DATASHEET_DIR`` overrides) named
``C<digits>_<MPN>.pdf`` and act as their own cache — an existing file is
never re-downloaded unless ``force=True``.

Not every EasyEDA ``link`` is a document: real-world records also carry
product pages (``item.szlcsc.com`` — a JS shell to plain HTTP), bot-gated
viewer paths (mouser), and even bare search-engine queries. :func:`resolve`
classifies instead of pretending: direct ``.pdf`` → ``resolved`` (fetchable),
product/viewer page → ``page-link`` (URL surfaced for a browser-grade
fetcher), search-engine junk → ``no-link`` with the LCSC product-page hint.

Network errors raise :class:`~.easyeda.EasyEdaError`; the CLI maps them to
exit 7 like the rest of the ``jlc`` family. This module is import-isolated
with the other networked code under ``akcli.parts``.
"""

from __future__ import annotations

import os
import re
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import easyeda

if TYPE_CHECKING:
    from ..model import Schematic
    from .easyeda import EasyEdaInfo

__all__ = ["DatasheetRow", "resolve", "fetch_pdf", "default_dir",
           "rows_for_schematic", "pdf_filename"]

MAX_PDF_BYTES = 64 * 1024 * 1024      # hard cap on a downloaded datasheet
_CHUNK = 256 * 1024

# Browser-like headers: atta.szlcsc.com serves plain urllib fine, but a
# default Python UA invites throttling on the resolver side already, so the
# download reuses the same disguise the EasyEDA transport ships.
_HEADERS = dict(easyeda._HEADERS)
_HEADERS["Accept"] = "application/pdf, */*"

_CNUM_RE = re.compile(r"^[Cc](\d{2,})$")
_SAFE_RE = re.compile(r"[^A-Za-z0-9._+-]+")
_PDF_URL_RE = re.compile(r"\.pdf(?:[?#]|$)", re.I)
_SEARCH_URL_RE = re.compile(
    r"(?:[?&][qk]=|/search\b|\bso\.szlcsc\.com|\bbing\.com|"
    r"\bgoogle\.[a-z.]+/search|/global\.html)", re.I)


@dataclass
class DatasheetRow:
    """One resolution/download outcome — a single part or one BOM line."""

    lcsc: str | None = None
    refs: list[str] | None = None      # BOM mode: the designators on this line
    mpn: str | None = None
    manufacturer: str | None = None
    url: str | None = None
    path: str | None = None            # local file after --fetch
    # resolved | fetched | cached | page-link | no-link | not-found |
    # no-lcsc | fetch-failed
    status: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "lcsc": self.lcsc, "refs": self.refs, "mpn": self.mpn,
            "manufacturer": self.manufacturer, "url": self.url,
            "path": self.path, "status": self.status, "note": self.note,
        }


def default_dir() -> Path:
    """Where fetched datasheets live: ``AKCLI_DATASHEET_DIR`` or the cache tree.

    Unlike the JSON caches this is a *deliverable* directory (the PDF is what
    the user asked for), so there is no "off" switch — pointing the env var
    somewhere relocates it, and ``--out`` overrides per call.
    """
    env = os.environ.get("AKCLI_DATASHEET_DIR")
    if env and env.strip():
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "akcli" / "datasheets"


def pdf_filename(lcsc: str | None, mpn: str | None) -> str:
    """``C2984661_TCRT5000.pdf`` — readable, unique, filesystem-safe."""
    parts = [p for p in (lcsc, mpn) if p]
    stem = _SAFE_RE.sub("-", "_".join(parts)).strip("-_") or "datasheet"
    return stem + ".pdf"


def resolve(lcsc: str, *,
            lookup: Callable[[str], "EasyEdaInfo | None"] | None = None,
            cache_dir: str | Path | None = None,
            timeout: float = easyeda.DEFAULT_TIMEOUT) -> DatasheetRow:
    """Resolve one C-number to a :class:`DatasheetRow` (no download).

    ``lookup`` is injectable for offline tests; the default resolves at call
    time (so monkeypatching :func:`easyeda.lookup` works) and shares the
    EasyEDA on-disk JSON cache when ``cache_dir`` is given.
    """
    if lookup is None:
        lookup = lambda c: easyeda.lookup(  # noqa: E731
            c, cache_dir=cache_dir, timeout=timeout)
    m = _CNUM_RE.match(lcsc.strip() if lcsc else "")
    canon = ("C" + m.group(1)) if m else (lcsc or "").strip()
    info = lookup(canon)
    if info is None:
        return DatasheetRow(lcsc=canon, status="not-found",
                            note="EasyEDA has no record for this C-number")
    row = DatasheetRow(lcsc=canon, mpn=info.mpn,
                       manufacturer=info.manufacturer, url=info.datasheet)
    url = (info.datasheet or "").strip()
    browser_hint = (f"try https://www.lcsc.com/product-detail/{canon}.html "
                    "in a browser (direct fetch is bot-gated)")
    if not url.lower().startswith(("http://", "https://")):
        row.url = None
        row.status = "no-link"
        row.note = "no datasheet link in the EasyEDA record — " + browser_hint
    elif _SEARCH_URL_RE.search(url):
        # EasyEDA data sometimes carries a search-engine query instead of a
        # document — worthless as-is, and occasionally for the WRONG part
        row.url = None
        row.status = "no-link"
        row.note = "EasyEDA carries a search link, not a datasheet — " + browser_hint
    elif _PDF_URL_RE.search(url):
        row.status = "resolved"
    else:
        # a product/viewer page (item.szlcsc.com, mouser, ...): the URL is
        # real information, but these hosts are JS-rendered or bot-gated —
        # a browser-grade fetcher has to take it from here
        row.status = "page-link"
        row.note = ("not a direct PDF — open the URL in a browser/WebFetch; "
                    "plain fetch gets a challenge or an empty JS shell")
    return row


def fetch_pdf(url: str, dest: Path, *,
              opener: urllib.request.OpenerDirector | None = None,
              timeout: float = 30.0, force: bool = False,
              max_bytes: int = MAX_PDF_BYTES) -> tuple[Path, bool]:
    """Download ``url`` to ``dest``; returns ``(path, downloaded)``.

    An existing ``dest`` short-circuits (``downloaded=False``) unless
    ``force``. The body must start with ``%PDF`` — an HTML challenge page is
    rejected with a ``decode``-kind :class:`~.easyeda.EasyEdaError` and no
    file is left behind. The write is atomic (tempfile + ``os.replace``).
    """
    dest = Path(dest)
    if dest.exists() and not force:
        return dest, False
    if opener is None:
        opener = easyeda._default_opener()
    req = urllib.request.Request(url, headers=_HEADERS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    total = 0
    try:
        try:
            resp = opener.open(req, timeout=timeout)
        except OSError as exc:  # URLError subclasses OSError
            raise easyeda.EasyEdaError(
                f"datasheet download failed: {exc}", kind="network",
                retryable=True) from exc
        with resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status and status >= 400:
                raise easyeda.EasyEdaError(
                    f"datasheet download failed: HTTP {status} for {url}",
                    kind="http", retryable=status in (429,) or status >= 500)
            first = resp.read(8)
            if not first.startswith(b"%PDF"):
                raise easyeda.EasyEdaError(
                    "not a PDF (the server answered with a web page — it "
                    f"likely gates direct downloads); open {url} in a "
                    "browser instead", kind="decode")
            os.write(fd, first)
            total = len(first)
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise easyeda.EasyEdaError(
                        f"datasheet exceeds {max_bytes // (1024*1024)} MB "
                        f"cap: {url}", kind="size")
                os.write(fd, chunk)
        os.close(fd)
        fd = -1
        os.replace(tmp, dest)
        return dest, True
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)


def rows_for_schematic(sch: "Schematic") -> list[DatasheetRow]:
    """Seed one unresolved row per BOM line, keyed by explicit LCSC ids.

    Lines carrying only an MPN (or nothing) are surfaced as ``no-lcsc`` —
    resolving them would mean a catalog search per line; ``jlc bom
    --suggest/--fix`` pins C-numbers first, or pass ``--resolve-mpn`` to
    this command to exact-match the catalog inline (one search per
    distinct MPN).
    """
    from .bom_jlc import collect_lines
    rows: list[DatasheetRow] = []
    for line in collect_lines(sch):
        if line.lcsc:
            rows.append(DatasheetRow(lcsc=line.lcsc, refs=line.refs,
                                     mpn=line.mpn))
        else:
            rows.append(DatasheetRow(
                refs=line.refs, mpn=line.mpn, status="no-lcsc",
                note=("no LCSC parameter — pin a C-number first "
                      "(jlc bom --suggest/--fix) or pass --resolve-mpn")))
    return rows
