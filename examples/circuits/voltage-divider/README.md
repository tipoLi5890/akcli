# Voltage divider — the smallest complete op-list

Two resistors, three named nets, drawn with the **label-on-pin** pattern (the
blessed connection ladder's first rung: no wire routing needed, every pin is
anchored by its label).

```bash
akcli new board.kicad_sch
akcli plan board.kicad_sch --ops ops.json
akcli draw board.kicad_sch --ops ops.json --apply --strict-nets
akcli nets board.kicad_sch --json
```

`plan` dry-runs and prints the net diff; `draw --apply --strict-nets` writes
behind the connectivity gate. The resulting nets are pinned in
`expected_nets.json` (and CI applies this exact op-list and asserts them —
the example cannot drift from the tool).

Points to copy:

- `add_net_label` with `"at": "REF.PIN"` snaps the label to the pin's world
  coordinate — position-independent, rotation-proof.
- Two labels with the same name (`VOUT`) ARE the connection; no wire op needed.
- Re-running `draw --apply` is byte-identical (deterministic UUIDv5 idempotency).
