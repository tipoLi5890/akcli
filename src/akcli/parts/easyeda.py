"""Light EasyEDA metadata + 3D-availability lookup.

This is **not** a converter. Its only job is to enrich ``akcli jlc show`` with a few
EasyEDA-side facts that jlcsearch does not give us — most usefully *"is a 3D/STEP model
available for this part?"* — **without** fetching or converting any symbol/footprint/3D.
Metadata lookup only; no library conversion happens here.

Transport mirrors :mod:`.search` exactly: stdlib ``urllib`` only, an **injectable
``opener``** (any object exposing ``open(request, *, timeout=...)``) so tests run fully
offline, a bounded timeout + response size cap, an optional on-disk cache, and every
transport/HTTP failure mapped to :class:`EasyEdaError` (a raw traceback never escapes).

Caveats (baked in; the lookup must *never* break ``jlc show``)
-------------------------------------------------------------------------
* **Unofficial / undocumented.** The *documented* EasyEDA API is an in-editor JS
  scripting API and does **not** expose these URLs. The endpoints — especially the
  hard-coded STEP token :data:`_STEP_TOKEN` — can change without notice. Every caller
  treats a failure as "metadata unavailable" and degrades gracefully.
* **``success: false`` / empty ``result`` is the normal "not found" path** — :func:`lookup`
  returns ``None`` there, *not* an error.
* **Std vs Pro split.** These endpoints serve EasyEDA **Std** parts; some newer
  "Pro-only" parts will not resolve here (a known ecosystem gap).
* **No documented rate limit** — cache aggressively, keep request volume low.

This module re-implements **no** conversion logic; it only reads metadata and detects
whether a 3D model UUID is present.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

# Injectable delay hook (defaults to time.sleep); tests pass a recording stub.
SleepFn = Callable[[float], None]

# --- service + transport constants ------------------------------------------
# Unofficial EasyEDA *Std* editor backend (the same source easyeda2kicad reads).
COMPONENTS_URL = "https://easyeda.com/api/products/{lcsc}/components"
# 3D STEP is keyed by the 3D-model UUID, NOT the LCSC id. The leading token is
# hard-coded upstream and is the single most fragile part of this module.
_STEP_TOKEN = "qAxj6KHrDKw4blvCG8QJPs7Y"
STEP_URL = "https://modules.easyeda.com/" + _STEP_TOKEN + "/{uuid}"

# Browser-like headers — a default Python/urllib UA tends to get throttled/blocked.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_HEADERS = {
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "User-Agent": _USER_AGENT,
    "Referer": "https://easyeda.com/",
}

DEFAULT_TIMEOUT = 15.0                 # seconds; bounded so a hung host can't wedge us
MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # hard cap on a single response body
CACHE_TTL_SECONDS = 3600               # on-disk cache freshness window

MAX_ATTEMPTS = 3                       # total tries per request (1 + up to 2 retries)
BACKOFF_BASE = 0.5                     # seconds; doubles per attempt, plus jitter
BACKOFF_CAP = 8.0                      # upper bound on the computed backoff delay
RETRY_AFTER_CAP = 30.0                 # never honor a Retry-After longer than this
STALE_EVICT_MULTIPLIER = 7             # cache entries older than 7x TTL get evicted


class EasyEdaError(Exception):
    """An EasyEDA metadata request failed at the transport/HTTP/decoding layer.

    Covers an unreachable host, a timeout, a non-2xx status, an oversized body, or
    undecodable JSON. A *missing part* (``success: false`` / empty result) is **not**
    an error — :func:`lookup` returns ``None`` for that. The CLI maps this onto the
    external-tool exit code and never leaks a traceback.

    ``kind`` classifies the failure (``http`` / ``network`` / ``timeout`` /
    ``size`` / ``decode``) and ``retryable`` marks the transient subset (URLError,
    timeout, HTTP 429/5xx) that the transport retries with backoff before this
    error escapes. ``retry_after`` carries the server's numeric Retry-After
    seconds when one was sent.
    """

    def __init__(self, message: str, *, kind: str = "network",
                 retryable: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass
class EasyEdaInfo:
    """A small bundle of EasyEDA-derived facts for one LCSC part."""

    lcsc: str
    title: str | None = None
    manufacturer: str | None = None
    mpn: str | None = None
    datasheet: str | None = None
    package: str | None = None
    has_3d: bool = False
    model_uuid: str | None = None
    source: str = "easyeda-std"        # provenance label

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# helpers (pure / offline)
# --------------------------------------------------------------------------- #
def _norm_lcsc(value: object) -> str:
    """Normalize to canonical ``C<digits>`` form (EasyEDA wants the ``C`` prefix)."""
    s = str(value or "").strip().upper()
    if not s:
        return ""
    digits = s[1:] if s.startswith("C") else s
    return ("C" + digits) if digits else ""


def _as_dict(value: object) -> dict:
    """Return ``value`` if it is a dict (possibly JSON-encoded), else ``{}``."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _first_str(*values: object) -> str | None:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _find_model_uuid(package_detail: dict) -> str | None:
    """Extract the 3D-model UUID from a footprint's ``packageDetail.dataStr``.

    The 3D model lives in a ``SVGNODE`` shape primitive whose payload is a JSON object
    carrying ``attrs.uuid``. EasyEDA serializes the footprint either as a dict with a
    ``shape`` list of ``"<TYPE>~<json>"`` strings, or (older) with already-parsed
    objects. We scan defensively and return the first uuid we find, or ``None``.
    """
    data_str = _as_dict(package_detail.get("dataStr"))
    shapes = data_str.get("shape")
    if not isinstance(shapes, list):
        return None
    for shape in shapes:
        # New style: "SVGNODE~{json...}"
        if isinstance(shape, str):
            if not shape.startswith("SVGNODE"):
                continue
            _, _, payload = shape.partition("~")
            node = _as_dict(payload)
        elif isinstance(shape, dict):
            node = shape
        else:
            continue
        attrs = _as_dict(node.get("attrs"))
        uuid = attrs.get("uuid")
        if isinstance(uuid, str) and uuid.strip():
            return uuid.strip()
    return None


