"""SPICE simulation for schematics: netlist -> deck -> child engine -> asserts.

The ``akcli sim`` pipeline is a chain of small, independently testable stages:

* :mod:`.models` resolves each schematic component to a ``DeviceCard`` (a SPICE
  device letter, a normalized value, and an optional ``.model``/``.subckt``
  block). It owns the engineering-notation -> SPICE ``M``/``MEG`` fix and a
  datasheet-point diode fitter.
* :mod:`.deck` walks the schematic net graph and emits a full ngspice ``Deck``
  (title line + element lines + models), mapping every net to a sanitized SPICE
  node and reporting unmodeled / collision problems as :class:`report.Finding`.
* :mod:`.assertions` parses a ``SimSpec`` (stimuli, analyses, asserts), turns
  each assert into a ``.meas`` statement, parses the engine's ``.meas`` output
  back into numbers, and evaluates pass/fail findings.
* :mod:`.engine` runs libngspice. Because a malformed deck can make ngspice
  ``abort()`` the whole process, the library is driven **in a child
  subprocess** (``python -m akcli.sim.engine <deck> <cmds>``) so a
  fatal fault or an infinite transient is contained and killable on timeout.

Only :mod:`.engine` is provided by this task; the sibling stages land in
parallel and compose through the dataclass contracts documented above.
"""

from __future__ import annotations
