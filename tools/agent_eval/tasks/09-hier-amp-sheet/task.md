# Task 09 — hierarchical amp sheet (multi-sheet envelope)

The workspace holds `board.kicad_sch` (blank) and `amp.kicad_sch` (blank child,
already created). Produce ONE op-list that:

1. places a hierarchical sheet `Amp` on the board referencing `amp.kicad_sch`,
   with an `OUT` output sheet pin;
2. inside the child (route ops with the envelope's `sheets` map + per-op
   `sheet` tags): a 10k resistor `R101`, its pin 1 exposed as the sheet's
   `OUT` (hierarchical label), its pin 2 on global `GND`;
3. on the board: a 1k resistor `R2` with pin 1 on `VIN` and pin 2 on `GND`.

The cross-sheet contract: the child's hierarchical label `OUT` pairs with the
parent's same-name sheet pin; `GND` is global and spans both files. Expected
nets are evaluated from the ROOT (the whole tree).
