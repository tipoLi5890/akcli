# altium_live — optional Windows live SCH driver

The **Altium live driver** lets `akcli` apply an op-list (SPEC §2) to a
schematic inside a **running** Altium Designer, via a small file-based JSON bridge.

It has two halves:

| Half | File | Runs on | Validated |
|---|---|---|---|
| Python bridge | [`bridge.py`](bridge.py) | any OS (offline-unit-testable) | macOS/Linux/Windows CI (mocked `response.json`) |
| DelphiScript | [`scripts/altium_api.pas`](scripts/altium_api.pas) + [`scripts/altium_api.PrjScr`](scripts/altium_api.PrjScr) | **Windows + Altium Designer 22+ only** | **manual, on the user's Windows box** |

> This driver is **optional**. The cross-platform KiCad writer
> (`writers/kicad.py`) needs none of it. Altium has no headless/offline write path
> on macOS/Linux, so live Altium edits require Windows + a licensed Altium install.
> Everything below the Python/DelphiScript boundary is therefore unvalidatable from
> the dev/CI environment and is shipped as a faithful, well-commented scaffold to be
> iterated on Windows.

---

## 1. How it works (the bridge method)

1. The Python bridge writes a **`request.json`** into a shared *bridge directory*
   (atomically: `request.json.tmp` → rename), guarded by a **`.lock`** file so only
   one request is ever in flight (single-flight, §3).
2. The DelphiScript `Run` procedure — already running inside Altium — reads
   `request.json`, resolves the target schematic, dispatches each op against the
   public SCH API, and writes **`response.json`** atomically
   (`response.json.tmp` → rename) so the poller never sees a partial file.
3. The bridge **polls** for `response.json` every ~200 ms until it appears or the
   timeout elapses, parses it, and removes the request/response pair.

Both halves agree on the **`protocol_version` handshake** (currently `1`, equal to
`ops.PROTOCOL_VERSION`). A mismatch is rejected document-wide with the frozen error
code **`PROTOCOL_MISMATCH`** (`errors.py`).

### The bridge directory

The two halves agree on **one base directory**. `bridge.py`'s `default_bridge_dir()`
resolves it from env **`AKCLI_ALTIUM_BRIDGE_DIR`**, else the per-user default
**`%TEMP%\akcli-altium-bridge\`**. Inside that base, **each call carves a per-run
`0700` sub-directory `run-<hex>/`** (`O_NOFOLLOW`) and writes `request.json` *there*,
then polls `run-<hex>/response.json`. The single-flight **`.lock`** lives in the
**base** dir, so only one `run-<hex>/` ever holds a pending request at a time.

Because `request.json` is consumed by a process Altium *already started*, an env
var set by the bridge will **not** propagate into a live Altium. The DelphiScript
therefore resolves the base dir in this priority (see `ResolveBridgeDir`) and then
finds the active run dir (see `FindActiveRunDir` — the newest `run-*` holding a
`request.json`):

1. env **`AKCLI_ALTIUM_BRIDGE_DIR`** (matches `bridge.py`; works when the bridge
   launches Altium itself);
2. pointer file **`%TEMP%\akcli-altium-bridge.path`** (first line = the base dir;
   script-side convenience for when Altium is pre-running);
3. default **`%TEMP%\akcli-altium-bridge\`** (matches `bridge.py`'s default).

When Altium is pre-running, set the env var the same way for the Altium process, or
use the pointer file, or just rely on the shared default base dir.

---

## 2. Request / response JSON protocol

### 2.1 `request.json`

The request body **is** an akcli op-list document (SPEC §2.3) plus a `command`
discriminator:

```jsonc
{
  "protocol_version": 1,
  "command": "apply_ops",          // "apply_ops" (default) | "altium_ping"
  "target_format": "altium",
  "target_file": "C:\\work\\project\\main.SchDoc",
  "run_id": "f1e2d3c4-....",         // echoed back in the response
  "ops": [
    { "op": "place_component", "lib_id": "Resistor", "designator": "R99",
      "x_mil": 1000, "y_mil": 2000, "rotation": 90, "value": "10k" },
    { "op": "add_wire", "vertices": ["R99.1", [1100, 2000]] },
    { "op": "place_power_port", "lib_id": "GND", "net_name": "GND", "at": [1100, 2100] }
  ]
}
```

* Coordinates are **mils**, origin top-left (SPEC §2.1).
* `rotation`/`orientation` ∈ `{0,90,180,270}`; `mirror` ∈ `{none,x,y}`.
* A wire/no-connect endpoint may be a **`"REF.PIN"`** string (snapped to the pin's
  world coordinate) or a raw **`[x,y]`** mils point.
* `add_bus` / `add_bus_entry` return **`OP_UNSUPPORTED`** in the v1 Altium driver
  (they remain valid for the KiCad writer).

### 2.2 `altium_ping` handshake

A request with `"command": "altium_ping"` does no editing and returns the version
pair so the bridge can confirm a compatible script is live before sending ops:

```jsonc
// request
{ "protocol_version": 1, "command": "altium_ping", "run_id": "ping-1" }

