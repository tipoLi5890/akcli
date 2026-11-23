# Documentation index

## Reference

- [SPEC.md](SPEC.md): data model, config tables, module map, JSON schemas.
- [cli-reference.md](cli-reference.md): every command, flag, exit code and error code.
- [op-list-authoring.md](op-list-authoring.md): op-list authoring guide (ops, macros, groups, hierarchy).
- [review-rules.md](review-rules.md): specification and references for every `REVIEW_*` rule.
- [sim.md](sim.md): `akcli sim` decks, assertions, sweeps and models.
- [jlc.md](jlc.md): `akcli jlc` part search, BOM purchasability, datasheet fetch, JLCPCB manufacturing handoff.
- [design-integrity.md](design-integrity.md): contracts, `verify`, the `library` workspace, fab profiles, `release preflight`.

## Guides

- [agent-state.md](agent-state.md): where workspace state lives and how sessions and agents hand work over.
- [codex-plugin.md](codex-plugin.md): the Codex plugin layout and bundled skills.
- [kicad-format-gotchas.md](kicad-format-gotchas.md): KiCad file-format rules for safe hand edits.

## Recipes

- [recipes/](recipes/README.md): copyable workflows.
  - [recipes/github-actions-pr-gate.md](recipes/github-actions-pr-gate.md): GitHub Actions workflow gating schematic pull requests (`check` SARIF upload, `review diff --fail-on-new`).

## Contracts & migration

- [stability.md](stability.md): the public contract surfaces and their gates, what is not contract, versioning rules, the deprecation policy, decisions before the 1.0 freeze.
- [migration-0.21.md](migration-0.21.md): the 0.21 contract changes (`protocol_version` 2, `schema_version` 2.0, net naming).
- [pin-identity.md](pin-identity.md): audit of the pin identity model ahead of v1.0.

## Maintainer

- [releasing.md](releasing.md): release runbook (version bump, CI gate, tag, PyPI).

`assets/hero.html` is the source of `assets/hero.png`, the README banner image.
