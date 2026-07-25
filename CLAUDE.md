# CLAUDE.md

akcli — AI-native KiCad schematic CLI (read/draw/check/review/sim; Altium is
import-only). Zero runtime dependencies is a hard rule: stdlib only, no new
packages in `[project.dependencies]` (`jlc` stays the only networked surface).

## Commands

A globally installed `akcli` shadows this checkout — always prefix with
`PYTHONPATH=src` (or work in an editable install):

```
PYTHONPATH=src python -m pytest tests/ -q            # full suite
PYTHONPATH=src python -m pytest tests/test_ops.py -q # one file
PYTHONPATH=src python -m akcli <cmd> ...             # run the CLI
python -m ruff check src tests                       # lint (CI-exact scope)
python -m mypy --config-file pyproject.toml          # typed beachhead (see [tool.mypy].files)
python3 tools/golden_regen.py                        # regenerate golden snapshots (review the diff!)
```

CI = ruff + mypy + pytest on ubuntu/macos/**windows** × py3.11–3.14, plus a
real-`kicad-cli` KiCad job, wheel smoke, hooks, plugin validate. **Local green
means nothing until ruff AND mypy AND pytest all pass** — mypy runs in the
same CI job as ruff and is the one people forget locally.

## Windows portability (the recurring release-breaker)

Every historically red release CI (0.4.0, 0.8.0, 0.9.0, 0.10.0) failed only on
`windows-latest`, always on platform text/process semantics. Rules:

- **Text I/O always pins `encoding="utf-8"` and, on writes, `newline="\n"`**
  (Windows defaults: cp1252 locale codec + `\n` → `\r\n` translation, which
  breaks byte-count assertions and output determinism). Enforced by
  `tests/test_text_io_portability.py` — an AST gate over `src/akcli`,
  `tools/` and `hooks/` (`_vendor` excluded but hand-patched); new
  `write_text`/`read_text`/text-mode `open` calls must comply or CI fails.
- Artifact files whose bytes matter (`.kicad_sch`) are written in **binary**
  (`os.fdopen(fd, "wb")` in `writers/kicad.py`) — keep it that way.
- Paths emitted into JSON/goldens use `as_posix()` (backslashes break
  byte-stable snapshots).
- Subprocess: `shlex.split(..., posix=False)` semantics differ on Windows
  (POSIX splitting eats `C:\...` backslashes — see
  `hooks/pretooluse_draw_guard.py`); child processes need the real env
  (`SYSTEMROOT`); only real executables can be spawned (no POSIX script
  stubs — `WinError 193`).
- You cannot run Windows locally: treat any new file-I/O / subprocess /
  path-formatting code as suspect until the CI matrix has seen it.

## Releases

Follow `docs/releasing.md`. Order is load-bearing: bump versions
(`pyproject.toml` + both plugin manifests), move CHANGELOG `[Unreleased]` →
`[X.Y.Z]`, push `main`, **wait for CI green on that commit, only then**
`git tag vX.Y.Z && git push origin vX.Y.Z`. The Release workflow now verifies
CI green on the tagged commit itself (`ci-green` job; `workflow_dispatch`
`force` is the documented bypass) — but tagging a red commit still wastes a
version number, so the manual wait stays. Release commits are squashed
feature batches, exactly the payload most likely to hide a Windows-only bug.
PyPI dist name is `akcli-kicad` (import package and CLI stay `akcli`). PyPI publishing uses trusted publishing (OIDC, no token), gated on the `PYPI_TRUSTED_PUBLISHING`
**repository** variable (Settings → Secrets and variables → Actions →
Variables — not an Environment variable: the job's `if:` is evaluated before
its environment resolves, so an environment-scoped variable there is silently
skipped); the `pypi` GitHub Environment must still exist for OIDC trusted
publishing to bind to (see `docs/releasing.md`). GitHub Releases publish
regardless.

## Op-vocabulary lockstep (adding/changing an op or macro)

CI enforces all of these together; change them in ONE commit:

1. `src/akcli/ops.py` tables: `_CORE_OPS`/`MACRO_OPS`, `_OP_REQUIRED`,
   `_OP_FIELDS` (must equal the schema branch's properties exactly),
   `_OP_OPTIONAL`/`MACRO_OPTIONAL` (feeds `ops template`), placeholders.
2. `schemas/ops.schema.json` branch (macros too; branches carry
   `additionalProperties: false` + the universal `group` field) + capabilities
   entry in `schemas/ops.capabilities.json` (core ops only) — then run
   `python3 tools/sync_schemas.py` (mirrors in `src/akcli/schemas/` must be
   byte-identical; `--check` for drift).
3. Writer handler + `_HANDLERS` in `src/akcli/writers/kicad.py`
   (`tests/test_ops.py` census asserts `_HANDLERS`/`_MACRO_EXPANDERS` match
   the registry).
4. Docs count gates: every "N ops"/"N macros" claim (English + 中文 regexes) in
   README×3 / ROADMAP / docs/ / skills/ / commands/ must match the registry
   (`tests/test_docs_conformance.py`); `tests/test_ops.py` census; the op-name
   list in `commands/circuit-draw.md`. Also update the op-pattern prose in
   `skills/akcli-schematic-authoring/SKILL.md` (+ the op enumeration in
   `skills/akcli-circuit-design/SKILL.md`) in the same commit — the count gate
   can't see a missing pattern writeup, and the skills have drifted behind
   `commands/circuit-draw.md` before.
5. New error codes need `errors.py` `ERROR_CODES` + `_CODE_EXIT` +
   `REMEDIATION` (1:1, enforced) + the census in `tests/test_errors.py`.

## Invariants (verified by tests — don't regress)

- **Byte-identical re-apply**: deterministic UUIDv5 (root uuid +
  `designator` (op-index-free, so op-list edits keep part identity) or `tag:coords`; annotation `key`s ignore coords/index)
  + `_append_top_idempotent` replace-in-place. Every new op needs an
  apply-twice byte-equality test.
- **Connectivity is the only hard write gate**; geometry/layout findings are
  advisory. `arrange`/group moves must stay net-preserving (before/after
  `netdiff.equivalent` asserted).
- **Netdiff neutrality**: graphics ops, properties, title block never touch
  nets (netbuild ignores them) — assert it for anything new.
- KiCad grammar claims are **fixture-first**: prove a new S-expression shape
  against a real `kicad-cli` (e2e test, skip-gated on availability) before
  shipping; committed ground truth lives in `tests/fixtures/kicad/`.
- Renderer/JSON outputs are deterministic (same input bytes → same output
  bytes) — golden snapshots in `tests/golden/` catch drift; regenerate only
  deliberately.

## Key layout

`src/akcli/ops.py` (vocabulary/validator/macros/groups resolution) ·
`writers/kicad.py` (executor) · `writers/geometry.py` (transform core —
`pin_world` is canonical) · `readers/sexpr.py` + `readers/kicad*.py` ·
`netbuild.py`/`netdiff.py` (net engine + diff rails) · `checks/` ·
`arrange.py`/`groupframe.py` (layout/groups) · `commands/` (one module per CLI
family, registered in `cli.py`) · docs: `docs/op-list-authoring.md` (authoring
bible), `docs/SPEC.md`, `docs/cli-reference.md`.
