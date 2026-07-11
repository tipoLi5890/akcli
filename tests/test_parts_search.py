"""Offline tests for the jlcsearch client (``parts/search.py``) and the ``jlc`` CLI.

NO real network: every request is served by a ``FakeOpener`` exposing
``open(request, timeout=...)`` (the same surface as ``urllib.request.OpenerDirector``)
and returning canned jlcsearch JSON captured from the live service
(``GET /components/list.json?search=…``). The CLI path is driven offline by
monkeypatching ``parts.search._default_opener``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error

import pytest

from altium_kicad_cli.cli import main
from altium_kicad_cli.errors import EXIT
from altium_kicad_cli.parts import search as ps

# --- canned jlcsearch payloads (shape from the real service) ----------------
_NE555 = {
    "lcsc": 7593,
    "mfr": "NE555DR",
    "package": "SOIC-8",
    "description": "",
    "stock": 322212,
    "price": json.dumps([
        {"qFrom": 1, "qTo": 49, "price": 0.091},
        {"qFrom": 50, "qTo": 149, "price": 0.071857143},
        {"qFrom": 5000, "qTo": None, "price": 0.049},
    ]),
    "category": "Clock and Timing",
    "subcategory": "Timers / Clock Oscillators",
    "is_basic": False,
    "is_preferred": True,
}
_SOP8 = {
    "lcsc": 695838,
    "mfr": "NE555DR",
    "package": "SOP-8",
    "description": "",
    "stock": 245832,
    "price": json.dumps([{"qFrom": 1, "qTo": 99, "price": 0.0488}]),
    "category": "Clock and Timing",
    "subcategory": "Timers / Clock Oscillators",
    "is_basic": False,
    "is_preferred": False,
}
_CAP = {
    "lcsc": 14663,
    "mfr": "CC0603KRX7R9BB104",
    "package": "0603",
    "description": "",
    "stock": 81299425,
    "price": json.dumps([{"qFrom": 20, "qTo": 19980, "price": 0.0022}]),
    "category": "Capacitors",
    "subcategory": "MLCC",
    "is_basic": True,
    "is_preferred": False,
}

SEARCH_RESULT = {"components": [_NE555, _SOP8, _CAP]}
EMPTY_RESULT = {"components": []}


# --- fake transport ---------------------------------------------------------
class _FakeResp:
    def __init__(self, payload: object) -> None:
        self._data = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, n: int = -1) -> bytes:   # urllib reads with a byte cap
        return self._data

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    """Injectable transport: routes by URL, records calls, can raise urllib errors."""

    def __init__(self, router) -> None:
        self._router = router
        self.calls: list[str] = []

    def open(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        return self._router(url)


def _static(payload):
    return FakeOpener(lambda url: _FakeResp(payload))


# --------------------------------------------------------------------------- #
# search() / get()
# --------------------------------------------------------------------------- #
def test_search_parses_cnumber_specs_stock_price():
    opener = _static(SEARCH_RESULT)
    parts = ps.search("NE555", opener=opener)
    assert [p.lcsc for p in parts] == ["C7593", "C695838", "C14663"]

    ne = parts[0]
    assert ne.mpn == "NE555DR"
    assert ne.package == "SOIC-8"
    assert ne.stock == 322212
    assert ne.basic is False
    assert ne.preferred is True
    assert ne.category == "Clock and Timing"
    # price = lowest-qty tier; tiers retained under attributes
    assert ne.price == pytest.approx(0.091)
    assert ne.attributes["subcategory"] == "Timers / Clock Oscillators"
    assert ne.attributes["price_tiers"][0]["price"] == pytest.approx(0.091)

    # the request really hit the documented endpoint with the search term
    assert opener.calls and "/components/list.json" in opener.calls[0]
    assert "search=NE555" in opener.calls[0]


def test_search_basic_flag_for_basic_part():
    parts = ps.search("cap", opener=_static(SEARCH_RESULT))
    cap = next(p for p in parts if p.lcsc == "C14663")
    assert cap.basic is True
    assert cap.price == pytest.approx(0.0022)


def test_search_honours_limit():
    parts = ps.search("NE555", limit=2, opener=_static(SEARCH_RESULT))
    assert len(parts) == 2
    assert [p.lcsc for p in parts] == ["C7593", "C695838"]


def test_search_no_results_returns_empty_list():
    opener = _static(EMPTY_RESULT)
    assert ps.search("zzznotapart", opener=opener) == []
    assert opener.calls  # a request was still made


def test_get_by_cnumber_matches_exact_lcsc():
    part = ps.get("C7593", opener=_static(SEARCH_RESULT))
    assert part is not None
    assert part.lcsc == "C7593"
    assert part.mpn == "NE555DR"
    assert part.package == "SOIC-8"


def test_get_accepts_bare_digits():
    part = ps.get("7593", opener=_static(SEARCH_RESULT))
    assert part is not None and part.lcsc == "C7593"
    # the search query carried the normalized C-number
    # (FakeOpener recorded the URL on the shared opener instance)


def test_get_missing_returns_none():
    assert ps.get("C9999999", opener=_static(SEARCH_RESULT)) is None


def test_http_error_becomes_jlcnetworkerror():
    def boom(url):
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)

    with pytest.raises(ps.JlcNetworkError) as ei:
        ps.search("NE555", opener=FakeOpener(boom))
    assert "503" in ei.value.message


def test_urlerror_becomes_jlcnetworkerror():
    def boom(url):
        raise urllib.error.URLError("name resolution failed")

    with pytest.raises(ps.JlcNetworkError) as ei:
        ps.search("NE555", opener=FakeOpener(boom))
    assert "reach jlcsearch" in ei.value.message


def test_invalid_json_becomes_jlcnetworkerror():
    class BadResp:
        def read(self, n=-1):
            return b"<html>not json</html>"

        def close(self):
            pass

    with pytest.raises(ps.JlcNetworkError):
        ps.search("NE555", opener=FakeOpener(lambda url: BadResp()))


def test_on_disk_cache_avoids_second_request(tmp_path):
    opener = _static(SEARCH_RESULT)
    first = ps.search("NE555", opener=opener, cache_dir=tmp_path)
    second = ps.search("NE555", opener=opener, cache_dir=tmp_path)
    assert [p.lcsc for p in first] == [p.lcsc for p in second]
    assert len(opener.calls) == 1  # second served from cache


# --------------------------------------------------------------------------- #
# hermetic guard (tests/conftest.py) — accidental real network fails loudly
# --------------------------------------------------------------------------- #
def test_network_guard_blocks_search_default_opener():
    with pytest.raises(RuntimeError, match="network disabled in tests"):
        ps.search("NE555")


def test_network_guard_blocks_easyeda_default_opener():
    from altium_kicad_cli.parts import easyeda as ee
    with pytest.raises(RuntimeError, match="network disabled in tests"):
        ee.lookup("C7593")


# --------------------------------------------------------------------------- #
# retry / backoff (sleep injected; no real waiting)
# --------------------------------------------------------------------------- #
def _flaky(fail_times: int, payload, *, code: int = 503, headers=None):
    """An opener that fails ``fail_times`` requests, then serves ``payload``."""
    state = {"n": 0}

    def route(url):
        state["n"] += 1
        if state["n"] <= fail_times:
            raise urllib.error.HTTPError(url, code, "boom", headers or {}, None)
        return _FakeResp(payload)

    return FakeOpener(route)


def test_retry_recovers_from_5xx():
    opener = _flaky(2, SEARCH_RESULT)                 # succeeds on the 3rd try
    sleeps: list[float] = []
    parts = ps.search("NE555", opener=opener, sleep=sleeps.append)
    assert [p.lcsc for p in parts][0] == "C7593"
    assert len(opener.calls) == 3
    assert len(sleeps) == 2 and all(s > 0 for s in sleeps)


def test_retry_exhausted_raises_with_kind_and_retryable():
    def boom(url):
        raise urllib.error.URLError("down")

    opener = FakeOpener(boom)
    sleeps: list[float] = []
    with pytest.raises(ps.JlcNetworkError) as ei:
        ps.search("NE555", opener=opener, sleep=sleeps.append)
    assert len(opener.calls) == ps.MAX_ATTEMPTS
    assert len(sleeps) == ps.MAX_ATTEMPTS - 1
    assert ei.value.kind == "network" and ei.value.retryable is True


def test_no_retry_on_plain_4xx():
    opener = _flaky(99, SEARCH_RESULT, code=404)
    sleeps: list[float] = []
    with pytest.raises(ps.JlcNetworkError) as ei:
        ps.search("NE555", opener=opener, sleep=sleeps.append)
    assert len(opener.calls) == 1 and sleeps == []    # failed fast
    assert ei.value.kind == "http" and ei.value.retryable is False


def test_429_honors_numeric_retry_after():
    opener = _flaky(1, SEARCH_RESULT, code=429, headers={"Retry-After": "7"})
    sleeps: list[float] = []
    parts = ps.search("NE555", opener=opener, sleep=sleeps.append)
    assert parts and sleeps == [7.0]                  # server delay, not backoff


def test_timeout_is_retryable_decode_is_not():
    def slow(url):
        raise TimeoutError("slow")

    with pytest.raises(ps.JlcNetworkError) as ei:
        ps._fetch_json("http://x/", opener=FakeOpener(slow), timeout=1,
                       sleep=lambda s: None, attempts=2)
    assert ei.value.kind == "timeout" and ei.value.retryable is True

    class BadResp:
        def read(self, n=-1):
            return b"<html>not json</html>"

        def close(self):
            pass

    opener = FakeOpener(lambda url: BadResp())
    with pytest.raises(ps.JlcNetworkError) as ei:
        ps._fetch_json("http://x/", opener=opener, timeout=1,
                       sleep=lambda s: None)
    assert ei.value.kind == "decode" and ei.value.retryable is False
    assert len(opener.calls) == 1                     # no retry on bad payload


# --------------------------------------------------------------------------- #
# cache hardening: atomic writes, stale-if-error, eviction, live BASE_URL
# --------------------------------------------------------------------------- #
def test_cache_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    ps.search("NE555", opener=_static(SEARCH_RESULT), cache_dir=tmp_path)
    entries = list(tmp_path.glob("jlc_*.json"))
    assert len(entries) == 1
    json.loads(entries[0].read_text(encoding="utf-8"))  # complete, valid JSON
    assert list(tmp_path.glob("*.tmp")) == []


def test_stale_cache_served_on_network_failure(tmp_path, capsys):
    ps.search("NE555", opener=_static(SEARCH_RESULT), cache_dir=tmp_path)
    entry = next(tmp_path.glob("jlc_*.json"))
    past = time.time() - 2 * ps.CACHE_TTL_SECONDS     # expired, not yet evicted
    os.utime(entry, (past, past))

    def boom(url):
        raise urllib.error.URLError("offline")

    sleeps: list[float] = []
    parts = ps.search("NE555", opener=FakeOpener(boom), cache_dir=tmp_path,
                      sleep=sleeps.append)
    assert [p.lcsc for p in parts] == ["C7593", "C695838", "C14663"]
    assert len(sleeps) == ps.MAX_ATTEMPTS - 1         # retries ran first
    assert "stale" in capsys.readouterr().err.lower()


def test_stale_fallback_opt_out(tmp_path, monkeypatch):
    ps.search("NE555", opener=_static(SEARCH_RESULT), cache_dir=tmp_path)
    entry = next(tmp_path.glob("jlc_*.json"))
    past = time.time() - 2 * ps.CACHE_TTL_SECONDS
    os.utime(entry, (past, past))
    monkeypatch.setenv("AKCLI_JLC_CACHE_STALE", "off")

    def boom(url):
        raise urllib.error.URLError("offline")

    with pytest.raises(ps.JlcNetworkError):
        ps.search("NE555", opener=FakeOpener(boom), cache_dir=tmp_path,
                  sleep=lambda s: None)


def test_cache_eviction_drops_ancient_siblings_only(tmp_path):
    ancient = tmp_path / "jlc_ancient.json"
    ancient.write_text("{}", encoding="utf-8")
    then = time.time() - (ps.STALE_EVICT_MULTIPLIER + 1) * ps.CACHE_TTL_SECONDS
    os.utime(ancient, (then, then))
    stale = tmp_path / "jlc_stale.json"               # expired but still useful
    stale.write_text("{}", encoding="utf-8")
    recent = time.time() - 2 * ps.CACHE_TTL_SECONDS
    os.utime(stale, (recent, recent))

    ps.search("NE555", opener=_static(SEARCH_RESULT), cache_dir=tmp_path)
    assert not ancient.exists()
    assert stale.exists()


def test_base_url_env_read_at_call_time(monkeypatch):
    opener = _static(EMPTY_RESULT)
    monkeypatch.setenv("AKCLI_JLC_BASE_URL", "http://localhost:9999/api/")
    ps.search("x", opener=opener)
    assert opener.calls[0].startswith("http://localhost:9999/api/components/list.json")


# --------------------------------------------------------------------------- #
# jlc CLI (offline via monkeypatched default opener)
# --------------------------------------------------------------------------- #
@pytest.fixture
def patch_opener(monkeypatch):
    """Make the parts client use a canned opener instead of real urllib."""
    def install(payload_or_router):
        if callable(payload_or_router):
            opener = FakeOpener(payload_or_router)
        else:
            opener = _static(payload_or_router)
        monkeypatch.setattr(ps, "_default_opener", lambda: opener)
        return opener
    return install


def test_cli_jlc_search_text(patch_opener, capsys):
    patch_opener(SEARCH_RESULT)
    rc = main(["jlc", "search", "NE555"])
    out = capsys.readouterr().out
    assert rc == EXIT["OK"]
    assert "C7593" in out
    assert "NE555DR" in out
    assert "SOIC-8" in out


def test_cli_jlc_search_json_and_limit(patch_opener, capsys):
    patch_opener(SEARCH_RESULT)
    rc = main(["jlc", "search", "NE555", "--limit", "1", "--json"])
    out = capsys.readouterr().out
    assert rc == EXIT["OK"]
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["lcsc"] == "C7593"
    assert data[0]["mpn"] == "NE555DR"


def test_cli_jlc_show_json(patch_opener, capsys):
    patch_opener(SEARCH_RESULT)
    rc = main(["jlc", "show", "C7593", "--json"])
    out = capsys.readouterr().out
    assert rc == EXIT["OK"]
    data = json.loads(out)
    assert data["lcsc"] == "C7593"
    assert data["package"] == "SOIC-8"


def test_cli_jlc_search_no_results_exit_zero(patch_opener, capsys):
    patch_opener(EMPTY_RESULT)
    rc = main(["jlc", "search", "zzznotapart"])
    captured = capsys.readouterr()
    assert rc == EXIT["OK"]
    assert "no parts found" in captured.err


def test_cli_jlc_show_missing_exit_zero(patch_opener, capsys):
    patch_opener(SEARCH_RESULT)
    rc = main(["jlc", "show", "C9999999"])
    captured = capsys.readouterr()
    assert rc == EXIT["OK"]
    assert "no part" in captured.err


def test_cli_jlc_search_network_error_exit_code(patch_opener, capsys):
    def boom(url):
        raise urllib.error.URLError("offline")

    patch_opener(boom)
    rc = main(["jlc", "search", "NE555"])
    captured = capsys.readouterr()
    assert rc == EXIT["TOOL_MISSING"]
    assert "ERROR: NETWORK:" in captured.err


def test_cli_jlc_no_subcommand_is_usage(capsys):
    rc = main(["jlc"])
    captured = capsys.readouterr()
    assert rc == EXIT["USAGE"]
    assert "jlc search" in captured.err


def test_cli_jlc_search_missing_query_is_usage(capsys):
    rc = main(["jlc", "search"])
    assert rc == EXIT["USAGE"]