def _build_info(lcsc: str, result: dict) -> EasyEdaInfo:
    """Map a ``/components`` ``result`` payload onto an :class:`EasyEdaInfo`."""
    # symbol head c_para (manufacturer / mpn / datasheet link / package)
    sym_data = _as_dict(result.get("dataStr"))
    sym_head = _as_dict(sym_data.get("head"))
    sym_para = _as_dict(sym_head.get("c_para"))

    # footprint head c_para (authoritative package name)
    pkg_detail = _as_dict(result.get("packageDetail"))
    pkg_data = _as_dict(pkg_detail.get("dataStr"))
    pkg_head = _as_dict(pkg_data.get("head"))
    pkg_para = _as_dict(pkg_head.get("c_para"))

    manufacturer = _first_str(sym_para.get("Manufacturer"),
                              pkg_para.get("Manufacturer"))
    mpn = _first_str(sym_para.get("Manufacturer Part"), sym_para.get("Manufacturer Part Number"),
                     pkg_para.get("Manufacturer Part"))
    # the szlcsc datasheet link lives in EITHER head's c_para depending on how
    # the part was authored (TCRT5000 carries it only on the footprint side)
    datasheet = _first_str(sym_para.get("link"), sym_para.get("Datasheet"),
                           pkg_para.get("link"), pkg_para.get("Datasheet"),
                           result.get("datasheet"))
    package = _first_str(pkg_para.get("package"), sym_para.get("package"), result.get("package"))
    title = _first_str(result.get("title"), result.get("description"))

    model_uuid = _find_model_uuid(pkg_detail)
    return EasyEdaInfo(
        lcsc=lcsc,
        title=title,
        manufacturer=manufacturer,
        mpn=mpn,
        datasheet=datasheet,
        package=package,
        has_3d=bool(model_uuid),
        model_uuid=model_uuid,
    )


def _default_opener() -> urllib.request.OpenerDirector:
    """Build the real urllib opener. Tests monkeypatch this to inject a fake."""
    return urllib.request.build_opener()


# --------------------------------------------------------------------------- #
# on-disk cache (best-effort, per-lcsc JSON; same shape as parts/search.py)
# --------------------------------------------------------------------------- #
def _cache_path(cache_dir: str | Path, url: str) -> Path:
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return Path(cache_dir) / f"easyeda_{key}.json"


def _cache_read(cache_dir: str | Path | None, url: str, ttl: float | None) -> object | None:
    if not cache_dir:
        return None
    p = _cache_path(cache_dir, url)
    try:
        st = p.stat()
    except OSError:
        return None
    if ttl is not None and (time.time() - st.st_mtime) > ttl:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cache_write(cache_dir: str | Path | None, url: str, payload: object,
                 *, ttl: float | None = None) -> None:
    """Atomically persist ``payload``, then opportunistically evict old siblings.

    tempfile + ``os.replace`` so a crashed or parallel writer can never leave a
    truncated JSON file behind for a later read to trip over.
    """
    if not cache_dir:
        return
    try:
        d = Path(cache_dir)
        d.mkdir(parents=True, exist_ok=True)
        target = _cache_path(cache_dir, url)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=target.stem + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(payload))
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        _cache_evict(d, ttl)
    except OSError:
        pass  # cache is best-effort; never fail a lookup because of it


def _cache_evict(d: Path, ttl: float | None) -> None:
    """Drop sibling entries older than ``STALE_EVICT_MULTIPLIER`` x TTL."""
    if not ttl or ttl <= 0:
        return
    cutoff = time.time() - STALE_EVICT_MULTIPLIER * ttl
    try:
        entries = list(d.glob("easyeda_*.json"))
    except OSError:
        return
    for p in entries:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            continue


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def _maybe_gunzip(raw: bytes) -> bytes:
    """Transparently decompress a gzip body (we advertise ``Accept-Encoding: gzip``)."""
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError as e:  # pragma: no cover - defensive
            raise EasyEdaError(f"easyeda gzip decode failed: {e}") from e
    return raw


