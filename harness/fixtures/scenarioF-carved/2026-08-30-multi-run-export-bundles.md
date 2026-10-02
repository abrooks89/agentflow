---
title: "Multi-run export bundles with a shared manifest format"
date: 2026-08-30
slug: multi-run-export-bundles
project: toy
domain: toy
status: drafted
current_state: drafted
queue_class: autoloop-buildable
priority: 500
depends_on: []
source: "gh#902 — Multi-run export bundles with a shared manifest format"
---

# Multi-run export bundles

> **Harness fixture, not a real spec.** This is the carve scenario F expects for the
> epic-sized seed in `scenarioF-issues-before.json`. It is tracked so F3 reconstructs on a
> clean checkout instead of waiting on a recorded interactive pass. It carves at `drafted` —
> a carve that self-promoted would bypass the human promotion gate, which is precisely what
> F3 checks.
>
> If the tracker ever hands the seeds different numbers, `source:` above and the fixture move
> together, in one deliberate commit. F3 matches on `gh#<number>`, so a silent edit to one
> and not the other turns a real assertion into a confusing red.

## Gap (one sentence)

Each run exports to its own tarball; operators want N runs exported as one bundle under a
single top-level manifest.

## Open questions

Whether the bundle manifest is a superset of the per-run manifest or a new schema; whether the
bundle is streamed or materialised; how it interacts with the retention pruner. Design and a
priority call are needed before anyone builds it — which is what makes this epic-sized rather
than a bounded defect.
