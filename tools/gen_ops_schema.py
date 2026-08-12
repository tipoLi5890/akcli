#!/usr/bin/env python3
"""Compatibility entry point for the GENERATE half of ``tools/schemas.py``.

The generator itself (opspec -> ``schemas/ops.schema.json`` +
``ops.capabilities.json``) lives in :mod:`tools.schemas`, together with the
packaged-mirror sync it must always be paired with. Prefer::

    python3 tools/schemas.py build     # generate + mirror
    python3 tools/schemas.py check     # exit 1 on either drift

This script stays so the older invocation keeps working:

    python3 tools/gen_ops_schema.py          # write both schemas
    python3 tools/gen_ops_schema.py --check  # exit 1 on generator drift
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schemas import (  # noqa: E402,F401  (re-exported for callers/tests)
    build_capabilities,
    build_schema,
    field_schema,
    generate,
    op_branch,
    render,
)


def main(argv: list[str]) -> int:
    rc = generate(check="--check" in argv)
    if rc == 0 and "--check" not in argv:
        print("run `python3 tools/schemas.py build` to also refresh the "
              "packaged mirror")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
