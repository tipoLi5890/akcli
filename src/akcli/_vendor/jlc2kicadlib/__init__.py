"""Vendored JLC2KiCadLib (MIT, (c) 2021 TousstNicolas) — see LICENSE + PROVENANCE.md.

Only the conversion core is vendored (footprint/ + symbol/ + helper.py). Its two
upstream dependencies are NOT vendored: ``requests`` is replaced by the stdlib
shim ``_http`` and the GPLv3 ``KicadModTree`` by the clean-room ``_kmt`` writer
(implemented from the KiCad footprint file format, not from KicadModTree code).
"""