// response
{ "protocol_version": 1, "altium_version": "22.11.1",
  "status": "ok", "run_id": "ping-1",
  "error_code": null, "message": "altium_ping ok", "results": [] }
```

### 2.3 `response.json`

The document envelope wraps **one per-op result object per op**. Each result
**matches the KiCad writer's `OpResult` shape exactly** (SPEC §2.4):

```jsonc
{
  "protocol_version": 1,
  "altium_version": "22.11.1",
  "status": "ok",                    // "ok" | "error" (document-level)
  "run_id": "f1e2d3c4-....",
  "error_code": null,                // document-level error code or null
  "message": "applied 3 op(s); verify by re-exporting the netlist ...",
  "results": [
    { "op_index": 0, "op": "place_component",
      "status": "ok", "created_uuids": ["<Altium UniqueId>"],
      "error_code": null, "message": "" },
    { "op_index": 1, "op": "add_wire",
      "status": "ok", "created_uuids": ["<UniqueId>"],
      "error_code": null, "message": "" },
    { "op_index": 2, "op": "place_power_port",
      "status": "ok", "created_uuids": ["<UniqueId>"],
      "error_code": null, "message": "lib_id=GND" }
  ]
}
```

* `created_uuids` are Altium **`UniqueId`** strings read back after the object is
  registered in the document.
* Per-op `error_code` values are the same frozen `errors.py` codes the rest of the
  tool uses (`OP_UNSUPPORTED`, `VERIFY_FAILED`, `NON_ORTHOGONAL_WIRE`, `OFF_GRID`,
  …). A single failing op never aborts the run; it yields an `"error"` result and
  the remaining ops still execute.
* Document-level rejections (`PROTOCOL_MISMATCH`, missing target document, wrong
  `target_format`) set the top-level `status:"error"` + `error_code` with an empty
  `results` array.

### 2.4 Verify-by-re-export

A successful apply does **not** itself prove electrical correctness — the SCH API
places primitives but does not re-run connectivity. The response `message` reminds
you to **re-export the Altium netlist and diff it** against the intended
connectivity (the same "verify by re-export / tolerance compare" posture the KiCad
writer's connectivity gate uses). Treat the live driver's `status:"ok"` as
"ops placed", not "design verified".

---

## 3. The `.lock` single-flight

Only one request may be in flight per base bridge directory. The Python bridge:

1. creates **`.lock`** (`O_CREAT|O_EXCL`) in the **base** dir — if it already exists,
   another apply is running and the new one is rejected (`BridgeBusy`);
2. makes a fresh `0700` `run-<hex>/` sub-dir and writes `run-<hex>/request.json`
   atomically;
3. polls for `run-<hex>/response.json`;
4. removes the whole `run-<hex>/` dir, then releases (`unlink`) `.lock`.

The DelphiScript does not need to take the lock (it only ever processes the single
active `run-<hex>/request.json`) but it **does** write `response.json` atomically
(`response.json.tmp` → rename) so a mid-write poll can never read a truncated file.

---

## 4. Launching on Windows

The DelphiScript half must be **loaded once** in the running Altium, then invoked
per request. Two supported styles:

### 4.1 Invoke via the command line (what the bridge expects)

The bridge shells out to the Altium executable with the documented
`-RScriptingSystem:RunScript(...)` form:

```bat
"C:\Program Files\Altium\AD22\X2.EXE" ^
  -RScriptingSystem:RunScript("ProjectName=<...>\scripts\altium_api.PrjScr|ProcName=altium_api>Run")
