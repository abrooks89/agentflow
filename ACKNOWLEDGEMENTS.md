# Acknowledgements

## Vendored code (attribution required — see licenses)

- **superpowers** by [Jesse Vincent](https://github.com/obra/superpowers), (c) 2025 Jesse
  Vincent, MIT License — the thirteen process skills under `skills/_vendored/` are a
  maintained fork of this project; `skills/refine/` is a diverged fork of its brainstorming
  skill. Upstream license and provenance: `skills/_vendored/LICENSE` and
  `skills/_vendored/README.md`.

## Patterns and prior art (no code included — credited voluntarily)

- The autodoc wiki structure (per-type folders, `index.md`/`hot.md`/`log.md`, the
  read-order protocol) follows the LLM-wiki pattern popularized by **Andrej Karpathy** and
  implemented for Obsidian by
  [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) (MIT).
- [Graphify](https://github.com/safishamsi/graphify) by **Safi Shamsi** independently
  arrived at a closely related codebase-knowledge-graph design; noted here as convergent
  prior art. No code from either project is included in this repository.

## Runtime dependencies

- [web-tree-sitter](https://github.com/tree-sitter/tree-sitter) (MIT) with prebuilt
  grammars from tree-sitter-wasms (Unlicense) — installed via npm at provision time
  for the optional autodoc subsystem; not vendored.
- [PyYAML](https://github.com/yaml/pyyaml) (MIT) — installed with the agentflow package
  (`skills/_shared`); not vendored.
- [sqlite-vec](https://github.com/asg017/sqlite-vec) (MIT or Apache-2.0) — installed with
  the autodoc-graph package, only when autodoc is on; not vendored.
- [networkx](https://github.com/networkx/networkx) (BSD-3-Clause) —
  installed with the autodoc-graph package, only when autodoc is on; not vendored.
