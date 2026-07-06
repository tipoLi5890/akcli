"""Stdlib drop-in for the tiny slice of ``requests`` the vendored code uses.

Original akcli code (project MIT license) — NOT an upstream JLC2KiCadLib file.
Surface: ``get(url, headers=...)`` returning an object with ``.status_code`` and
``.content``, plus ``codes.ok``. A module-level ``opener`` is injectable so
tests run fully offline (same pattern as :mod:`altium_kicad_cli.parts.search`).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from types import SimpleNamespace

# EasyEDA payloads are small JSON / STEP files; cap reads defensively.
_MAX_BYTES = 64 * 1024 * 1024
_TIMEOUT_S = 30.0

#: Injectable transport: any object with ``open(request, *, timeout=...)``.
opener = None

codes = SimpleNamespace(ok=200)


@dataclass
class Response:
    status_code: int
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


def get(url: str, headers: dict | None = None, timeout: float = _TIMEOUT_S) -> Response:
    req = urllib.request.Request(url, headers=dict(headers or {}))
    op = opener or urllib.request.build_opener()
    try:
        with op.open(req, timeout=timeout) as resp:
            body = resp.read(_MAX_BYTES + 1)
            if len(body) > _MAX_BYTES:
                return Response(status_code=502, content=b"")
            status = getattr(resp, "status", None) or resp.getcode() or 200
            return Response(status_code=int(status), content=body)
    except urllib.error.HTTPError as exc:  # non-2xx: report the code, never raise
        return Response(status_code=int(exc.code), content=b"")
    except (urllib.error.URLError, OSError, TimeoutError):
        return Response(status_code=599, content=b"")
