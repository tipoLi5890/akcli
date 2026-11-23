# Recipes

Copyable workflows built on the public CLI contract ([stability.md](../stability.md)).

- [github-actions-pr-gate.md](github-actions-pr-gate.md): a GitHub Actions workflow that gates
  schematic pull requests (`check` SARIF upload, `check` gate, `review diff --fail-on-new`).

Every `akcli` command line in a recipe is validated against the live argument parser by
`tests/test_docs_conformance.py`: a recipe lists its commands in a fenced `bash` block, and the
`run:` lines of its workflow must all appear in that block.
