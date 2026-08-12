"""The ``--groups`` map file: parse ``{group_name: [refdes, ...]}`` (TOML/JSON).

Parsing lives next to the planner that consumes the map rather than in the
command module, so a future non-CLI caller (a hook, the workstation app) reads
the same file with the same rules. The engine reports malformed input as
:class:`GroupsFileError`; turning that into an exit code and a message is the
caller's job — this layer never decides how a process should die.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["GroupsFileError", "load_groups_file"]


class GroupsFileError(ValueError):
    """A ``--groups`` file that cannot be used, with the reason as its message.

    ``missing`` distinguishes "the path is not a usable file" (the caller maps
    it to the not-found exit class) from "the contents are wrong" (a usage
    error) — the two classes the CLI has always reported for this input.
    """

    def __init__(self, message: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.missing = missing


def load_groups_file(path: str) -> dict[str, list[str]]:
    """Parse a ``--groups`` map: ``{group_name: [refdes, ...]}`` (TOML or JSON).

    TOML uses a top-level ``[groups]`` table (or bare ``name = [...]`` tables);
    JSON is either ``{"groups": {...}}`` or the bare mapping. Order is preserved
    (groups stack down the page in file order). Malformed input raises
    :class:`GroupsFileError`.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        raise GroupsFileError(f"groups file not found: {path}", missing=True)
    except OSError as exc:  # a directory, mode 000, ... — never a traceback
        raise GroupsFileError(
            f"groups file unreadable: {path}: {exc.strerror or exc}",
            missing=True)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GroupsFileError(
            f"groups file is not UTF-8 text: {path} "
            f"(byte 0x{raw[exc.start]:02x} at offset {exc.start})")
    data: object
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise GroupsFileError(f"invalid groups JSON: {e}")
    else:
        import tomllib
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise GroupsFileError(f"invalid groups TOML: {e}")
    if isinstance(data, dict) and isinstance(data.get("groups"), dict):
        data = data["groups"]
    if not isinstance(data, dict) or not data:
        raise GroupsFileError(
            "groups file must map group-name -> [refdes, ...]")
    out: dict[str, list[str]] = {}
    for gname, refs in data.items():
        if (not isinstance(refs, list)
                or not all(isinstance(r, str) and r for r in refs)):
            raise GroupsFileError(
                f"group {gname!r} must be a list of designators")
        out[str(gname)] = list(refs)
    return out
