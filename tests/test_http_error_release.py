"""An ``urllib.error.HTTPError`` is the open HTTP response, not just an error.

Every handler that turns one into a domain error (or a status code) must
``close()`` it — otherwise the response body/socket stays open until the
garbage collector gets to it. CPython 3.14 reports exactly that as a
``ResourceWarning`` ("Implicitly cleaning up <HTTPError 503>"), which the
suite treats as an error; these tests pin the release itself, so the contract
holds on the interpreters that say nothing.
"""

from __future__ import annotations

import io
import urllib.error

import pytest


class _Body(io.BytesIO):
    """A response body that remembers being closed."""


def _http_error(url: str, code: int = 503):
    """``(HTTPError, its body)`` — the body is what must end up closed."""
    body = _Body(b"unavailable")
    err = urllib.error.HTTPError(url, code, "Service Unavailable",
                                 {"Retry-After": "0"}, body)
    return err, body


class _RaisingOpener:
    """Opener whose every request fails with a (tracked) HTTPError.

    It keeps each error alive on purpose: a dropped HTTPError closes its body
    when it is deallocated, which would make "closed" true for the leak too.
    """

    def __init__(self, code: int = 503) -> None:
        self.code = code
        self.raised: list[urllib.error.HTTPError] = []
        self.bodies: list[_Body] = []

    def open(self, request, *, timeout=None):
        err, body = _http_error(request.full_url, self.code)
        self.raised.append(err)
        self.bodies.append(body)
        raise err


def test_easyeda_fetch_releases_the_http_error():
    from akcli.parts import easyeda

    opener = _RaisingOpener()
    with pytest.raises(easyeda.EasyEdaError) as ei:
        easyeda._fetch_json_once("https://example.invalid/x", opener=opener,
                                 timeout=1.0)
    assert ei.value.kind == "http" and ei.value.retry_after == 0.0
    assert [b.closed for b in opener.bodies] == [True]


def test_jlcsearch_fetch_releases_the_http_error():
    from akcli.parts import search

    opener = _RaisingOpener()
    with pytest.raises(search.JlcNetworkError) as ei:
        search._fetch_json_once("https://example.invalid/x", opener=opener,
                                timeout=1.0)
    assert ei.value.kind == "http" and ei.value.retry_after == 0.0
    assert [b.closed for b in opener.bodies] == [True]


def test_datasheet_download_releases_the_http_error(tmp_path):
    from akcli.parts import datasheet, easyeda

    opener = _RaisingOpener(code=403)
    with pytest.raises(easyeda.EasyEdaError):
        datasheet.fetch_pdf("https://example.invalid/ds.pdf",
                            tmp_path / "ds.pdf", opener=opener)
    assert [b.closed for b in opener.bodies] == [True]
    assert list(tmp_path.iterdir()) == []            # and no .part left behind


def test_vendored_http_shim_releases_the_http_error(monkeypatch):
    from akcli._vendor.jlc2kicadlib import _http

    opener = _RaisingOpener(code=404)
    monkeypatch.setattr(_http, "opener", opener)
    resp = _http.get("https://example.invalid/x")
    assert resp.status_code == 404 and resp.content == b""
    assert [b.closed for b in opener.bodies] == [True]


def test_doctor_network_probe_releases_the_http_error(monkeypatch):
    import urllib.request

    from akcli.commands import doctor

    raised: list[urllib.error.HTTPError] = []        # kept alive, see above
    bodies: list[_Body] = []

    def fake_urlopen(request, timeout=None):
        err, body = _http_error(request.full_url, 405)
        raised.append(err)
        bodies.append(body)
        raise err

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok, detail, hint = doctor._check_network()
    # any HTTP answer proves reachability
    assert ok is True and "HTTP 405" in detail and hint == ""
    assert [b.closed for b in bodies] == [True]
