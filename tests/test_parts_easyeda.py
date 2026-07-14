"""Fully-offline tests for the EasyEDA metadata lookup (``parts/easyeda.py``).

NO real network: every request is served by a ``FakeOpener`` exposing
``open(request, timeout=...)`` (the same surface as ``urllib.request.OpenerDirector``)
and returning canned EasyEDA ``/components`` JSON. The lookup is a *light* metadata +
3D-availability read — it converts nothing.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from akcli.parts import easyeda as ee


# --- canned EasyEDA /components payloads -------------------------------------
def _svgnode(uuid: str) -> str:
    return "SVGNODE~" + json.dumps(
        {
            "gId": "g1",
            "nodeName": "svg",
            "attrs": {"uuid": uuid, "c_etype": "outline3D", "c_origin": "0,0"},
            "childNodes": [],
        }
    )


def _result(*, with_3d: bool) -> dict:
    shape = ["TRACK~1~...", "PAD~..."]
    if with_3d:
        shape.append(_svgnode("3d-uuid-abc123"))
    return {
        "success": True,
        "code": 0,
        "result": {
            "title": "RP2040 dual ARM Cortex-M0+",
            "description": "microcontroller",
            "lcsc": {"id": 1, "number": "C2040", "url": "https://lcsc.com/x"},
            "dataStr": {
                "head": {
                    "c_para": {
                        "Manufacturer": "Raspberry Pi",
                        "Manufacturer Part": "RP2040",
                        "link": "https://datasheet.example/rp2040.pdf",
                        "package": "QFN-56",
                    }
                }
            },
            "packageDetail": {
                "dataStr": {
                    "head": {"c_para": {"package": "QFN-56_L7.0-W7.0-P0.40"}},
                    "shape": shape,
                }
            },
        },
    }


RESULT_3D = _result(with_3d=True)
RESULT_NO_3D = _result(with_3d=False)
NOT_FOUND_FALSE = {"success": False, "code": 1, "result": None, "message": "not found"}
NOT_FOUND_EMPTY = {"success": True, "code": 0, "result": {}}


# --- fake transport ----------------------------------------------------------
class _FakeResp:
    def __init__(self, payload: object) -> None:
        if isinstance(payload, bytes):
            self._data = payload
        else:
            self._data = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        return self._data

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    """Injectable transport: records the Request objects + URLs, can raise urllib errors."""

    def __init__(self, router) -> None:
        self._router = router
        self.calls: list[str] = []
        self.requests: list = []

    def open(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        self.requests.append(req)
        return self._router(url)


def _static(payload):
    return FakeOpener(lambda url: _FakeResp(payload))


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_lookup_parses_metadata_and_detects_3d():
    opener = _static(RESULT_3D)
    info = ee.lookup("C2040", opener=opener)
    assert info is not None
    assert info.lcsc == "C2040"
    assert info.manufacturer == "Raspberry Pi"
    assert info.mpn == "RP2040"
    assert info.datasheet == "https://datasheet.example/rp2040.pdf"
    assert info.package == "QFN-56_L7.0-W7.0-P0.40"
    assert info.title == "RP2040 dual ARM Cortex-M0+"
    assert info.has_3d is True
    assert info.model_uuid == "3d-uuid-abc123"
    assert info.source == "easyeda-std"
    # it hit the documented /components endpoint with the C-prefixed id
    assert opener.calls and opener.calls[0].endswith("/api/products/C2040/components")


def test_lookup_no_3d_when_no_svgnode():
    info = ee.lookup("C2040", opener=_static(RESULT_NO_3D))
    assert info is not None
    assert info.has_3d is False
    assert info.model_uuid is None
    # other metadata still present
    assert info.mpn == "RP2040"


def test_lookup_accepts_bare_digits_and_normalizes():
    opener = _static(RESULT_3D)
    info = ee.lookup("2040", opener=opener)
    assert info is not None and info.lcsc == "C2040"
    assert opener.calls[0].endswith("/api/products/C2040/components")


def test_lookup_empty_lcsc_returns_none_without_request():
    opener = _static(RESULT_3D)
    assert ee.lookup("", opener=opener) is None
    assert opener.calls == []


# --------------------------------------------------------------------------- #
# not-found is None, NOT an error
# --------------------------------------------------------------------------- #
def test_success_false_is_not_found_none():
    assert ee.lookup("C9999999", opener=_static(NOT_FOUND_FALSE)) is None


def test_empty_result_is_not_found_none():
    assert ee.lookup("C9999999", opener=_static(NOT_FOUND_EMPTY)) is None


# --------------------------------------------------------------------------- #
# transport failures -> EasyEdaError
# --------------------------------------------------------------------------- #
def test_http_error_becomes_easyedaerror():
    def boom(url):
        raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)

    with pytest.raises(ee.EasyEdaError) as ei:
        ee.lookup("C2040", opener=FakeOpener(boom))
    assert "500" in ei.value.message


def test_urlerror_becomes_easyedaerror():
    def boom(url):
        raise urllib.error.URLError("name resolution failed")

    with pytest.raises(ee.EasyEdaError) as ei:
        ee.lookup("C2040", opener=FakeOpener(boom))
    assert "reach easyeda" in ei.value.message


def test_timeout_becomes_easyedaerror():
    def boom(url):
        raise TimeoutError("slow")

    with pytest.raises(ee.EasyEdaError) as ei:
        ee.lookup("C2040", opener=FakeOpener(boom))
    assert "timed out" in ei.value.message


def test_malformed_json_becomes_easyedaerror():
    with pytest.raises(ee.EasyEdaError):
        ee.lookup("C2040", opener=FakeOpener(lambda url: _FakeResp(b"<html>nope</html>")))


# --------------------------------------------------------------------------- #
# gzip transparently decoded (we advertise Accept-Encoding: gzip)
# --------------------------------------------------------------------------- #
def test_gzip_body_is_decoded():
    import gzip as _gz

    body = _gz.compress(json.dumps(RESULT_3D).encode("utf-8"))
    info = ee.lookup("C2040", opener=FakeOpener(lambda url: _FakeResp(body)))
    assert info is not None and info.has_3d is True


# --------------------------------------------------------------------------- #
# browser-like headers (UA + Referer) are set on the request
# --------------------------------------------------------------------------- #
def test_browser_headers_are_set():
    opener = _static(RESULT_3D)
    ee.lookup("C2040", opener=opener)
    req = opener.requests[0]
    headers = {k.lower(): v for k, v in req.header_items()}
    assert "mozilla" in headers["user-agent"].lower()
    assert headers["referer"] == "https://easyeda.com/"


# --------------------------------------------------------------------------- #
# on-disk cache hit avoids a second request
# --------------------------------------------------------------------------- #
def test_on_disk_cache_avoids_second_request(tmp_path):
    opener = _static(RESULT_3D)
    a = ee.lookup("C2040", opener=opener, cache_dir=tmp_path)
    b = ee.lookup("C2040", opener=opener, cache_dir=tmp_path)
    assert a is not None and b is not None
    assert a.model_uuid == b.model_uuid
    assert len(opener.calls) == 1  # second served from cache
