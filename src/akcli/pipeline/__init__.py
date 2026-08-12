"""Write-pipeline engines (rank 80): the work a write command does.

The layer between the CLI (``commands/``) and the executors (``writers/``).
A command module parses argparse, builds a request, calls one function here,
and renders the returned dataclass as text or JSON; everything BETWEEN those
two steps — staging a dry-apply, the before/after net gate, the transaction
routing, the backup swap arithmetic — lives in this package.

House rules, enforced by the gates in ``tests/``:

* **No CLI surface.** Nothing here imports ``argparse`` or ``commands``, prints
  to stdout, or builds a ``--json`` envelope: engines return dataclasses.
  Diagnostics go through the caller-supplied ``warn`` / ``log`` sinks so the
  CLI keeps ownership of stderr (and a library caller can silence it).
* **No ``_ExitWith``.** Process exit codes belong to the CLI. A refusal the
  engine has to describe comes back as data (``Refusal``) or as an
  ``AkcliError`` with an existing ``ERROR_CODES`` member — never a new code
  invented for a refactor.
* **Fully typed** — new code is born inside the mypy beachhead.

Modules: :mod:`draw` (plan/draw orchestration), :mod:`netgate` (staged
dry-apply + net diff), :mod:`preview` (SVG look-before-apply),
:mod:`undo` (backup swap/walk/transaction restore), :mod:`arrange_apply`
(auto-layout moves through the draw pipeline).
"""

from __future__ import annotations

from typing import Any

from ..errors import EXIT
from ..report import Severity

__all__ = ["draw_exit"]

_ACTIONABLE = {Severity.ERROR, Severity.CRITICAL}


def draw_exit(results: list[Any], findings: list[Any]) -> int:
    """Exit 6 (OPLIST) when any op errored or connectivity has an error finding.

    The one verdict for "was this (dry-)apply refused?": the staged gate, the
    real apply and ``commands._shared._draw_exit`` all read it here, so a
    dry-run can never disagree with the apply it is previewing.
    """
    if any(r.status == "error" for r in results):
        return EXIT["OPLIST"]
    if any(f.severity in _ACTIONABLE for f in findings):
        return EXIT["OPLIST"]
    return EXIT["OK"]
