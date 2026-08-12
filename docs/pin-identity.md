# Pin identity — the audit before v1.0

Status: **audited, deliberately unchanged** (0.21). This document is the
design review the v1.0 freeze required: does the current pin identity model
block a future per-pin (`designator`, `pin`) identity upgrade? Conclusion:
**no — but the upgrade is a coordinated major-version batch**, and the
boundaries below are where it bites.

## Where "one pin" is encoded today

| Surface | Encoding | Home |
|---|---|---|
| Python model | `PinRef = tuple[str, str]` — (designator, pin number) | `src/akcli/model.py` |
| `Net.members` | sorted `PinRef` list | `src/akcli/model.py` |
| nets / diff JSON | `"REF.PIN"` flat string (`"C1.2"`) | `checks/diff.py` (`NetChange.export`, the ONE member encoding; pinned by the `diff-cut-wire` golden) |
| findings `refs` | `PinRef` | `report.py` / `model.py` |
| op-list anchors | `"R1.2"` endpoint / anchor syntax | `ops/validate.py`, `writers/` anchor resolution |
| node uuids | UUIDv5 seeds: `R3` / `R3#u2` (symbol), `R3.pin1` / `R3#u2.pin1` (pin; a *repeated* pin number gets `#2`, `#3`, …) | `writers/kicad.py` (`deterministic_uuid` call sites) |
| netlist export | net node = (ref, pin) pairs | `exporters.py` |
| agent-eval expectations | net name → member strings | `tools/agent_eval/tasks/*/expected_nets.json` |

## Verified findings

1. **`PinRef` never lands in a `.kicad_sch`, the journal, or the BOM lock
   file** (grep-verified). It exists only in *derived outputs* (nets/diff
   JSON, findings, netlist). Changing its encoding is therefore an output
   **schema break**, not an on-disk file migration — no uuid rewrite of
   existing schematics is required.
2. **The `.` separator is ambiguous in principle.** KiCad pin "numbers" are
   free strings; a pin named `1.1` (seen on connector footprints) makes
   `"REF.PIN"` non-invertible. The same convention is shared by the op-list
   anchor syntax (`"R1.2"`), so fixing one without the other would trade one
   inconsistency for another — they are a single batch.
3. **Reader and writer disagree about the unit dimension.** The reader-side
   `PinRef` is (designator, pin number) with no unit; the writer's uuid
   seed carries `#u<unit>` for units > 1. This is survivable today because
   `PinRef` is derived-output-only, but a future per-pin identity must pick
   one answer.
4. **Per-instance references (0.20) do not re-identify nodes.** On a
   multiply-instantiated sheet the uuid seed stays the scalar designator;
   `instance_designators` only changes the `(instances …)` block. BOM
   identity already uses (UniqueId, sheet) instead of the bare designator.
5. **The uuid seed vocabulary is a red line.** Every existing document's
   node uuids derive from the seed strings above; changing a seed format
   re-identifies every node, so the next re-apply would duplicate the whole
   design. A per-pin `PinRef` upgrade does **not** require touching seeds
   (finding 1); any future change that *does* must ship a one-shot uuid
   migration tool in the same major release.

## Why not now

The motivating scenario — a designator rename making `diff` report every
net's membership as changed — is real but has no reported instance yet, and
the fix (object-encoded members plus a rename-aware differ) forces the
op-list anchor syntax break at the same time (finding 2). That is a
major-version batch on its own; bundling it into 0.21's structural rework
would break the "internal refactor, byte-identical outputs" guarantee the
release depends on.

## Re-review triggers

* A reported case of pin numbers containing `.` in real libraries, or
* a real multi-instance project where (designator, pin) stops being unique
  per tree-scoped net, or
* the first consumer that needs machine-stable member identity across
  designator renames.

When triggered: encode members as objects (`ref` / `pin` / optionally
`path`), version the nets/diff schemas together, change the op-list anchor
grammar in the same protocol bump, and leave uuid seeds alone.