def _parse_retry_after(err: object) -> float | None:
    """Numeric ``Retry-After`` seconds from an HTTPError, or ``None`` (dates ignored)."""
    get = getattr(getattr(err, "headers", None), "get", None)
    raw = get("Retry-After") if callable(get) else None
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (ValueError, TypeError):
        return None
    return seconds if seconds >= 0 else None


def _retry_delay(attempt: int, retry_after: float | None) -> float:
    """Seconds before retry ``attempt + 1``: Retry-After (capped) wins, else
    exponential backoff with jitter so parallel clients desynchronize."""
    if retry_after is not None:
        return min(retry_after, RETRY_AFTER_CAP)
    base = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt))
    return base + random.uniform(0, base)


def _fetch_json_once(url: str, *, opener: urllib.request.OpenerDirector,
                     timeout: float) -> object:
    """GET ``url`` once and decode JSON, mapping every failure to :class:`EasyEdaError`."""
    req = urllib.request.Request(url, headers=dict(_HEADERS))
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:      # subclass of URLError — must come first
        reason = getattr(e, "reason", "") or ""
        raise EasyEdaError(
            f"easyeda returned HTTP {e.code} {reason}".strip(), kind="http",
            retryable=(e.code == 429 or 500 <= e.code < 600),
            retry_after=_parse_retry_after(e)) from e
    except urllib.error.URLError as e:
        raise EasyEdaError(f"could not reach easyeda: {getattr(e, 'reason', e)}",
                           kind="network", retryable=True) from e
    except TimeoutError as e:                # socket.timeout is an alias since 3.10
        raise EasyEdaError(f"easyeda request timed out after {timeout}s",
                           kind="timeout", retryable=True) from e

    try:
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError) as e:
        raise EasyEdaError(f"easyeda read failed: {e}",
                           kind="network", retryable=True) from e
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - defensive
                pass

    if len(raw) > MAX_RESPONSE_BYTES:
        raise EasyEdaError("easyeda response exceeded the size cap", kind="size")
    raw = _maybe_gunzip(raw)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise EasyEdaError(f"easyeda returned invalid JSON: {e}", kind="decode") from e


def _fetch_json(url: str, *, opener: urllib.request.OpenerDirector, timeout: float,
                sleep: SleepFn | None = None, attempts: int = MAX_ATTEMPTS) -> object:
    """GET ``url`` and decode JSON, retrying transient failures.

    Only ``retryable`` errors (unreachable host, timeout, HTTP 429/5xx) are
    retried, up to ``attempts`` total tries with exponential backoff + jitter
    (a numeric Retry-After wins). ``sleep`` is injectable so offline tests can
    assert retry behavior without waiting.
    """
    if sleep is None:
        sleep = time.sleep
    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        try:
            return _fetch_json_once(url, opener=opener, timeout=timeout)
        except EasyEdaError as exc:
            if not exc.retryable or attempt >= attempts - 1:
                raise
            sleep(_retry_delay(attempt, exc.retry_after))
    raise EasyEdaError("unreachable")  # pragma: no cover - loop returns or raises


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def lookup(
    lcsc_id: str,
    *,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    cache_dir: str | Path | None = None,
    cache_ttl: float | None = CACHE_TTL_SECONDS,
    sleep: SleepFn | None = None,
) -> EasyEdaInfo | None:
    """Return :class:`EasyEdaInfo` for ``lcsc_id``, or ``None`` when the part is absent.

    A part EasyEDA does not know (``success: false`` or an empty ``result``) yields
    ``None`` — that is the normal "not found" path, **not** an error. A
    network/HTTP/decoding failure raises :class:`EasyEdaError`. ``opener`` injects a
    transport (defaults to a real urllib opener) so tests run offline; pass
    ``cache_dir`` to enable a short on-disk cache keyed by request URL.

    This performs a single ``/components`` request; 3D availability is inferred from
    the presence of the model UUID (no second request to the STEP URL).
    """
    lcsc = _norm_lcsc(lcsc_id)
    if not lcsc:
        return None
    if opener is None:
        opener = _default_opener()

    url = COMPONENTS_URL.format(lcsc=urllib.parse.quote(lcsc, safe=""))
    payload = _cache_read(cache_dir, url, cache_ttl)
    if payload is None:
        payload = _fetch_json(url, opener=opener, timeout=timeout, sleep=sleep)
        _cache_write(cache_dir, url, payload, ttl=cache_ttl)

    if not isinstance(payload, dict):
        return None
    if payload.get("success") is False:
        return None
    result = payload.get("result")
    if not isinstance(result, dict) or not result:
        return None
    return _build_info(lcsc, result)