```

* `ProjectName` points at the paired **`altium_api.PrjScr`** (which lists
  `altium_api.pas`).
* `ProcName` is **`altium_api>Run`** (`<unit>` `>` `<procedure>`).
* This targets the *running* Altium instance and runs `Run` once, which processes
  exactly one `request.json` and writes one `response.json`.

Set the base bridge directory first (env or pointer file, §1):

```bat
set AKCLI_ALTIUM_BRIDGE_DIR=%TEMP%\akcli-altium-bridge
```

### 4.2 Run interactively (for first-time setup / debugging)

1. Open Altium Designer 22+ with the target schematic.
2. **File → Open Project…** → select `scripts/altium_api.PrjScr`.
3. **DXP → Run Script…** (or the Scripting panel) → choose `altium_api.pas` → `Run`.

Drop a `request.json` into the bridge dir first; each `Run` consumes one request.

---

## 5. Safety guidance (read before any live write)

This driver **mutates a real schematic**. It is not yet validated on Windows, so:

* **Snapshot before writing.** Commit the design to version control (or copy the
  whole project folder) immediately before the first apply, every session.
* **Test on a copy first.** Point `target_file` at a *throwaway duplicate* of the
  schematic until you trust a given op-list, then re-run against the real file.
* **One undo transaction.** `Run` wraps the whole op-list in
  `ProcessControl.PreProcess`/`PostProcess`, so a single Ctrl+Z reverts the entire
  apply — but undo is **not** a substitute for a snapshot.
* **Keep the lock single-flight.** Never run two applies against the same bridge
  directory concurrently.
* **Verify by re-export.** After apply, re-export the netlist and diff it (see
  §2.4). `status:"ok"` means "ops placed", not "verified".
* **Trust boundary.** `request.json` is produced by the local bridge only; the
  bridge dir is `0700` and `O_NOFOLLOW`. Do not point the bridge at a
  world-writable location.

---

## 6. Clean-room / attribution

**Independent reimplementation; no source copied; attribution retained where
structures are referenced** (SPEC Appendix B).

* `altium_api.pas` references **only** Altium's public, documented DelphiScript /
  SCH Scripting API (`SchServer`, `ISch_Document`, `SchObjectFactory`,
  `ISch_Iterator`, `ISch_Component`/`Wire`/`Junction`/`Netlabel`/`PowerObject`/
  `Label`/`NoERC`, `MilsToCoord`, …) and the **documented file-based-bridge
  method**. None of its JSON reader/writer, SCH dispatch, or protocol envelope is
  copied from any third-party project.
* Our `protocol_version`, the `command`/`results` envelope, the per-op
  `OpResult` shape, and the structured `error_code` enum are original to this
  project (the same definitions used by `ops.py` / `errors.py` / `writers/kicad.py`).
* The **high-level pattern** — "drive Altium from an AI agent through a JSON file
  bridge, with a `protocol_version` field and structured `ERROR: CODE` strings" —
  is credited to **flaco-source/altium-mcp** (2026) and
  **coffeenmusic / Siddharth Ahuja** (2025). Their full MIT attribution is recorded
  in the repository's [`THIRD_PARTY_NOTICES.md`](../../../../THIRD_PARTY_NOTICES.md).
  No bytes of their schema or source are reused.

**Platform validity:** the DelphiScript half is validated **only on Windows +
Altium Designer 22+** and cannot be exercised from this macOS/Linux/CI session; the
JSON envelope is unit-tested on the Python side against a mocked `response.json`.
