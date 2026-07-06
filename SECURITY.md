# Security Policy

`altium-kicad-cli` parses **untrusted binary and text input** (Altium `.SchDoc`/`.SchLib`/`.PcbDoc`
OLE2 containers and KiCad S-expression files) and can **write** KiCad schematics. The threat model and
the enforced safety limits below are first-class design requirements, not afterthoughts.

## Reporting a vulnerability

Please report security issues through GitHub's **private vulnerability reporting** on this repository
(the **Security** tab → *Report a vulnerability*). If private reporting is not enabled, open a GitHub
issue and prefix the title with `security:`. Please do not publicly disclose an unfixed vulnerability
before a fix is available. There is no bug-bounty program.

## Threat model

The core assumption: **input files are hostile.** A `.SchDoc` may come from an email attachment, a
shared drive, or an AI agent acting on a remote URL. A KiCad file or a JSON op-list may be attacker-
controlled. The parser and writer therefore treat every byte as adversarial.

Attack surfaces and the threats we defend against:

1. **Malformed / weaponized OLE2 (CFBF) containers.** Crafted FAT/miniFAT chains causing infinite
   loops; out-of-bounds sector references; bogus sector-shift / mini-cutoff header fields used as
   allocation bombs; cyclic or self-referential red-black directory trees; DIFAT spillover.
2. **Malformed KiCad S-expressions.** Deeply nested parentheses to exhaust the stack (recursion →
   uncatchable `SIGSEGV`); multi-megabyte atoms or unterminated quotes to exhaust memory; pathological
   node counts.
3. **Resource exhaustion (DoS).** Oversized files, decompression/decoding bombs, runaway record counts.
4. **Path traversal.** Malicious `lib_id`, config `[paths]`, op-list `target_file`, or bridge request
   paths that try to escape a project root, follow symlinks, or expand environment variables to read or
   clobber files outside the intended directory.
5. **Subprocess / command injection.** Invoking `kicad-cli` with attacker-influenced arguments.
6. **Destructive writes.** A crash or a bad op-list corrupting the user's real `.kicad_sch`.
7. **The Windows live bridge.** A second process racing or planting files in the request/response
   directory.

Out of scope: vulnerabilities in third-party tools you install yourself (KiCad, Altium), the security
of files you *intend* to overwrite with `--apply`, and protecting against a malicious local user who
already has your filesystem privileges.

## Enforced safety limits (`safety.py`)

All limits live in one module (`altium_kicad_cli.safety`) and are applied at every read/write boundary.
A violation raises a **structured** `AkcliError` with a stable error code and a non-zero exit code — a
raw traceback never reaches the agent unless `--debug` is passed.

| Limit constant | Bounds | Defeats |
|---|---|---|
| `MAX_FILE_BYTES` | total input file size | oversized-file DoS |
| `MAX_SECTORS` | OLE2 FAT/miniFAT sectors walked (with a seen-set) | FAT cycles, sector-chain bombs |
| `MAX_RECORDS` | Altium records decoded per stream | record-count exhaustion |
| `MAX_DIR_ENTRIES` | CFBF directory-tree entries visited | directory-tree cycles/explosion |
| `MAX_DECODED_BYTES` | bytes materialized from any stream | decode/allocation bombs |
| `MAX_SEXPR_DEPTH` | S-expression nesting depth | stack-exhaustion `SIGSEGV` |
| `MAX_ATOM_BYTES` | single S-expression atom size | giant-atom memory bombs |
| `MAX_NODES` | total S-expression nodes | node-count exhaustion |

Additional structural defenses:

- **Iterative parsers only.** The S-expression parser uses an explicit stack; calling
  `sys.setrecursionlimit` is banned. Cycle detection (seen-sets) guards every OLE2 FAT/miniFAT and
  directory-tree walk.
- **Header validation before allocation.** Sector-shift must be `{9, 12}`, mini-sector-shift `6`,
  mini-cutoff `4096`; the file must be ≥ 512 bytes; the DIFAT spillover chain is walked under the
  header-declared count, a cycle set, and the global sector cap (`ALTIUM_FAT_CYCLE` /
  `ALTIUM_ALLOC_GUARD` / `ALTIUM_MALFORMED` on hostile input). No untrusted header field is used to
  size an allocation unchecked.
- **Time and memory budgets** are exercised by `test_fuzz_safety.py` against a corpus of malformed
  inputs (FAT cycle, OOB sector, bogus sector-shift, huge `ndifat`, mini-cutoff bomb, truncated header,
  missing root, deeply nested S-expr, 10 MB atom, unterminated quote, symlinked lib path). Each must
  raise a structured error within budget.

### Path safety — `safety.safe_path(base, candidate)`

- `realpath`s both base and candidate and **rejects any path that escapes `base`** or traverses a
  symlink out of the allowlisted root → `PATH_OUTSIDE_ROOT`.
- **Never expands environment variables or `~`** from untrusted file contents.
- Applied to `lib_id` symbol-source resolution, config `[paths]`, op-list `target_file`, and bridge
  request/response paths.

### Subprocess safety — `safety.run_subprocess(argv, timeout, maxout)`

- `shell=False`, **absolute executable path**, an explicit `--` before any file path, a hard
  **timeout** (`KICAD_CLI_TIMEOUT`), and a captured-output cap. No string is ever passed to a shell.
- `kicad-cli` is invoked **without** `--exit-code-violations` (its ERC exits 0 even with violations);
  its absence is non-fatal (`KICAD_CLI_MISSING`) because the pure-Python verifier is primary.

### Write safety — `safety.atomic_write_with_backup(...)`

KiCad writes are **default dry-run**; an explicit `--apply` is required. On apply:

1. Snapshot the original (timestamped backup).
2. Write to a temp file **in the same directory**, then `fsync`.
3. Re-parse the temp file and run the pure-Python connectivity verifier on it.
4. Only on a clean verify, `os.replace` (atomic) into place — otherwise abort, leaving the original
   untouched (`VERIFY_FAILED`).

An mtime/hash optimistic lock detects concurrent external edits before replacing.

### Windows live bridge

The optional `drivers/altium_live/bridge.py` uses a **per-run `0700` directory**, opens files with
`O_NOFOLLOW`, writes requests atomically (`*.tmp` → rename), and enforces single-flight with a `.lock`.
A `protocol_version` handshake (`altium_ping`) rejects mismatched peers → `PROTOCOL_MISMATCH`.

## Supported versions

Pre-1.0: only the latest commit on `main` receives fixes. Once releases begin, the latest minor series
is supported.
