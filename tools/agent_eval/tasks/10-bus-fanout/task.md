# Task 10 — 2-bit bus fan-out

On a blank sheet: a horizontal bus labelled `D[0..1]`, two bus entries, and
two 33R series resistors — `D0` reaches `R1.1`, `D1` reaches `R2.1` through
wires off the bus entries; both resistors' far pins sit on global `GND`.

Teaches: `add_bus` + `add_bus_entry` (a bus entry must land ON the bus and
its free end ON a wire — dangling entries are refused), bus range labels, and
wire routing from an entry's 45° free end down to a pin.
